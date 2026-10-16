import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Quaternion} from '../static/vendor/three.webgpu.js';
import {createSwimmer} from '../static/aquarium-motion.js';
import {applySwimPose} from '../static/aquarium-rig.js';
import {createContainment} from '../static/aquarium-collision.js';
import {tankCenterZ} from '../static/aquarium-space.js';
import {createFinFrame,createFinController,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';
import {createCaudalFrame,createCaudalController,bindCaudalGeometry,deformCaudalGeometry} from '../static/aquarium-caudal.js';

const bendKeys=['turnBend','turnBend1','turnBend2','turnBend3'];
const recruitmentKeys=['turnRecruitment','turnRecruitment1','turnRecruitment2','turnRecruitment3'];
const withoutBend=Object.fromEntries(bendKeys.map(key=>[key,0]));
const seeded=seed=>()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
const yaw=quaternion=>{
  const direction=new Vector3(1,0,0).applyQuaternion(quaternion);
  return Math.atan2(-direction.z,direction.x);
};

async function geometry(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'TurnGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const joints=[],fins=[];
  asset.traverse(mesh=>{
    if(mesh.isBone&&mesh.name.startsWith('spine_'))joints.push(mesh);
    if(mesh.isMesh&&/^(Pectoral|Pelvic|Caudal)_/.test(mesh.name))fins.push({mesh,
      base:Float32Array.from(mesh.geometry.attributes.position.array),paired:!mesh.name.startsWith('Caudal'),
      pectoral:mesh.name.startsWith('Pectoral')});
  });
  joints.sort((a,b)=>a.name.localeCompare(b.name));
  const rest=joints.map(b=>b.quaternion.clone()),body=asset.getObjectByName('Koi_skin');
  assert.equal(joints.length,34,'turns retain the actual articulated spine');
  assert.ok(body?.isSkinnedMesh,'turn trajectories use the exported skin');
  const skin=body.geometry.attributes.position;
  const skinIndices=[-.25,-.05,.18,.45,.53].map(x=>{
    let nearest=Infinity,index=0;
    for(let i=0;i<skin.count;i++){
      const distance=(skin.getX(i)-x)**2+skin.getY(i)**2+(skin.getZ(i)-.08)**2;
      if(distance<nearest){nearest=distance;index=i;}
    }
    return index;
  });
  const skinXs=Array.from({length:skin.count},(_,i)=>skin.getX(i));
  const ringIndices=[-.315,0,.2].map(x=>{
    const nearest=skinXs.reduce((best,value)=>Math.abs(value-x)<Math.abs(best-x)?value:best);
    return skinXs.map((value,i)=>Math.abs(value-nearest)<1e-6?i:-1).filter(i=>i>=0);
  });
  assert.ok(ringIndices.every(indices=>indices.length>100),'the trunk probe averages complete material rings');
  for(const pectoral of [true,false]){
    const family=fins.filter(fin=>fin.paired&&fin.pectoral===pectoral);
    const frames=Object.fromEntries(family.filter(fin=>fin.mesh.name.includes('_membrane'))
      .map(fin=>[Math.sign(fin.base[2]),createFinFrame(fin.base,fin.mesh.geometry.attributes.uv.array)]));
    family.forEach(fin=>{fin.binding=bindFinGeometry(fin.base,frames);});
  }
  const membrane=fins.find(fin=>fin.mesh.name.startsWith('Caudal_membrane'));
  const caudalFrame=createCaudalFrame(membrane.base,membrane.mesh.geometry.attributes.uv.array);
  fins.filter(fin=>!fin.paired).forEach(fin=>{fin.binding=bindCaudalGeometry(fin.base,caudalFrame);});
  const skinPoints=()=>{
    body.skeleton.update();
    return skinIndices.map(index=>asset.worldToLocal(body.getVertexPosition(index,new Vector3()).applyMatrix4(body.matrixWorld)));
  };
  const trunkSag=()=>{
    body.skeleton.update();
    const [a,b,c]=ringIndices.map(indices=>{
      const center=new Vector3();
      for(const index of indices)center.add(asset.worldToLocal(body.getVertexPosition(index,new Vector3()).applyMatrix4(body.matrixWorld)));
      return center.divideScalar(indices.length);
    });
    const dx=c.x-a.x,dz=c.z-a.z;
    return (dx*(b.z-a.z)-dz*(b.x-a.x))/Math.hypot(dx,dz);
  };
  return {asset,joints,rest,fins,skinPoints,trunkSag};
}

// These are bounds and temporal relationships for this authored model, not
// universal biological curvature or reaction-time limits. Read actual bones
// and skin so a diagnostic field with no geometric effect cannot pass.
export async function assertTurnBend(create=createSwimmer,pose=applySwimPose){
  const {asset,joints,rest,fins,skinPoints,trunkSag}=await geometry(),report={turns:[],maximumCorrection:0};
  const stations=[14,24,32]; // anterior trunk, posterior trunk, peduncle.
  const isolated=fish=>({...fish,drive:0,stroke:0,muscle1:0,muscle2:0,muscle3:0,bank:0});
  for(const sign of [-1,1]){
    const swimmer=create(()=>.5);
    Object.assign(swimmer.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
    pose(asset,joints,rest,isolated(swimmer.fish));
    const headOrigin=joints[0].getWorldQuaternion(new Quaternion()).invert();
    const lengths=joints.slice(1).map(b=>b.getWorldPosition(new Vector3()).distanceTo(b.parent.getWorldPosition(new Vector3())));
    swimmer.navigate({x:0,y:.8,z:tankCenterZ+sign*.345});
    const records=[];let headOnset=null,previousSkin,maxSkinStep=0,maxAngle=0,phaseSample;
    for(let frame=0;frame<420;frame++){
      swimmer.step(1/120);
      const fish=swimmer.fish;
      if(frame===120)phaseSample={...fish};
      // Finish one actual turning bout, then request a quiet stop before a
      // subsequent bout can hide the first pulse's posterior recovery.
      if(frame===131)swimmer.navigate({...fish});
      const sample=isolated(fish);
      pose(asset,joints,rest,{...sample,...withoutBend});
      const straight=stations.map(i=>joints[i].getWorldQuaternion(new Quaternion()).invert());
      const straightSkin=skinPoints();
      pose(asset,joints,rest,sample);
      const angles=stations.map((i,j)=>yaw(joints[i].getWorldQuaternion(new Quaternion()).multiply(straight[j])));
      const skin=skinPoints().map((point,i)=>point.sub(straightSkin[i]));
      if(previousSkin)skin.forEach((point,i)=>{maxSkinStep=Math.max(maxSkinStep,point.distanceTo(previousSkin[i]));});
      previousSkin=skin;
      if(headOnset===null&&Math.abs(yaw(joints[0].getWorldQuaternion(new Quaternion()).multiply(headOrigin)))>.005)headOnset=fish.age;
      angles.forEach(angle=>{maxAngle=Math.max(maxAngle,Math.abs(angle));
        assert.ok(angle*sign<=1e-8,'the internal turn bend follows its latched side without copying carrier phase');});
      bendKeys.forEach(key=>assert.ok(Number.isFinite(fish[key])&&Math.abs(fish[key])<=.22+1e-12,
        'the finite turning command and its regional responses respect the authored magnitude bound'));
      recruitmentKeys.forEach(key=>assert.ok(Number.isFinite(fish[key])&&fish[key]>=0&&fish[key]<=1,
        'causal regional turn recruitment remains a bounded mode blend'));
      joints.slice(1).forEach((bone,i)=>assert.ok(Math.abs(bone.getWorldPosition(new Vector3())
        .distanceTo(bone.parent.getWorldPosition(new Vector3()))-lengths[i])<1e-6,'bending preserves every actual bone length'));
      // The skull's extra pulse is exactly zero; its normal heading remains
      // free to lead the internal body bend.
      const head=joints[0].getWorldQuaternion(new Quaternion());
      pose(asset,joints,rest,{...sample,...withoutBend});
      assert.ok(head.normalize().angleTo(joints[0].getWorldQuaternion(new Quaternion()).normalize())<1e-6,
        'the turning pulse cannot flex or detach the skull');
      records.push({age:fish.age,angles});
    }
    const peaks=stations.map((_,i)=>Math.max(...records.map(r=>Math.abs(r.angles[i]))));
    assert.ok(peaks[0]>.015&&peaks[1]>.06&&peaks[2]>.10,
      'the finite turning stroke produces a real anterior, trunk and peduncle bend');
    const onset=stations.map((_,i)=>records.find(r=>Math.abs(r.angles[i])>.003)?.age);
    const peakTime=stations.map((_,i)=>records.find(r=>Math.abs(r.angles[i])===peaks[i]).age);
    const returned=stations.map((_,i)=>records.find(r=>r.age>peakTime[i]&&Math.abs(r.angles[i])<peaks[i]*.2)?.age);
    assert.ok(Number.isFinite(headOnset)&&onset.every(Number.isFinite)&&returned.every(Number.isFinite),
      'the actual head and all three axial regions load and return during the probe');
    assert.ok(headOnset<onset[0],'the head changes its heading before the internal turn stroke');
    for(let i=1;i<stations.length;i++){
      assert.ok(onset[i]>onset[i-1],`loading travels from anterior trunk toward the actual peduncle: ${onset.join(', ')}`);
      assert.ok(peakTime[i]>peakTime[i-1],'the curvature pulse peaks in successive axial regions');
      assert.ok(returned[i]>returned[i-1],'straightening travels posteriorly instead of releasing the whole fish at once');
    }
    assert.ok(maxAngle<=.22+1e-8,'the actual added axial rotation respects the command bound');
    assert.ok(maxSkinStep<.008,'the added bend moves actual skin continuously without a regional snap');
    assert.ok(bendKeys.every(key=>Math.abs(swimmer.fish[key])<1e-8),'the finite stroke settles during a quiet glide');
    assert.ok(recruitmentKeys.every(key=>swimmer.fish[key]<.001),'regional carrier recruitment releases during a quiet glide');
    assert.ok(records.at(-1).angles.every(angle=>Math.abs(angle)<1e-8),'settled regional commands leave the actual body straight');

    // A frozen authored study must not borrow a previous live turn's history.
    const phasePose=sample=>{
      pose(asset,joints,rest,{...sample,bank:0,verticalGain:0,twistGain:0});
      return skinPoints();
    };
    const held={...phaseSample,poseHistory:false,stroke:1};
    const heldA=phasePose({...held,...Object.fromEntries(recruitmentKeys.map(key=>[key,0]))});
    const heldB=phasePose({...held,...Object.fromEntries(recruitmentKeys.map(key=>[key,1]))});
    assert.ok(heldA.every((point,i)=>point.distanceTo(heldB[i])<1e-10),
      'an authored held pose ignores stale live carrier-recruitment history');
    report.turns.push({sign,headOnset,onset,peakTime,returned,peaks,maxSkinStep});
  }

  // Sample loading, the loaded stroke and recovery on both sides. A nascent
  // yaw lag is not the finite turn stroke: existing cruise beats may continue
  // before that region loads, then return as its finite bend relaxes.
  report.carrierPhase=[];
  for(const sign of [-1,1]){
    const swimmer=create(()=>.5);
    Object.assign(swimmer.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
    swimmer.navigate({x:.8,y:.8,z:tankCenterZ+sign*.2});
    const measure=sample=>{
      pose(asset,joints,rest,{...sample,x:0,y:0,z:0,yaw:0,pitch:0,bank:0,verticalGain:0,twistGain:0});
      return {sag:trunkSag(),tail:joints[32].getWorldQuaternion(new Quaternion())};
    };
    for(let frame=1;frame<=132;frame++){
      swimmer.step(1/120);
      if(![60,87,108,132].includes(frame))continue;
      const fish={...swimmer.fish},intended=measure(isolated(fish));
      const yawOnly=measure({...isolated(fish),...withoutBend});
      const neutral=measure({...isolated(fish),...withoutBend,yawLag1:0,yawLag2:0,yawLag3:0});
      const idealSag=intended.sag-neutral.sag;
      const finiteSag=intended.sag-yawOnly.sag;
      const inverse=neutral.tail.invert();
      const idealTail=yaw(intended.tail.clone().multiply(inverse));
      const low={sag:Infinity,tail:Infinity},high={sag:-Infinity,tail:-Infinity};
      for(let phase=0;phase<12;phase++){
        const actual=measure({...fish,phase:phase*Math.PI/6});
        const values={sag:Math.sign(idealSag)*(actual.sag-neutral.sag),
          tail:Math.sign(idealTail)*yaw(actual.tail.multiply(inverse))};
        for(const key of ['sag','tail']){low[key]=Math.min(low[key],values[key]);high[key]=Math.max(high[key],values[key]);}
      }
      // These are authored visibility policies, not measured koi thresholds.
      // Gate the skin assertion on finite-pulse sag, not total yaw-lag sag.
      if(Math.abs(finiteSag)>.003)assert.ok(low.sag>Math.abs(idealSag)*.5,
        'a meaningfully loaded finite turn retains actual trunk curvature across opposing carrier phases');
      if(Math.abs(fish.turnBend3)>.22*.25)assert.ok(low.tail>Math.abs(idealTail)*.5,
        'a loaded posterior turn cannot reverse with an independent cruise beat');
      if(frame===60)assert.ok(high.tail-low.tail>.05,
        'a nascent posterior turn retains the preceding cruise beat before regional loading');
      if(frame===132)assert.ok(high.sag-low.sag>.003,
        'anterior carrier motion resumes as the finite stroke releases');
      report.carrierPhase.push({sign,age:fish.age,finiteSag,idealSag,idealTail,low,high});
    }
  }

  // Join the rendered material curve to the following physical impulse. If a
  // loaded turn has replaced the cruise wave, changing that absent carrier
  // must not accelerate the same body differently. Ordinary strokes retain
  // both their visible wave and their phase-dependent forward effort.
  report.carrierThrust=[];
  for(const mode of ['cruise','loading','loaded','releasing']){
    const frames={cruise:60,loading:60,loaded:87,releasing:132}[mode];
    const samples=[];
    for(let phase=0;phase<12;phase++){
      const swimmer=create(()=>.5);
      Object.assign(swimmer.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
      swimmer.navigate(mode==='cruise'?{x:-.85,y:.8,z:tankCenterZ}:{x:.8,y:.8,z:tankCenterZ+.2});
      for(let frame=0;frame<frames;frame++)swimmer.step(1/120);
      const fish=swimmer.fish;
      fish.phase=phase*Math.PI/6;
      pose(asset,joints,rest,{...fish,x:0,y:0,z:0,yaw:0,pitch:0,bank:0,verticalGain:0,twistGain:0});
      const beforeSag=trunkSag(),tailAngle=yaw(joints[32].getWorldQuaternion(new Quaternion()));
      const velocity=new Vector3(fish.vx,fish.vy,fish.vz);
      swimmer.step(1/120);
      const direction=new Vector3(-Math.cos(fish.yaw)*Math.cos(fish.pitch),Math.sin(fish.pitch),Math.sin(fish.yaw)*Math.cos(fish.pitch));
      const acceleration=new Vector3(fish.vx,fish.vy,fish.vz).sub(velocity).dot(direction)*120;
      pose(asset,joints,rest,{...fish,x:0,y:0,z:0,yaw:0,pitch:0,bank:0,verticalGain:0,twistGain:0});
      samples.push({beforeSag,tailAngle,afterSag:trunkSag(),acceleration});
    }
    const range=key=>Math.max(...samples.map(sample=>sample[key]))-Math.min(...samples.map(sample=>sample[key]));
    const sagRange=range('beforeSag'),nextSagRange=range('afterSag'),tailRange=range('tailAngle'),accelerationRange=range('acceleration');
    if(mode==='loaded'){
      assert.ok(sagRange<1e-7&&nextSagRange<1e-7,'the loaded physical probe actually has the same material bend across carrier phases');
      assert.ok(accelerationRange<1e-7,'an absent cruise carrier cannot surge the loaded turning body');
    }else if(mode==='cruise'){
      assert.ok(sagRange>.003&&nextSagRange>.003,'ordinary cruising retains its actual travelling material wave');
      assert.ok(accelerationRange>.05,'ordinary cruising retains phase-dependent forward effort');
    }else if(mode==='loading'){
      assert.ok(sagRange>.001&&nextSagRange>.001,'partial loading retains some actual cruise motion');
      assert.ok(accelerationRange>.001,'partial posterior loading retains some carrier effort');
    }else {
      assert.ok(sagRange>.001&&nextSagRange>.001,'the anterior material wave returns during progressive release');
      assert.ok(tailRange<1e-7,'the posterior material remains recruited while the anterior carrier returns');
      assert.ok(accelerationRange<1e-7,'the posterior drive stays phase-independent until its own release');
    }
    report.carrierThrust.push({mode,sagRange,nextSagRange,tailRange,accelerationRange});
  }

  const tiny=create(()=>.5);
  Object.assign(tiny.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
  tiny.navigate({x:-.9*Math.cos(.2),y:.8,z:tankCenterZ+.9*Math.sin(.2)});
  let tinyPeak=0,tinyRecruitment=0;
  for(let frame=0;frame<180;frame++){
    tiny.step(1/120);pose(asset,joints,rest,isolated(tiny.fish));
    bendKeys.forEach(key=>{tinyPeak=Math.max(tinyPeak,Math.abs(tiny.fish[key]));});
    recruitmentKeys.forEach(key=>{tinyRecruitment=Math.max(tinyRecruitment,tiny.fish[key]);});
  }
  assert.ok(tinyPeak<.001,'a small course correction does not recruit the large-turn C bend');
  assert.ok(tinyRecruitment<.01,'gentle fin trim does not switch off the cruise wave');
  report.tinyPeak=tinyPeak;

  // Flick escape enters its bout through a separate production path. It must
  // initialize the same finite stroke rather than wait for a later turn.
  const escape=create(()=>.5);
  Object.assign(escape.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
  escape.flick({x:0,y:.8,z:tankCenterZ+.2});
  let escapePeak=0;
  for(let frame=0;frame<60;frame++){
    escape.step(1/120);
    escapePeak=Math.max(escapePeak,Math.abs(escape.fish.turnBend));
  }
  assert.ok(escapePeak>.15&&escapePeak<=.22,'a flick initializes a bounded escape stroke during its first bout');
  report.escapePeak=escapePeak;

  report.repeatedFlicks=[];
  const flickToward=(swimmer,heading)=>swimmer.flick({x:swimmer.fish.x+.2*Math.cos(heading),
    y:swimmer.fish.y,z:swimmer.fish.z-.2*Math.sin(heading)});
  for(const firstSign of [-1,1])for(const relativeSign of [-1,1]){
    const repeated=create(()=>.5),uninterrupted=create(()=>.5);
    for(const swimmer of [repeated,uninterrupted]){
      Object.assign(swimmer.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
      flickToward(swimmer,firstSign*Math.PI/2);
      for(let frame=0;frame<24;frame++)swimmer.step(1/120);
    }
    const before=repeated.fish.turnBend;
    assert.ok(Math.abs(before)>.15,'the repeated-flick probe interrupts an already loaded turn');
    pose(asset,joints,rest,isolated(repeated.fish));
    const beforeSkin=skinPoints();
    flickToward(repeated,firstSign*relativeSign*Math.PI/2);
    repeated.step(1/120);
    const commandJump=Math.abs(repeated.fish.turnBend-before);
    // A smooth restart traverses at most the full signed .44 rad range.
    // Its first sample is bounded by 3*t² over the .27 s escape loading stage.
    const maximumEntryJump=.44*3*((1/120)/(.9*.3))**2;
    assert.ok(commandJump<maximumEntryJump+1e-12,'a repeated flick preserves the current source command at pulse entry');
    pose(asset,joints,rest,isolated(repeated.fish));
    const repeatedSkin=skinPoints();
    uninterrupted.step(1/120);pose(asset,joints,rest,isolated(uninterrupted.fish));
    const uninterruptedSkin=skinPoints();
    const skinDifference=Math.max(...repeatedSkin.map((p,i)=>p.distanceTo(uninterruptedSkin[i])));
    assert.ok(skinDifference<.001,'a restarted turn does not pull actual skin abruptly away from its continuing trajectory');
    const continuingStep=Math.max(...uninterruptedSkin.map((p,i)=>p.distanceTo(beforeSkin[i])));
    const repeatedStep=Math.max(...repeatedSkin.map((p,i)=>p.distanceTo(beforeSkin[i])));
    assert.ok(repeatedStep<continuingStep+.001,'repeated-flick skin motion remains continuous with the established stroke');
    for(let frame=1;frame<36;frame++){
      repeated.step(1/120);
      assert.ok(bendKeys.every(key=>Math.abs(repeated.fish[key])<=.22+1e-12),
        'same-side and opposite-side restarts stay inside the convex bend bound');
    }
    assert.equal(Math.sign(repeated.fish.turnBend),firstSign*relativeSign,
      'the preserved command progresses smoothly to the newly selected turn side');
    report.repeatedFlicks.push({firstSign,relativeSign,commandJump,skinDifference});
  }

  // During recovery and rapid retriggers, the full carrier-to-turn transition
  // must also remain continuous, rather than only its isolated C-bend channel.
  const transitions=create(()=>.5);
  Object.assign(transitions.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
  let priorFullSkin,maximumFullStep=0;
  for(let frame=0;frame<480;frame++){
    if([0,24,48].includes(frame))flickToward(transitions,frame===24?Math.PI/2:-Math.PI/2);
    if(frame===180)transitions.navigate({...transitions.fish});
    transitions.step(1/120);pose(asset,joints,rest,transitions.fish);
    const current=skinPoints();
    if(priorFullSkin)current.forEach((point,i)=>{maximumFullStep=Math.max(maximumFullStep,point.distanceTo(priorFullSkin[i]));});
    priorFullSkin=current;
  }
  assert.ok(maximumFullStep<.015,'the full skin remains continuous through carrier release and repeated turn recruitment');
  report.maximumFullTransitionStep=maximumFullStep;

  // Complete joint check: actual membranes/rays receive their production
  // deformation before the articulated skin and collision probes are read.
  const containment=createContainment(asset);
  for(const seed of [913,481,33,123,456,789]){
    const swimmer=create(seeded(seed));
    const controllers={pectoral:{'-1':createFinController(-1,true),'1':createFinController(1,true)},
      pelvic:{'-1':createFinController(-1,false),'1':createFinController(1,false)}};
    const caudalController=createCaudalController();
    let activeTurns=0;
    for(let frame=0;frame<120*120;frame++){
      swimmer.step(1/120);const fish=swimmer.fish;
      const motions=Object.fromEntries(Object.entries(controllers).map(([kind,sides])=>[kind,
        {'-1':sides[-1].sample(fish),'1':sides[1].sample(fish)}]));
      const caudalMotion=caudalController.sample(fish);
      if(frame%24)continue;
      if(Math.abs(fish.turnBend)>.05)activeTurns++;
      for(const fin of fins){
        const output=fin.mesh.geometry.attributes.position.array;
        if(fin.paired)deformFinGeometry(fin.binding,motions[fin.pectoral?'pectoral':'pelvic'],output);
        else deformCaudalGeometry(fin.binding,caudalMotion,output);
      }
      pose(asset,joints,rest,fish);
      const correction=containment.correction(),length=Math.hypot(correction.x,correction.y,correction.z);
      report.maximumCorrection=Math.max(report.maximumCorrection,length);
      assert.ok(length<.001,`the actual bending body and deformed fins clear the tank for routine seed ${seed}`);
      swimmer.contain(correction);
    }
    assert.ok(activeTurns>10,'each long routine probe includes multiple substantial finite turn strokes');
  }
  return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  console.log(`Finite turn bend passed: ${JSON.stringify(await assertTurnBend())}`);
}
