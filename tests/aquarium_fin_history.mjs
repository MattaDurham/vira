import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {createFinController,createFinMotion} from '../static/aquarium-fins.js';
import {finActuation} from '../static/aquarium-motion.js';

const tau=Math.PI*2,delayIndex=16;
const quiet=(side,pectoral)=>({age:0,speed:.24,forwardSpeed:.24,brake:0,stroke:0,
  muscle3:0,yawRate:0,finSteer:0,pitchRate:0,pitch:0,
  // Hold a known loaded carrier phase, independently of command onset.
  finPhase:tau*.19-(side<0?.75:0)-(pectoral?0:1.35)});
const snapshot=motion=>({...motion,samples:Array.from(motion.samples)});
const channel=(motion,index=delayIndex)=>motion.samples[index*2];
const zero=motion=>motion.samples.every(value=>Math.abs(value)<1e-12);
const near=(actual,expected,message)=>assert.ok(Math.abs(actual-expected)<1e-11,message);

// The tissue samples isolate compliance from the immediate rigid movement at
// the basal joint. They are authored response delays, not measured koi EMG.
export function assertFinHistory(create=createFinController){
  for(const side of [-1,1])for(const pectoral of [true,false]){
    for(const poseHistory of [true,false]){
      const controller=create(side,pectoral),fish={...quiet(side,pectoral),poseHistory};
      const at=(age,active)=>snapshot(controller.sample(Object.assign(fish,{age,yawRate:active?.8:0})));
      at(.76,false);at(.90,false);at(.999999,false);
      const onset=at(1,true);
      assert.ok(channel(onset,0)>0,'the observed command loads the basal tissue response');
      assert.equal(channel(onset),0,'a new command does not reach distal tissue before its travel delay');
      at(1.05,true);
      assert.equal(channel(at(1.119,true)),0,'distal tissue retains the actual quiet commands before onset arrives');
      assert.ok(channel(at(1.13,true))>0,'the observed loaded command reaches distal tissue after its delay');
      for(let i=1;i<=26;i++)at(1.13+i*.01,true);
      at(1.399999,true);
      const release=at(1.4,false);
      assert.equal(channel(release,0),0,'the basal response releases with the observed command');
      assert.ok(channel(release)>0,'distal tissue retains its earlier loading during basal release');
      at(1.45,false);
      assert.ok(channel(at(1.519,false))>0,'distal release follows the actual command history');
      assert.equal(channel(at(1.53,false)),0,'distal tissue settles after the release reaches it');

      // All control inputs are present in the stored paddle output. A braking
      // command must also travel rather than be projected into past phases.
      for(let i=1;i<=12;i++)at(1.53+i*.01,false);
      fish.brake=1;fish.age=1.66;
      const braking=controller.sample(fish);
      assert.ok(channel(braking,0)>0,'actual flow-dependent braking loads the tissue');
      assert.equal(channel(braking),0,'new braking demand is not backdated into distal tissue');
    }

    // Interpolate the signed observed paddle before the nonlinear loading and
    // recovery response. This also exercises a crossing between stroke signs.
    const interpolation=create(side,pectoral),fish=quiet(side,pectoral);
    fish.yawRate=.8;
    const positive=finActuation(fish,side,pectoral).paddle;
    interpolation.sample(fish);
    fish.age=.1;fish.finPhase=tau*.69-(side<0?.75:0)-(pectoral?0:1.35);
    const negative=finActuation(fish,side,pectoral).paddle;
    assert.ok(positive>0&&negative<0,'the fixture spans power and recovery');
    interpolation.sample(fish);
    fish.age=.15;
    const mixed=interpolation.sample(fish),paddle=positive*.7+negative*.3;
    const loaded=Math.max(0,paddle)/(.06+Math.abs(paddle));
    const recovery=Math.max(0,-paddle)/(.025+Math.abs(paddle));
    near(channel(mixed),(.010*loaded-.003*recovery)*(pectoral?1:.7),
      'distal cupping interpolates actual signed paddle across observations');
    near(mixed.samples[delayIndex*2+1],(.50*loaded-.08*recovery)*(pectoral?1:.7),
      'distal feathering uses the same observed paddle history');

    // Repeatedly wrap the bounded history at a fast desktop refresh rate.
    // Compare against separately recorded real commands, including changing
    // brake/turn demand and both signs of the carrier stroke.
    const fast=create(side,pectoral),commands=[];
    for(let frame=0;frame<=720;frame++){
      const fish={...quiet(side,pectoral),age:frame/240,finPhase:frame*.06,
        yawRate:.6*Math.sin(frame*.017),brake:frame%140<70?.8:0};
      commands.push(finActuation(fish,side,pectoral).paddle);
      const motion=fast.sample(fish);
      if(frame<60)continue;
      for(const sampleIndex of [1,16,31]){
        const location=frame-sampleIndex/32*.24*240;
        const low=Math.floor(location),fraction=location-low;
        const paddle=commands[low]*(1-fraction)+commands[low+1]*fraction;
        const loaded=Math.max(0,paddle)/(.06+Math.abs(paddle));
        const recovery=Math.max(0,-paddle)/(.025+Math.abs(paddle));
        near(channel(motion,sampleIndex),(.010*loaded-.003*recovery)*(pectoral?1:.7),
          'history wrap preserves the complete delayed window of actual control inputs');
      }
    }

    const startup=create(side,pectoral).sample({...quiet(side,pectoral),age:4,yawRate:.8});
    assert.ok(channel(startup,0)>0,'the first observed loading is available at the attachment');
    assert.equal(channel(startup),0,'unobserved time before startup remains at rest');

    const modes=create(side,pectoral),live={...quiet(side,pectoral),age:5,yawRate:.8};
    modes.sample(live);live.age=5.12;
    const livePose=snapshot(modes.sample(live));
    const held={...quiet(side,pectoral),age:live.age,poseHistory:false};
    const heldMotion=modes.sample(held),heldPose=snapshot(heldMotion);
    assert.notEqual(heldPose.flap,livePose.flap,'a paused live-to-held switch applies the new basal fin pose');
    assert.ok(zero(heldPose),'a new held pose starts without the discarded live commands');
    const pausedHeld=modes.sample({...held,yawRate:.8,finPhase:held.finPhase+.4});
    assert.strictEqual(pausedHeld,heldMotion,'an unchanged held pose key preserves paused output identity');
    assert.deepEqual(snapshot(pausedHeld),heldPose,'same-mode paused controls do not insert observations');
    const otherHeld={...held,yawRate:.8};
    const otherPose=snapshot(modes.sample(otherHeld,'fins'));
    assert.notEqual(otherPose.flap,heldPose.flap,'a different held axis applies its controls at the same age');
    assert.ok(otherPose.samples.every(Number.isFinite),'new inspection-mode response samples stay finite');
    assert.ok(channel(otherPose,0)>0,'the new held mode records its current basal command');
    assert.equal(channel(otherPose),0,'a new held axis has no invented past tissue loading');
    otherHeld.age+=.13;
    assert.ok(channel(modes.sample(otherHeld,'fins'))>0,'progressing held axes retain actual elapsed fin history');
    const backToLive=snapshot(modes.sample({...live,age:otherHeld.age}));
    assert.equal(channel(backToLive),0,'returning to live starts a new command history at the current age');

    const controller=create(side,pectoral),glide=quiet(side,pectoral);
    let motion=controller.sample(glide),samples=motion.samples;
    for(let frame=1;frame<=2400;frame++){
      glide.age=frame/120;glide.finPhase+=.04;
      motion=controller.sample(glide);
      assert.strictEqual(motion.samples,samples,'live response tables reuse their allocated storage');
      assert.ok(zero(motion),'a long undisturbed glide stays quiet through history pruning and carrier cycles');
    }
    const paused=snapshot(motion);
    for(let i=0;i<400;i++){
      glide.finPhase+=.5;glide.yawRate=i%2?.8:0;
      assert.strictEqual(controller.sample(glide),motion,'paused simulation ages do not add commands');
      assert.deepEqual(snapshot(motion),paused,'paused output remains idempotent even if controls are inspected again');
    }
    glide.age+=1/120;glide.yawRate=.8;
    motion=controller.sample(glide);
    assert.equal(channel(motion),0,'paused observations never populate future delayed commands');

    // Reset to a quiet earlier time after an active sequence. No command from
    // that discarded future may appear in the new response field.
    Object.assign(glide,quiet(side,pectoral));
    assert.ok(zero(controller.sample(glide)),'age rewind discards future loading');
    glide.age=.06;glide.yawRate=.8;
    const reset=snapshot(controller.sample(glide));
    assert.ok(channel(reset,0)>0,'rewound observations accept new basal commands');
    assert.equal(channel(reset),0,'rewound startup begins at rest before its first observed command');
    glide.age=.18;
    near(channel(controller.sample(glide)),channel(reset,0),'rewound commands arrive after their real elapsed delay');
    glide.age=2;
    const gap=controller.sample(glide);
    assert.equal(channel(gap),0,'an unobserved gap does not invent past control commands');
  }

  // Frozen authored poses retain the stateless cycle API used by geometry
  // fixtures. An elapsed controller is an explicit choice for live animation.
  const fish={...quiet(1,true),yawRate:.8};
  const first=createFinMotion(fish,1,true);
  const second=createFinMotion({...fish,finPhase:fish.finPhase+Math.PI},1,true);
  assert.notDeepEqual(first.samples,second.samples,'stateless authored samples still inspect different carrier phases');
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  assertFinHistory();
  console.log('Fin command history passed: causal onset/release, signed interpolation, quiet glide, pause, rewind and gaps.');
}
