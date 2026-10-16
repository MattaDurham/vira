// Public source-controller/navigation join, with a named one-tick intent freeze.
// A real controller retains a legally queued inactive state through the factory
// seam. Ordinary navigation's request(0) clears that queue; this fixture tests
// the join contract without claiming the same state occurs on a normal route.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {createSourceSteeringController} from '../static/aquarium-source-steering.js';
import {createSwimmer} from '../static/aquarium-motion.js';

const data=JSON.parse(await readFile(new URL('../static/aquarium/reference-turn.json',import.meta.url),'utf8'));
const clone=x=>structuredClone(x),wrap=x=>Math.atan2(Math.sin(x),Math.cos(x));
const safe={min:{x:-1,y:.3,z:-.5},max:{x:1,y:1.4,z:.4}},unsafe={min:{x:-3,y:.3,z:-.5},max:{x:1,y:1.4,z:.4}};

function oneTickIntentFreezeFixture(create,clock,rejectFuture){
  const actual=createSourceSteeringController(data,{duration:.2,urgentDuration:.2});
  const proposals=[],peeks=[],poses=[];let freezeIntent=false,commitCount=0;
  const wrapper={
    reset:(...args)=>actual.reset(...args),
    request:(...args)=>freezeIntent?false:actual.request(...args),
    ready:(...args)=>actual.ready(...args),sample:(...args)=>actual.sample(...args),
    peek:(seconds,options)=>{const p=actual.peek(seconds,options);peeks.push({seconds,options:clone(options),packet:clone(p)});return p;},
    propose:(dt,options)=>{const p=actual.propose(dt,options);proposals.push({dt,options:clone(options),packet:clone(p.gesture),headDelta:p.headDelta});return p;},
    commit:p=>{commitCount++;return actual.commit(p);}
  };
  const swimmer=create(()=>.5,{turnControllerFactory:()=>wrapper});
  actual.reset({headYaw:0});
  assert.equal(actual.request(.3,0,{headYaw:0,duration:.2}),true);
  actual.commit(actual.propose(clock,{authority:1}));
  const before=clone(actual.sample());assert.equal(before.active,false);
  assert.equal(actual.request(-.6,clock,{headYaw:before.headHeading,duration:.2}),false);
  assert.equal(actual.ready(),false,'opposing request legally queues during the quiet recovery');
  assert.equal(actual.peek(.3,{authority:1}).active,true,'queued source starts inside the forecast horizon');
  Object.assign(swimmer.fish,{yaw:before.headHeading,yawRate:0,yawAcceleration:0,age:clock,
    turnGestureActive:false,turnHeadingBudget:.3,turnFinClearance:1,turnSourceWeight:0});
  const initialYaw=swimmer.fish.yaw;
  swimmer.footprint(pose=>{
    poses.push({pose:clone(pose),peek:peeks.length?clone(peeks.at(-1)):null});
    return rejectFuture&&poses.length>1?unsafe:safe;
  });
  freezeIntent=true;
  const dt=1/120,zeroAuthority=actual.propose(dt,{authority:0});
  swimmer.step(dt);
  const forecasts=poses.slice(1).map(({pose,peek})=>({
    time:peek.seconds,authority:peek.options.authority,
    futureSourceActive:peek.packet.active,sourceBody:peek.packet.b,
    forecastBody:[pose.turnAnterior,pose.turnTrunk,pose.turnPosterior],
    sourceWeight:peek.packet.sourceReplacementWeight,forecastWeight:pose.turnSourceWeight,
    yawError:wrap(initialYaw+peek.packet.headDelta-pose.yaw),
    yawLags:[pose.yawLag1,pose.yawLag2,pose.yawLag3]
  }));
  const tickProposal=proposals.find(p=>Math.abs(p.dt-dt)<1e-12),committed=clone(actual.sample());
  assert.ok(tickProposal,'one actual navigation tick proposes a source packet');
  return {clock,rejectFuture,forecasts,commitCount,
    tickProposal:{authority:tickProposal.options.authority,active:tickProposal.packet.active,headDelta:tickProposal.headDelta},
    sameTickIfAuthorityZero:{active:zeroAuthority.gesture.active,headDelta:zeroAuthority.headDelta},
    fishAfter:{power:swimmer.fish.turnPower,
      body:[swimmer.fish.turnAnterior,swimmer.fish.turnTrunk,swimmer.fish.turnPosterior],
      yawLags:[swimmer.fish.yawLag1,swimmer.fish.yawLag2,swimmer.fish.yawLag3]},
    committed:{power:committed.power,b:committed.b},actualHeadDelta:wrap(swimmer.fish.yaw-initialYaw)};
}

export function assertSourceForecast(create=createSwimmer){
  const activation=oneTickIntentFreezeFixture(create,.759,true),preOnset=oneTickIntentFreezeFixture(create,.65,false);
  assert.equal(preOnset.forecasts.length,3,'a safe forecast checks .10, .20 and .30 seconds');
  assert.equal(preOnset.forecasts[0].futureSourceActive,false,'the first sample precedes queued source onset');
  assert.equal(preOnset.forecasts.at(-1).futureSourceActive,true,'the same whole-horizon plan includes source motion');
  for(const test of [activation,preOnset]){
    assert.equal(test.commitCount,1,'a navigation tick commits the accepted source proposal exactly once');
    for(const f of test.forecasts){
      assert.ok(Math.abs(f.yawError)<1e-12,`one source plan must preserve its head clock even before onset: t=${f.time}, error=${f.yawError}`);
      assert.deepEqual(f.forecastBody,f.sourceBody,'forecast body uses the same authority/time as its head');
      assert.equal(f.forecastWeight,f.sourceWeight);
      assert.ok(f.yawLags.every(x=>x===0),'source-plan forecasts cannot inject legacy yaw lag');
    }
    assert.deepEqual(test.fishAfter.body,test.committed.b,'actual body uses the committed gesture');
    assert.equal(test.fishAfter.power,test.committed.power,'thrust power uses the committed gesture, not the preceding snapshot');
  }
  assert.equal(activation.forecasts.length,4,'the fixture rejects each candidate clearance authority');
  assert.equal(activation.tickProposal.authority,0,'newly active source cannot bypass rejected clearance with authority1');
  assert.equal(activation.tickProposal.active,true);
  assert.ok(Math.abs(activation.tickProposal.headDelta-activation.sameTickIfAuthorityZero.headDelta)<1e-14);
  assert.ok(Math.abs(activation.actualHeadDelta-activation.tickProposal.headDelta)<1e-14,'actual root consumes the exact source headDelta once');
  assert.ok(activation.fishAfter.yawLags.every(x=>x===0),'newly committed source cannot inject fallback yaw lag');
  return {forecastSamples:activation.forecasts.length+preOnset.forecasts.length,
    maximumForecastYawError:Math.max(...[...activation.forecasts,...preOnset.forecasts].map(f=>Math.abs(f.yawError))),
    activationHeadDelta:activation.actualHeadDelta,commits:[activation.commitCount,preOnset.commitCount],
    scope:'One-tick intent freeze retains a legal queued API state; normal navigation reachability is not claimed.'};
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href)console.log(JSON.stringify(assertSourceForecast()));
