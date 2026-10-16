import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Quaternion} from '../static/vendor/three.webgpu.js';
import {createSwimmer} from '../static/aquarium-motion.js';
import {applySwimPose,createFacialRig} from '../static/aquarium-rig.js';
import {createTurnController} from '../static/aquarium-turn.js';
import {createResearchShape} from '../static/aquarium-research-shape.js';
import {createMedianController,createMedianFin,deformMedianFin} from '../static/aquarium-median.js';
import {createManeuverFootprint} from '../static/aquarium-footprint.js';
import {createSupportArticulation} from '../static/aquarium-supports.js';
import {createStudyRandom,createTurnStudy} from '../static/aquarium-study.js';
import {createContainment} from '../static/aquarium-collision.js';
import {tankCenterZ} from '../static/aquarium-space.js';
import {createFinFrame,createFinController,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';
import {createCaudalFrame,createCaudalController,bindCaudalGeometry,deformCaudalGeometry} from '../static/aquarium-caudal.js';

const bendKeys=['turnBend','turnBend1','turnBend2','turnBend3'];
const issuedKeys=['turnAnterior','turnTrunk','turnPosterior'];
const issuedVelocityKeys=issuedKeys.map(key=>key+'Velocity');
const recruitmentKeys=['turnRecruitment','turnRecruitment1','turnRecruitment2','turnRecruitment3'];
const withoutBend=Object.fromEntries([...bendKeys,...issuedKeys,...issuedVelocityKeys].map(key=>[key,0]));
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
  const shape=createResearchShape(asset),face=createFacialRig(asset),footprint=createManeuverFootprint(asset),supports=createSupportArticulation(asset);
  const median=['Dorsal','Anal'].map(prefix=>createMedianFin(asset.getObjectByName(`${prefix}_membrane`),asset.getObjectByName(`${prefix}_branched_rays`),{kind:prefix.toLowerCase()}));
  return {asset,joints,rest,fins,skinPoints,trunkSag,shape,face,footprint,supports,median};
}

// These are bounds and temporal relationships for this authored model, not
// universal biological curvature or reaction-time limits. Read actual bones
// and skin so a diagnostic field with no geometric effect cannot pass.
export async function assertTurnBend(create=createSwimmer,pose=applySwimPose){
  const {asset,joints,rest,fins,skinPoints,trunkSag,shape,face,footprint,supports,median}=await geometry(),report={turns:[],maximumCorrection:0};
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
      bendKeys.forEach(key=>assert.ok(Number.isFinite(fish[key])&&Math.abs(fish[key])<=1.8+1e-12,
        'the finite turning command and its regional responses respect the authored magnitude bound'));
      recruitmentKeys.forEach(key=>assert.ok(Number.isFinite(fish[key])&&fish[key]>=0&&fish[key]<=.25+1e-12,
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
    const onset=stations.map((_,i)=>records.find(r=>Math.abs(r.angles[i])>peaks[i]*.05)?.age);
    const peakTime=stations.map((_,i)=>records.find(r=>Math.abs(r.angles[i])===peaks[i]).age);
    const returned=stations.map((_,i)=>records.find(r=>r.age>peakTime[i]&&Math.abs(r.angles[i])<peaks[i]*.2)?.age);
    assert.ok(Number.isFinite(headOnset)&&onset.every(Number.isFinite)&&returned.every(Number.isFinite),
      'the actual head and all three axial regions load and return during the probe');
    assert.ok(headOnset<peakTime[0]&&headOnset<peakTime[2],'the actual head rotates before the peak loaded body and peduncle');
    for(let i=1;i<stations.length;i++){
      assert.ok(onset[i]>onset[i-1],`loading travels from anterior trunk toward the actual peduncle: ${onset.join(', ')}`);
      assert.ok(peakTime[i]>=peakTime[i-1],`the curvature pulse peaks in successive axial regions: ${peakTime}`);
      assert.ok(returned[i]>returned[i-1],'straightening travels posteriorly instead of releasing the whole fish at once');
    }
    assert.ok(peakTime[2]>peakTime[0],'the actual peduncle reaches its peak after the anterior trunk');
    assert.ok(maxAngle<=1.8+1e-8,'the actual added axial rotation respects the command bound');
    assert.ok(maxSkinStep<.04,'the added bend moves actual skin continuously without a regional snap');
    assert.ok(bendKeys.every(key=>Math.abs(swimmer.fish[key])<1e-8),'the finite stroke settles during a quiet glide');
    assert.ok(recruitmentKeys.every(key=>swimmer.fish[key]<.001),'regional carrier recruitment releases during a quiet glide');
    assert.ok(records.at(-1).angles.every(angle=>Math.abs(angle)<1e-8),'settled regional commands leave the actual body straight');

    // A frozen authored study must not borrow a previous live turn's history.
    const phasePose=sample=>{
      pose(asset,joints,rest,{...sample,bank:0,verticalGain:0,twistGain:0});
      return skinPoints();
    };
    const held={...phaseSample,poseHistory:false,stroke:1};
    const heldA=phasePose({...held,...withoutBend,...Object.fromEntries(recruitmentKeys.map(key=>[key,0]))});
    const heldB=phasePose({...held,...Object.fromEntries([...bendKeys,...issuedKeys].map(key=>[key,2.4])),
      ...Object.fromEntries(issuedVelocityKeys.map(key=>[key,12])),...Object.fromEntries(recruitmentKeys.map(key=>[key,1]))});
    assert.ok(heldA.every((point,i)=>point.distanceTo(heldB[i])<1e-10),
      'an authored held pose ignores stale live carrier-recruitment history');
    report.turns.push({sign,headOnset,onset,peakTime,returned,peaks,maxSkinStep});
  }

  // Measure the body and full caudal extent at the SAME actual pose. A root
  // rotation or a tail-only hook cannot substitute for a coherent trunk C.
  report.materialC=[];
  for(const urgent of [false,true])for(const sign of [-1,1]){
    const controller=createTurnController(),maximum=urgent?2.4:1.8;
    controller.request(sign*Math.PI,0,{urgent});
    const neutral={...isolated(create(()=>.5).fish),x:0,y:0,z:0,yaw:0,pitch:0,
      yawLag1:0,yawLag2:0,yawLag3:0,pitchLag1:0,pitchLag2:0,pitchLag3:0,
      bankLag1:0,bankLag2:0,bankLag3:0,verticalGain:0,twistGain:0,...withoutBend};
    pose(asset,joints,rest,neutral);
    const skull=joints[0].getWorldQuaternion(new Quaternion()).normalize();
    const lengths=joints.slice(1).map(b=>b.getWorldPosition(new Vector3()).distanceTo(b.parent.getWorldPosition(new Vector3())));
    let peak,maximumCommand=0;
    for(let tick=0;tick<=168;tick++){
      const gesture=controller.sample(tick/120),fish={...neutral,age:tick/120};
      issuedKeys.forEach((key,i)=>{fish[key]=gesture.b[i];fish[issuedVelocityKeys[i]]=gesture.v[i];});
      bendKeys.forEach((key,i)=>{fish[key]=gesture.b[Math.max(0,i-1)];});
      recruitmentKeys.forEach((key,i)=>{fish[key]=gesture.activity[Math.max(0,i-1)];});
      maximumCommand=Math.max(maximumCommand,...gesture.b.map(Math.abs));
      pose(asset,joints,rest,fish);
      assert.ok(skull.angleTo(joints[0].getWorldQuaternion(new Quaternion()).normalize())<1e-6,
        'normal and urgent C gestures keep the actual skull rigid');
      joints.slice(1).forEach((bone,i)=>assert.ok(Math.abs(bone.getWorldPosition(new Vector3())
        .distanceTo(bone.parent.getWorldPosition(new Vector3()))-lengths[i])<1e-6,
        'the full normal/urgent geometry envelope preserves every actual bone length'));
      const measured=shape.sample();
      if(!peak||measured.chordShortening>peak.shortening){
        const curve=measured.head.bodyCurve.map(point=>point.clone());
        const tangent=s=>{const i=Math.round(s*100),a=curve[Math.max(0,i-3)],b=curve[Math.min(100,i+2)];return Math.atan2(b.z-a.z,b.x-a.x);};
        const stations=[.30,.45,.60,.75,.90,.975].map(tangent).map(angle=>angle*sign);
        peak={age:tick/120,dOverL:measured.maximumDOverL,coherence:measured.coherence,
          shortening:measured.chordShortening,headTailAngle:Math.abs(measured.headTailAngle),stations};
      }
    }
    assert.ok(maximumCommand>maximum-.01&&maximumCommand<=maximum+1e-12,
      'the exported body is checked at the actual authored normal/urgent C amplitude');
    assert.ok(peak.dOverL>.44&&peak.coherence>.88&&peak.shortening>(urgent?.26:.15),
      `the same frame contains a substantial coherent C and real chord shortening: ${JSON.stringify(peak)}`);
    assert.ok(peak.headTailAngle>(urgent?2.0:1.45),'the material body participates in the turn rather than only the caudal fan');
    const [anterior,midA,midB,posterior,,tip]=peak.stations;
    assert.ok(midA>.12*tip&&midB>.32*tip&&(posterior-midA)>.30*(tip-anterior),
      'actual midtrunk secants share the bend; a posterior-only hook cannot satisfy the C diagnostic');
    report.materialC.push({urgent,sign,maximumCommand,...peak});
  }

  // Sample loading, the loaded stroke and recovery on both sides. A nascent
  // yaw lag is not the finite turn stroke: existing cruise beats may continue
  // before that region loads, then return as its finite bend relaxes.
  report.carrierPhase=[];let loadedPosteriorChecks=0;
  for(const sign of [-1,1]){
    const swimmer=create(()=>.5);
    Object.assign(swimmer.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0,stroke:1,muscle1:1,muscle2:1,muscle3:1});
    swimmer.navigate({x:.8,y:.8,z:tankCenterZ+sign*.2});
    const measure=sample=>{
      pose(asset,joints,rest,{...sample,x:0,y:0,z:0,yaw:0,pitch:0,bank:0,verticalGain:0,twistGain:0});
      return {sag:trunkSag(),tail:joints[32].getWorldQuaternion(new Quaternion())};
    };
    for(let frame=1;frame<=150;frame++){
      swimmer.step(1/120);
      if(![6,48,108,150].includes(frame))continue;
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
      if(fish.turnRecruitment3>=.25-1e-12){
        loadedPosteriorChecks++;
        assert.ok(low.tail>Math.abs(idealTail)*.5,
          'a fully replaced posterior turn cannot reverse with an independent cruise beat');
      }
      if(frame===6)assert.ok(high.tail-low.tail>.005,
        'a nascent posterior turn retains the preceding cruise beat before regional loading');
      if(frame===150)assert.ok(high.sag-low.sag>.003,
        'anterior carrier motion resumes as the finite stroke releases');
      report.carrierPhase.push({sign,age:fish.age,finiteSag,idealSag,idealTail,low,high});
    }
  }

  assert.ok(loadedPosteriorChecks>=4,'both turn sides actually exercise full posterior carrier replacement');

  // Join the rendered material curve to the following physical impulse. If a
  // loaded turn has replaced the cruise wave, changing that absent carrier
  // must not accelerate the same body differently. Ordinary strokes retain
  // both their visible wave and their phase-dependent forward effort.
  report.carrierThrust=[];
  for(const mode of ['cruise','loading','loaded','releasing']){
    // The peduncle interpolates trunk and posterior influence fields. Sample
    // anterior release before the trunk starts releasing at 1.0692 s.
    const frames={cruise:60,loading:6,loaded:48,releasing:127}[mode];
    const samples=[];
    for(let phase=0;phase<12;phase++){
      const swimmer=create(()=>.5);
      Object.assign(swimmer.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0,stroke:1,muscle1:1,muscle2:1,muscle3:1});
      swimmer.navigate(mode==='cruise'?{x:-.85,y:.8,z:tankCenterZ}:{x:.8,y:.8,z:tankCenterZ+.2});
      for(let frame=0;frame<frames;frame++)swimmer.step(1/120);
      const fish=swimmer.fish;
      if(mode==='releasing')assert.ok(fish.turnRecruitment1<.24&&fish.turnRecruitment2>=.25-1e-12&&fish.turnRecruitment3>=.25-1e-12,
        'the joined release fixture has anterior carrier return and fully replaced posterior material influences');
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
      assert.ok(tailRange>.005,'the posterior retains its preceding material beat while the anterior turn loads');
      assert.ok(accelerationRange>1e-6,'the not-yet-recruited posterior retains its causal carrier effort');
    }else {
      assert.ok(sagRange>.001&&nextSagRange>.001,'the anterior material wave returns during progressive release');
      assert.ok(tailRange<1e-7,'the posterior material remains recruited while the anterior carrier returns');
      assert.ok(accelerationRange<1e-7,'the posterior drive stays phase-independent until its own release');
    }
    report.carrierThrust.push({mode,sagRange,nextSagRange,tailRange,accelerationRange});
  }

  // Read the actual posterior skin and infer the propulsive contribution from
  // the following velocity step after accounting for the production drag.
  // These straight routes avoid the wall sculling force. Bounds describe this
  // authored motor/shape join; they are not measured carp force or EMG timings.
  const pitchedSwimmer=sign=>{
    const swimmer=create(()=>.5);
    Object.assign(swimmer.fish,{x:.65,y:sign>0?.58:1.13,z:tankCenterZ,yaw:0,pitch:0});
    swimmer.navigate({x:-.85,y:sign>0?1.17:.50,z:tankCenterZ});
    return swimmer;
  };
  const inferredThrust=(before,after)=>{
    const direction=new Vector3(-Math.cos(after.yaw)*Math.cos(after.pitch),Math.sin(after.pitch),Math.sin(after.yaw)*Math.cos(after.pitch));
    const acceleration=new Vector3(after.vx-before.vx,after.vy-before.vy,after.vz-before.vz).dot(direction)*120;
    const drag=(.38+.80*after.brake)*after.forwardSpeed+.95*after.forwardSpeed*Math.abs(after.forwardSpeed);
    return acceleration+drag;
  };
  const localTail=fish=>{
    pose(asset,joints,rest,{...fish,x:0,y:0,z:0,yaw:0,pitch:0,bank:0,verticalGain:0,twistGain:0});
    return skinPoints().at(-1);
  };
  const carrierExcursion=fish=>{
    const positions=Array.from({length:12},(_,i)=>localTail({...fish,phase:i*Math.PI/6}).z);
    return Math.max(...positions)-Math.min(...positions);
  };
  report.posteriorPropulsion=[];
  for(const sign of [-1,1]){
    const natural=pitchedSwimmer(sign),primed=pitchedSwimmer(sign);
    for(const swimmer of [natural,primed]){
      for(let frame=0;frame<12;frame++)swimmer.step(1/120);
      swimmer.fish.phase=4.8;
    }
    Object.assign(primed.fish,{muscle1:1,muscle2:1,muscle3:1});
    const excursion=carrierExcursion(natural.fish),primedExcursion=carrierExcursion(primed.fish);
    assert.ok(excursion<primedExcursion*.15,'natural stroke onset has less actual posterior material motion than an already loaded tail');
    const beforeNatural={...natural.fish},beforePrimed={...primed.fish};
    natural.step(1/120);primed.step(1/120);
    const onsetThrust=inferredThrust(beforeNatural,natural.fish),primedThrust=inferredThrust(beforePrimed,primed.fish);
    assert.ok(primedThrust>.01&&onsetThrust>=0&&onsetThrust<primedThrust*.05,
      'root propulsion waits for the observed posterior activation instead of the anterior stroke request alone');

    const swimmer=pitchedSwimmer(sign);
    let previousTail,previousSpeed=0,previousThrust=0,transition,quiet;
    for(let frame=0;frame<300;frame++){
      const before={...swimmer.fish},gait=swimmer.behavior.gait;
      swimmer.step(1/120);
      const fish=swimmer.fish,tail=localTail(fish),tailSpeed=previousTail?(tail.z-previousTail.z)*120:0;
      const thrust=inferredThrust(before,fish);
      if(gait==='stroke'&&swimmer.behavior.gait==='coast'){
        assert.ok(fish.pitch*sign>.15,'the finishing-stroke probe is actually climbing or descending');
        assert.ok(fish.muscle3>.9&&Math.abs(tailSpeed)>.1,'the actual posterior material is still finishing a substantial stroke');
        assert.ok(Math.abs(tailSpeed-previousSpeed)<.10,'the actual tail passes continuously into the coast request');
        assert.ok(previousThrust>.01&&thrust>previousThrust*.5&&Math.abs(thrust-previousThrust)<.04,
          'a coast request cannot cut off force in one tick while its actual posterior stroke continues');
        transition={age:fish.age,previousThrust,thrust,tailSpeed,previousTailSpeed:previousSpeed};
      }
      if(frame===299){
        assert.equal(swimmer.behavior.gait,'coast','the quiet endpoint is inside the same uninterrupted coast');
        assert.ok(Math.abs(tailSpeed)<.001&&Math.abs(thrust)<1e-6,
          'the actual settling tail and its propulsive effort both become quiet during the glide');
        quiet={thrust,tailSpeed};
      }
      previousTail=tail;previousSpeed=tailSpeed;previousThrust=thrust;
    }
    assert.ok(transition,'the real pitched route contains a stroke-to-coast transition');
    report.posteriorPropulsion.push({sign,excursion,primedExcursion,onsetThrust,primedThrust,transition,quiet});
  }

  // Preparation retains the issued turn's carrier suppression while the
  // actual body is quiet. Its replacement mean thrust must be quiet too.
  report.quietTurnPropulsion=[];
  for(let phase=0;phase<6;phase++){
    const swimmer=create(()=>.5);
    Object.assign(swimmer.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
    swimmer.flick({x:-.3,y:.8,z:tankCenterZ+.0001});
    for(let tick=0;tick<30;tick++)swimmer.step(1/120);
    const fish=swimmer.fish;
    assert.ok(fish.turnPreparing&&issuedKeys.every(key=>fish[key]===0)&&issuedVelocityKeys.every(key=>fish[key]===0),
      'the propulsion probe is inside actual quiet urgent preparation');
    assert.ok(fish.turnRecruitment3>=.25-1e-12&&fish.turnPower===0,
      'a recruited but stationary issued body has no replacement propulsive power');
    fish.phase=phase*Math.PI/3;
    const before={...fish};swimmer.step(1/120);
    const thrust=inferredThrust(before,swimmer.fish);
    assert.ok(Math.abs(thrust)<1e-7,'an invisible cruise beat or stored turn load cannot propel the quiet preparation');
    report.quietTurnPropulsion.push(thrust);
  }

  // Arrive at the current location, settle, and let the real intent chooser
  // decide to travel again. A cruise cannot inherit the hover's remaining
  // coast and report movement intent while its actual tail stays inactive.
  const traveler=create(()=>.5);
  Object.assign(traveler.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
  traveler.navigate({...traveler.fish});
  let travelOnset;
  for(let frame=0;frame<6*120;frame++){
    const before=traveler.behavior.intent;
    traveler.step(1/120);
    if(before==='hover'&&traveler.behavior.intent==='cruise'){travelOnset={...traveler.fish};break;}
  }
  assert.ok(travelOnset&&travelOnset.muscle3<.01,'the actual intent chooser selects travel from a settled hover');
  for(let frame=0;frame<90;frame++)traveler.step(1/120);
  const departure=traveler.fish;
  const departureExcursion=carrierExcursion(departure);
  const departureDistance=Math.hypot(departure.x-travelOnset.x,departure.y-travelOnset.y,departure.z-travelOnset.z);
  assert.ok(departureExcursion>.10,'a new travel intent loads a real posterior stroke without inheriting a quiet gait timer');
  assert.ok(departureDistance>.015&&departure.speed>.05,'the newly chosen cruise actually propels the fish after its causal posterior loading');
  report.cruiseDeparture={age:travelOnset.age,departureExcursion,departureDistance,speed:departure.speed};

  const tiny=create(()=>.5);
  Object.assign(tiny.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
  tiny.navigate({x:-.9*Math.cos(.2),y:.8,z:tankCenterZ+.9*Math.sin(.2)});
  let tinyPeak=0,tinyRecruitment=0;
  for(let frame=0;frame<180;frame++){
    tiny.step(1/120);pose(asset,joints,rest,isolated(tiny.fish));
    bendKeys.forEach(key=>{tinyPeak=Math.max(tinyPeak,Math.abs(tiny.fish[key]));});
    recruitmentKeys.forEach(key=>{tinyRecruitment=Math.max(tinyRecruitment,tiny.fish[key]);});
  }
  assert.ok(tinyPeak<.005,'a small course correction does not recruit the large-turn C bend');
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
  assert.ok(escapePeak>.15&&escapePeak<=2.4,'a flick initializes a bounded escape stroke during its first bout');
  report.escapePeak=escapePeak;

  report.repeatedFlicks=[];
  const flickToward=(swimmer,heading)=>swimmer.flick({x:swimmer.fish.x+.2*Math.cos(heading),
    y:swimmer.fish.y,z:swimmer.fish.z-.2*Math.sin(heading)});
  for(const firstSign of [-1,1])for(const relativeSign of [-1,1]){
    const repeated=create(()=>.5),uninterrupted=create(()=>.5);
    for(const swimmer of [repeated,uninterrupted]){
      Object.assign(swimmer.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
      flickToward(swimmer,firstSign*Math.PI/2);
      for(let frame=0;frame<68;frame++)swimmer.step(1/120);
    }
    const before=repeated.fish.turnBend;
    assert.ok(Math.abs(before)>.15,'the repeated-flick probe interrupts an already loaded turn');
    pose(asset,joints,rest,isolated(repeated.fish));
    const beforeSkin=skinPoints(),beforeIssued=issuedKeys.map(key=>repeated.fish[key]);
    flickToward(repeated,firstSign*relativeSign*Math.PI/2);
    assert.deepEqual(issuedKeys.map(key=>repeated.fish[key]),beforeIssued,
      'a new flick does not rewrite the already issued regional material pose');
    repeated.step(1/120);uninterrupted.step(1/120);
    const commandJump=Math.max(...issuedKeys.map(key=>Math.abs(repeated.fish[key]-uninterrupted.fish[key])));
    // C2 handoff differs from the ongoing trajectory only at cubic order.
    // This bound uses the complete +/-2.4 rad excursion, not a small-C cap.
    const maximumEntryDifference=4.8*10*((1/120)/.42)**3;
    assert.ok(commandJump<maximumEntryDifference+1e-6,
      'a repeated flick joins the current trajectory instead of replacing its value or velocity');
    pose(asset,joints,rest,isolated(repeated.fish));
    const repeatedSkin=skinPoints();
    pose(asset,joints,rest,isolated(uninterrupted.fish));
    const uninterruptedSkin=skinPoints();
    const skinDifference=Math.max(...repeatedSkin.map((p,i)=>p.distanceTo(uninterruptedSkin[i])));
    assert.ok(skinDifference<.001,'a restarted turn does not pull actual skin abruptly away from its continuing trajectory');
    const continuingStep=Math.max(...uninterruptedSkin.map((p,i)=>p.distanceTo(beforeSkin[i])));
    const repeatedStep=Math.max(...repeatedSkin.map((p,i)=>p.distanceTo(beforeSkin[i])));
    assert.ok(repeatedStep<continuingStep+.001,'repeated-flick skin motion remains continuous with the established stroke');
    let newSideReached=false;
    for(let frame=1;frame<120;frame++){
      repeated.step(1/120);
      if(repeated.fish.turnAnterior*firstSign*relativeSign>.15)newSideReached=true;
      assert.ok([...bendKeys,...issuedKeys].every(key=>Math.abs(repeated.fish[key])<=2.4+1e-12),
        'same-side and opposite-side restarts stay inside the authored normal/urgent geometry envelope');
    }
    assert.ok(newSideReached,'the preserved command progresses smoothly to the newly selected turn side after fin preparation');
    report.repeatedFlicks.push({firstSign,relativeSign,commandJump,skinDifference});
  }

  // During recovery and rapid retriggers, the full carrier-to-turn transition
  // must also remain continuous, rather than only its isolated C-bend channel.
  const transitionRates=[120,240,480];
  const measureTransitions=poseField=>transitionRates.map(rate=>{
    const transitions=create(()=>.5);
    Object.assign(transitions.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0});
    let priorFullSkin,maximumFullStep=0,peak;
    for(let frame=0;frame<4*rate;frame++){
      if([0,.2*rate,.4*rate].includes(frame))flickToward(transitions,frame===.2*rate?Math.PI/2:-Math.PI/2);
      if(frame===1.5*rate)transitions.navigate({...transitions.fish});
      transitions.step(1/rate);poseField(asset,joints,rest,transitions.fish);
      const current=skinPoints();
      if(priorFullSkin)current.forEach((point,i)=>{
        const step=point.distanceTo(priorFullSkin[i]);
        if(step>maximumFullStep){maximumFullStep=step;peak={age:transitions.fish.age,probe:i};}
      });
      priorFullSkin=current;
    }
    return {rate,step:maximumFullStep,speed:maximumFullStep*rate,...peak};
  });
  const assertTransitions=measurements=>{
    // This is an authored stress guard, not a calibrated biological speed
    // limit. A rapid opposite-C handoff can legitimately exceed the old
    // 60 mm/120 Hz threshold while its material speed converges near 7.1 m/s.
    // Enforce the fixed speed ceiling at all three physical step sizes, then
    // require BOTH refinements to reduce displacement. A pose jump cannot
    // acquire a pass merely because its first pair happens to look smaller.
    assert.ok(measurements.every(row=>row.speed<7.5),
      `actual skin remains inside the authored 7.5 m/s stress envelope: ${JSON.stringify(measurements)}`);
    for(let i=1;i<measurements.length;i++)assert.ok(measurements[i].step<measurements[i-1].step*.60,
      `actual skin displacement converges through carrier release and urgent reversal: ${JSON.stringify(measurements)}`);
  };
  const transitionMeasurements=measureTransitions(pose),transitionSteps=transitionMeasurements.map(row=>row.step);
  assertTransitions(transitionMeasurements);
  report.maximumFullTransitionStep=transitionSteps;
  report.fullTransitionMeasurements=transitionMeasurements;
  // Exercise the real rig with discontinuous counterfactuals. The first
  // restores a regional pose snap; the second abruptly reveals a cruise wave
  // instead of releasing observed recruitment over its finite C2 envelope.
  const transitionMutants={
    regionalPoseJump:(a,j,r,fish)=>pose(a,j,r,{...fish,turnPosterior:fish.turnPosterior+(fish.age>=1.7?.3:0)}),
    instantaneousCarrierUnlock:(a,j,r,fish)=>{
      // Historical regression: an instantaneous squared shape/rate envelope
      // exposes a strong unrelated carrier while opposing C fields cancel.
      const activity=issuedKeys.map((key,i)=>{
        const t=Math.max(0,Math.min(1,(fish[key]**2+.01*fish[issuedVelocityKeys[i]]**2)/.09));
        return t*t*t*(10+t*(-15+6*t));
      });
      return pose(a,j,r,{...fish,turnRecruitment:activity[0],turnRecruitment1:activity[0],turnRecruitment2:activity[1],turnRecruitment3:activity[2]});
    }
  };
  for(const [name,mutant] of Object.entries(transitionMutants))assert.throws(()=>assertTransitions(measureTransitions(mutant)),undefined,
    `${name} must fail the actual material continuity guard`);

  // Observe every production controller AFTER each actual physics tick, then
  // pose skull/axial/support bones BEFORE intrinsic fins and posed medians.
  // Keeping all histories alive catches a folded display driven by stale cup.
  const containment=createContainment(asset);
  const controllers=()=>({paired:Object.fromEntries(['pectoral','pelvic'].map(kind=>[kind,
    Object.fromEntries([-1,1].map(side=>[side,createFinController(side,kind==='pectoral')]))])),
    caudal:createCaudalController(),median:Object.fromEntries(['dorsal','anal'].map(kind=>[kind,createMedianController(kind)]))});
  const sampleAll=(control,fish)=>({paired:Object.fromEntries(Object.entries(control.paired).map(([kind,sides])=>[kind,
    Object.fromEntries(Object.entries(sides).map(([side,controller])=>[side,controller.sample(fish,'turn route')]))])),
    caudal:control.caudal.sample(fish,'turn route'),median:Object.fromEntries(Object.entries(control.median).map(([kind,controller])=>[kind,controller.sample(fish,'turn route')]))});
  const render=(fish,motions)=>{
    pose(asset,joints,rest,fish);face.pose(fish);supports.pose();
    for(const fin of fins){
      const output=fin.mesh.geometry.attributes.position.array;
      if(fin.paired)deformFinGeometry(fin.binding,motions.paired[fin.pectoral?'pectoral':'pelvic'],output);
      else deformCaudalGeometry(fin.binding,motions.caudal,output);
    }
    for(const fin of median)deformMedianFin(fin,motions.median[fin.kind]);
    const correction=containment.correction(),length=Math.hypot(correction.x,correction.y,correction.z);
    report.maximumCorrection=Math.max(report.maximumCorrection,length);
    return {correction,length};
  };
  for(const seed of [913,481,33,123,456,789]){
    const swimmer=create(seeded(seed)),control=controllers();
    swimmer.footprint(footprint.bounds);
    let activeTurns=0,observed=0,motions=sampleAll(control,swimmer.fish);
    const observe=fish=>{observed++;motions=sampleAll(control,fish);};
    for(let frame=0;frame<120*120;frame++){
      swimmer.step(1/120,observe);const fish=swimmer.fish;
      assert.equal(observed,frame+1,'all seven fin histories observe each real 120 Hz tick exactly once');
      if(frame%24)continue;
      if(Math.abs(fish.turnBend)>.05)activeTurns++;
      const {correction,length}=render(fish,motions);
      assert.ok(length<.001,`the actual bending body, supports and all deformed fins clear the tank for routine seed ${seed}: ${length}`);
      swimmer.contain(correction);
    }
    assert.ok(activeTurns>10,'each long routine probe includes multiple substantial finite turn strokes');
  }

  const studyHarness=withGeometry=>{
    const random=createStudyRandom(913),swimmer=create(random);
    swimmer.footprint(footprint.bounds);
    let control,motions,observations=0,digest,largestBend=0,turnOnset,peakAge=0,maximum=0;
    const study=createTurnStudy(swimmer,{random,onReset:fish=>{
      control=controllers();motions=sampleAll(control,fish);observations=0;digest=createHash('sha256');
      largestBend=maximum=0;turnOnset=undefined;peakAge=0;
    },onTick:(fish,tick)=>{
      observations++;assert.equal(tick,observations,'the study issues exactly one full fin observation per fixed physics tick');
      assert.ok(Math.abs(fish.age-tick/120)<1e-9,'fin observation age is the actual 120 Hz physics age');
      motions=sampleAll(control,fish);digest.update(JSON.stringify(fish));digest.update(JSON.stringify(motions));
      if(fish.age>2&&turnOnset===undefined&&Math.abs(fish.yaw)>.05)turnOnset=fish.age;
      if(fish.turnIntensity>largestBend){largestBend=fish.turnIntensity;peakAge=fish.age;}
      if(withGeometry&&tick%12===0){
        const {length}=render(fish,motions);maximum=Math.max(maximum,length);
        assert.ok(length<1e-8,`the warm two-second U turn needs no render-timed correction: tick${tick}, ${length}`);
      }
    }});
    return {study,swimmer,get observations(){return observations;},get digest(){return digest.copy().digest('hex');},
      get motions(){return motions;},get report(){return {largestBend,peakAge,turnOnset,maximum};}};
  };
  const advance=(test,cadence,seconds)=>{
    let time=0,frame=0;while(time<seconds-1e-10){const dt=Math.min(cadence[frame++%cadence.length],seconds-time);test.study.step(dt);time+=dt;}
  };
  const warm=studyHarness(true);warm.study.reset();advance(warm,[1/60],8);
  assert.equal(warm.observations,960,'the warmed swimming turn observes all 960 physics ticks');
  assert.ok(warm.report.largestBend>1.5&&warm.report.turnOnset<warm.report.peakAge,
    'the warmed real swimmer rotates its head before the full loaded C peaks');
  const digest=warm.digest,final=structuredClone(warm.swimmer.fish),finalFins=structuredClone(warm.motions);
  const beforePause=warm.observations;for(const dt of [0,-1,NaN,Infinity])warm.study.step(dt);
  assert.equal(warm.observations,beforePause,'paused/invalid elapsed time issues no physics or fin-history observations');
  for(const cadence of [[1/30],[1/144],[.011,.027,.004,.053,.008,.019]]){
    const test=studyHarness(false);test.study.reset();advance(test,cadence,8);
    assert.equal(test.digest,digest,'all issued turn states and complete fin histories match across display cadences');
    assert.deepEqual(test.swimmer.fish,final,'display cadence cannot change the warmed turn trajectory');
    assert.deepEqual(test.motions,finalFins,'display cadence cannot change the actual delayed fin field');
  }
  warm.study.reset();advance(warm,[1/30],8);
  assert.equal(warm.digest,digest,'restart clears both turn lifecycle and every delayed fin history');
  report.warmTurn=warm.report;
  return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const report=await assertTurnBend();
  console.log(`Finite turn bend passed: ${JSON.stringify({normal:report.materialC.find(row=>!row.urgent),
    urgent:report.materialC.find(row=>row.urgent),maximumCorrection:report.maximumCorrection,
    transitionSteps:report.maximumFullTransitionStep,transitionMeasurements:report.fullTransitionMeasurements,warmTurn:report.warmTurn,
    maximumQuietThrust:Math.max(...report.quietTurnPropulsion.map(Math.abs))})}`);
}
