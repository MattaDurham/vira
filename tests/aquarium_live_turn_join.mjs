/* Actual-asset opt-in joins. Legacy defaults keep their own existing tests.
 * COM is independently recaptured from material markers in real bone matrices;
 * bounds inspect the uploaded Float32 skin palette, never a helper's centroid. */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve} from 'node:path';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Box3,Euler,Group,Matrix4,Quaternion,Texture,Vector3} from '../static/vendor/three.webgpu.js';
import {createAxialFrame} from '../static/aquarium-axial-frame.js';
import {createMassPlacement} from '../static/aquarium-mass-placement.js';
import {applySwimPose,createFacialRig} from '../static/aquarium-rig.js';
import {createKoiInstance} from '../static/aquarium-fish-instance.js';
import {createManeuverFootprint} from '../static/aquarium-footprint.js';
import {neutralMechanicsFish} from '../static/aquarium-mechanics-study.js';
import {createSourceSteeringController} from '../static/aquarium-source-steering.js';
import {createSwimmer} from '../static/aquarium-motion.js';
import {createStudyRandom} from '../static/aquarium-study.js';
import {waterMin,waterMax,tankCenterZ} from '../static/aquarium-space.js';

// Optional paired paths reuse exactly the same oracle for a separately built
// visual variant. The original asset remains the default physical baseline.
// Usage: node tests/aquarium_live_turn_join.mjs [assetPath profilePath]
const suppliedPaths=process.argv.slice(2);
if(suppliedPaths.length!==0&&suppliedPaths.length!==2)
  throw new Error('Usage: node tests/aquarium_live_turn_join.mjs [assetPath profilePath]');
const assetPath=suppliedPaths.length?resolve(suppliedPaths[0]):new URL('../static/aquarium/koi.glb',import.meta.url),
  profilePath=suppliedPaths.length?resolve(suppliedPaths[1]):new URL('../static/aquarium/body-mass.json',import.meta.url);
const bytes=await readFile(assetPath),profile=JSON.parse(await readFile(profilePath,'utf8')),
  assetSHA256=createHash('sha256').update(bytes).digest('hex');
assert.equal(profile.assetSHA256,assetSHA256,'the actual tested asset must match its selected mass profile');
const sourceData=JSON.parse(await readFile(new URL('../static/aquarium/reference-turn.json',import.meta.url),'utf8'));
const parse=async()=> (await new GLTFLoader().register(()=>({name:'LiveTurnJoinGeometry',loadTexture:()=>Promise.resolve(new Texture())}))
  .parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;
const totalMass=profile.bodies29.reduce((sum,r)=>sum+r.mass,0),neutralCOM=profile.bodies29.reduce((sum,r)=>sum.addScaledVector(new Vector3(...r.com),r.mass),new Vector3()).divideScalar(totalMass);
const F=new Vector3(.12,.84,tankCenterZ),rootQ=fish=>new Quaternion().setFromEuler(new Euler(0,fish.yaw,-fish.pitch,'YXZ')).multiply(new Quaternion().setFromAxisAngle(new Vector3(1,0,0),fish.bank??0));
const poseFields=[
  {name:'neutral',yaw:0,pitch:0,bank:0},
  {name:'deepC',yaw:.7,pitch:.23,bank:-.1,turnAnterior:2.3,turnTrunk:2.3,turnPosterior:2.3},
  {name:'recovery',yaw:-.9,pitch:-.27,bank:.12,turnAnterior:2.1,turnTrunk:.7,turnPosterior:-.5},
  {name:'verticalTwist',yaw:1.1,pitch:.31,bank:-.11,turnAnterior:-1.8,turnTrunk:-1.3,turnPosterior:-.4,
    pitchLag1:-.1,pitchLag2:-.2,pitchLag3:-.3,torsionDrive:.21},
  {name:'opposing',yaw:-1.2,pitch:-.28,bank:.12,turnAnterior:-2.2,turnTrunk:-1.0,turnPosterior:.6},
];
const fishPose=(shape,age=1)=>({...neutralMechanicsFish(),x:F.x,y:F.y,z:F.z,age,...shape,
  pectoralPaddlePlus:.085,pectoralPaddleMinus:-.024,pectoralForeAftPlus:.06,pectoralForeAftMinus:-.06});
function geometryHash(asset){const hash=createHash('sha256');asset.traverse(mesh=>{if(!mesh.isMesh)return;for(const name of Object.keys(mesh.geometry.attributes).sort()){const a=mesh.geometry.attributes[name].array;hash.update(mesh.name+' '+name);hash.update(new Uint8Array(a.buffer,a.byteOffset,a.byteLength));}});return hash.digest('hex');}
function materialMarkers(pristine){
  pristine.updateMatrixWorld(true);
  return profile.bodies29.map(region=>{const name=`spine_${String(region.regions[0]).padStart(2,'0')}`,bone=pristine.getObjectByName(name);
    return {name,mass:region.mass,point:new Vector3(...region.com).applyMatrix4(pristine.matrixWorld).applyMatrix4(bone.matrixWorld.clone().invert())};});
}
function actualCOM(model,markers){
  model.asset.updateMatrixWorld(true);const value=new Vector3();
  for(const marker of markers)value.addScaledVector(marker.point.clone().applyMatrix4(model.asset.getObjectByName(marker.name).matrixWorld),marker.mass);
  return value.divideScalar(totalMass);
}
function assertCOM(model,fish,markers,report){
  const expected=neutralCOM.clone().multiplyScalar(model.asset.scale.x).applyQuaternion(rootQ(fish)).add(new Vector3(fish.x,fish.y,fish.z));
  if(model.asset.parent){model.asset.parent.updateMatrixWorld(true);expected.applyMatrix4(model.asset.parent.matrixWorld);}
  const error=actualCOM(model,markers).distanceTo(expected),scale=model.asset.getWorldScale(new Vector3()).x;
  if(report)report.maximumCOMError=Math.max(report.maximumCOMError,error);
  assert.ok(error<7e-7*Math.max(1,scale),`actual region COM preserves parent*[F+sQ*c0], including all three root axes: ${error}`);
}
function uploadedBounds(model,limit,report,{reference}={}){
  const meshes=[];model.asset.traverse(mesh=>{if(mesh.isSkinnedMesh)meshes.push(mesh);});
  for(const skeleton of new Set(meshes.map(mesh=>mesh.skeleton)))skeleton.update();
  const bind=new Vector3(),point=new Vector3(),post=new Matrix4(),actual=new Box3();let maximumOutside=0;
  for(const mesh of meshes){const a=mesh.geometry.attributes,palette=mesh.skeleton.boneMatrices;post.multiplyMatrices(mesh.matrixWorld,mesh.bindMatrixInverse);
    for(let i=0;i<a.position.count;i++){
      bind.fromBufferAttribute(a.position,i).applyMatrix4(mesh.bindMatrix);point.set(0,0,0);
      for(let slot=0;slot<4;slot++){const weight=a.skinWeight.getComponent(i,slot);if(!weight)continue;const e=a.skinIndex.getComponent(i,slot)*16;
        point.x+=weight*(palette[e]*bind.x+palette[e+4]*bind.y+palette[e+8]*bind.z+palette[e+12]);
        point.y+=weight*(palette[e+1]*bind.x+palette[e+5]*bind.y+palette[e+9]*bind.z+palette[e+13]);
        point.z+=weight*(palette[e+2]*bind.x+palette[e+6]*bind.y+palette[e+10]*bind.z+palette[e+14]);}
      point.applyMatrix4(post);assert.ok(Number.isFinite(point.x)&&Number.isFinite(point.y)&&Number.isFinite(point.z),'every uploaded actual skin/fin/ray/scale point is finite');actual.expandByPoint(point);
      if(limit)maximumOutside=Math.max(maximumOutside,limit.min.x-point.x,point.x-limit.max.x,limit.min.y-point.y,point.y-limit.max.y,limit.min.z-point.z,point.z-limit.max.z,0);
      if(report){report.vertices++;report.meshes.add(mesh.name);}
      if(report&&reference){const dx=point.x-reference.x,dy=point.y-reference.y,dz=point.z-reference.z;report.maximumReferenceRadius=Math.max(report.maximumReferenceRadius??0,Math.hypot(dx,dy,dz));report.maximumHorizontalRadius=Math.max(report.maximumHorizontalRadius??0,Math.hypot(dx,dz));report.maximumVerticalRadius=Math.max(report.maximumVerticalRadius??0,Math.abs(dy));}
    }
  }
  if(limit)assert.ok(maximumOutside<2e-6*model.asset.scale.x,`independently uploaded complete material stays inside supplied bounds: ${maximumOutside}`);
  if(report)report.maximumOutside=Math.max(report.maximumOutside,maximumOutside);return actual;
}
async function primary(scale){
  const asset=await parse(),frame=createAxialFrame(asset),placement=createMassPlacement({...frame,profile}),rest=frame.joints.map(b=>b.quaternion.clone()),face=createFacialRig(asset);asset.scale.setScalar(scale);
  return {asset,joints:frame.joints,pose(fish){applySwimPose(asset,frame.joints,rest,fish,{placement});face.pose(fish);}};
}
export async function assertLiveMassJoin(){
  const source=await parse(),markers=materialMarkers(source),before=geometryHash(source),report={poses:0,vertices:0,meshes:new Set(),maximumCOMError:0,maximumOutside:0,maximumPrimaryFactoryBoneError:0,maximumScaleError:0,maximumSourceScaleError:0};
  const models=[createKoiInstance(source,{massProfile:profile,scale:1,random:()=>.5}),createKoiInstance(source,{massProfile:profile,scale:.43,random:()=>.5})];
  source.scale.setScalar(.7);const equivalent=createKoiInstance(source,{massProfile:profile,scale:.43/.7,random:()=>.5});source.scale.setScalar(1);source.updateMatrixWorld(true);
  const mains=[await primary(1),await primary(.43)],footprints=[];
  for(const scale of [1,.43]){source.scale.setScalar(scale);source.updateMatrixWorld(true);footprints.push(createManeuverFootprint(source,{massProfile:profile}));}
  source.scale.setScalar(1);source.updateMatrixWorld(true);
  let age=0;
  for(const shape of poseFields){
    const fish=fishPose(shape,age+=.11);
    for(const model of [...models,equivalent,...mains]){model.pose(fish);model.updateVisual?.(fish);assertCOM(model,fish,markers,report);}
    for(let j=0;j<34;j++){
      const p=models[0].joints[j].getWorldPosition(new Vector3()),small=models[1].joints[j].getWorldPosition(new Vector3()),other=equivalent.joints[j].getWorldPosition(new Vector3());
      report.maximumScaleError=Math.max(report.maximumScaleError,small.distanceTo(p.sub(F).multiplyScalar(.43).add(F)));
      report.maximumSourceScaleError=Math.max(report.maximumSourceScaleError,small.distanceTo(other));
      for(let k=0;k<2;k++)report.maximumPrimaryFactoryBoneError=Math.max(report.maximumPrimaryFactoryBoneError,mains[k].joints[j].getWorldPosition(new Vector3()).distanceTo(models[k].joints[j].getWorldPosition(new Vector3())));
    }
    for(let i=0;i<2;i++){uploadedBounds(models[i],footprints[i].bounds(fish),report);report.poses++;}
  }
  assert.ok(report.maximumScaleError<2e-7&&report.maximumSourceScaleError<2e-7,'physical source scale and instance scale enter root placement exactly once');
  assert.ok(report.maximumPrimaryFactoryBoneError<2e-7,'raw main-like applySwimPose and companion factory share the opt-in mass contract');
  const parent=new Group();parent.position.set(.2,-.1,.15);parent.rotation.set(.18,.5,-.09);parent.scale.setScalar(1.7);parent.add(models[1].asset);
  const mixed=fishPose(poseFields[3],1.5);models[1].pose(mixed);models[1].updateVisual(mixed);assertCOM(models[1],mixed,markers,report);models[1].asset.removeFromParent();

  // Mutations act on the actual rendered root, leaving the independent mass
  // markers and declared navigation anchor intact. They never edit production.
  const model=models[1],fixture=fishPose(poseFields[3],1.6);model.pose(fixture);model.updateVisual(fixture);const correct=model.asset.position.clone(),origin=new Vector3(fixture.x,fixture.y,fixture.z);
  assert.ok(correct.distanceTo(origin)>.005,'nonneutral material placement is substantial enough to expose centering faults');
  model.asset.position.copy(origin);assert.throws(()=>assertCOM(model,fixture,markers),assert.AssertionError,'neutral-centroid substitution must fail the actual-matrix COM oracle');
  model.asset.position.copy(correct).sub(origin).multiplyScalar(.43).add(origin);assert.throws(()=>assertCOM(model,fixture,markers),assert.AssertionError,'retaining the old companion scale wrapper must fail');
  const yawOnly=new Quaternion().setFromAxisAngle(new Vector3(0,1,0),fixture.yaw),wrong=correct.clone().sub(origin).applyQuaternion(rootQ(fixture).invert()).applyQuaternion(yawOnly).add(origin);
  model.asset.position.copy(wrong);assert.throws(()=>assertCOM(model,fixture,markers),assert.AssertionError,'yaw-only correction cannot stand in for three-axis placement');model.asset.position.copy(correct);model.asset.updateMatrixWorld(true);
  const wrongBounds=footprints[1].bounds(fixture).clone().translate(new Vector3(.8,0,0));assert.throws(()=>uploadedBounds(model,wrongBounds),assert.AssertionError,'all-uploaded-vertex coverage oracle must reject shifted material bounds');
  const forecastParent=new Group();forecastParent.position.set(.3,0,.1);forecastParent.rotation.y=.4;forecastParent.add(source);forecastParent.updateMatrixWorld(true);
  assert.throws(()=>createManeuverFootprint(source,{massProfile:profile}),/requires an identity parent/,'opt-in navigation rejects a transformed parent rather than returning local bounds as world bounds');
  assert.throws(()=>footprints[0].bounds(fixture),/requires an identity parent/,'a forecast captured before later reparenting must also reject the unsupported frame');source.removeFromParent();source.updateMatrixWorld(true);
  assert.equal(geometryHash(source),before,'optional clone/forecast/placement construction does not deform source material buffers');
  for(const name of ['Koi_skin','Pectoral_branched_rays','Pelvic_branched_rays','Caudal_branched_rays','Dorsal_branched_rays','Anal_branched_rays'])assert.ok(report.meshes.has(name),`${name} participates in independent complete-material coverage`);
  assert.ok([...report.meshes].some(name=>name.startsWith('Overlapping_scales')),'scale relief participates in independent complete-material coverage');
  report.meshCount=report.meshes.size;delete report.meshes;for(const model of [...models,equivalent])model.dispose();return report;
}

const wrap=a=>Math.atan2(Math.sin(a),Math.cos(a));
function observedFactory(store,data=sourceData){return()=>{
  store.factories=(store.factories??0)+1;const controller=createSourceSteeringController(data),commit=controller.commit;
  controller.commit=proposal=>{store.commits=(store.commits??0)+1;store.packet=commit(proposal);return store.packet;};store.controller=controller;return controller;
};}
function skinPoint(mesh,index,out=new Vector3()){
  const a=mesh.geometry.attributes,bind=new Vector3().fromBufferAttribute(a.position,index).applyMatrix4(mesh.bindMatrix),p=mesh.skeleton.boneMatrices;out.set(0,0,0);
  for(let slot=0;slot<4;slot++){const weight=a.skinWeight.getComponent(index,slot);if(!weight)continue;const e=a.skinIndex.getComponent(index,slot)*16;
    out.x+=weight*(p[e]*bind.x+p[e+4]*bind.y+p[e+8]*bind.z+p[e+12]);out.y+=weight*(p[e+1]*bind.x+p[e+5]*bind.y+p[e+9]*bind.z+p[e+13]);out.z+=weight*(p[e+2]*bind.x+p[e+6]*bind.y+p[e+10]*bind.z+p[e+14]);}
  return out.applyMatrix4(new Matrix4().multiplyMatrices(mesh.matrixWorld,mesh.bindMatrixInverse));
}
// Reconstruct intended source-phase bones independently of the live rendered
// hierarchy. The pristine axial sampler supplies relative shape only; head Q,
// mass-centred root and exported material palette are assembled here. Actual
// skin points then test the controller→navigation→pose→GPU-palette join.
function sourceSkinError(model,fish,packet,native,indices){
  const expectedFish={...fish,yaw:packet.headHeading,turnAnterior:packet.b[0],turnTrunk:packet.b[1],turnPosterior:packet.b[2],turnSourceWeight:packet.sourceReplacementWeight,
    turnRecruitment:packet.activity[0],turnRecruitment1:packet.activity[0],turnRecruitment2:packet.activity[1],turnRecruitment3:packet.activity[2]};
  if(packet.sourceReplacementWeight>1-1e-10)Object.assign(expectedFish,{yawLag1:0,yawLag2:0,yawLag3:0});
  const frame=native.capture(expectedFish,native.createFrame()),Q=rootQ(expectedFish),scale=model.asset.scale.x,centroid=new Vector3();
  for(const r of profile.bodies29){const panel=r.regions[0],R=frame.rotations[panel].clone().multiply(native.rest[panel].clone().invert());centroid.addScaledVector(new Vector3(...r.com).sub(native.centers[panel]).applyQuaternion(R).add(frame.positions[panel]),r.mass);}centroid.divideScalar(totalMass);
  const origin=neutralCOM.clone().sub(centroid).multiplyScalar(scale).applyQuaternion(Q).add(new Vector3(fish.x,fish.y,fish.z)),matrices=frame.positions.map((p,i)=>new Matrix4().compose(p.clone().multiplyScalar(scale).applyQuaternion(Q).add(origin),Q.clone().multiply(frame.rotations[i]),new Vector3(scale,scale,scale)));
  const mesh=model.asset.getObjectByName('Koi_skin'),a=mesh.geometry.attributes,bind=new Vector3(),expected=new Vector3(),point=new Vector3();mesh.skeleton.update();let maximum=0,headMaximum=0;
  // Keep independently animated face joints in their observed local cranial
  // frames. Their ancestor's heading/translation still comes only from the
  // source packet and the independently assembled material-centred root.
  const materialBones=mesh.skeleton.bones.map(bone=>{
    let axial=bone;while(axial&&!axial.name.startsWith('spine_'))axial=axial.parent;
    assert.ok(axial,'skin joints attach to a known axial frame');const panel=Number(axial.name.slice(6));assert.ok(Number.isInteger(panel));
    return matrices[panel].clone().multiply(axial.matrixWorld.clone().invert().multiply(bone.matrixWorld));
  });
  for(const index of indices){bind.fromBufferAttribute(a.position,index).applyMatrix4(mesh.bindMatrix);expected.set(0,0,0);
    for(let slot=0;slot<4;slot++){const weight=a.skinWeight.getComponent(index,slot);if(!weight)continue;const id=a.skinIndex.getComponent(index,slot);expected.addScaledVector(point.copy(bind).applyMatrix4(mesh.skeleton.boneInverses[id]).applyMatrix4(materialBones[id]),weight);}
    const error=expected.distanceTo(skinPoint(mesh,index));maximum=Math.max(maximum,error);if(a.position.getX(index)<-.45)headMaximum=Math.max(headMaximum,error);
  }
  return {maximum,headMaximum};
}
function compareFish(actual,expected){for(const [key,value] of Object.entries(actual)){if(typeof value==='number')assert.ok(Number.isFinite(value)&&Math.abs(value-expected[key])<2e-10,`main/factory source navigation agrees for ${key}`);else assert.equal(value,expected[key],`main/factory source navigation agrees for ${key}`);}}
async function sourceRoute({source,offset=0,rate=60,geometry=true,compareMain=true,data=sourceData}){
  const store={},random=createStudyRandom(913),instance=createKoiInstance(source,{massProfile:profile,random,turnControllerFactory:observedFactory(store,data)}),native=createAxialFrame(source),markers=materialMarkers(source),forecast=createManeuverFootprint(source,{massProfile:profile});
  const referenceStore={},referenceRandom=createStudyRandom(913),reference=compareMain?createSwimmer(referenceRandom,{turnControllerFactory:observedFactory(referenceStore,data)}):null;
  let forecastCalls=0;if(reference)reference.footprint(pose=>{forecastCalls++;return forecast.bounds(pose);});
  for(const [live,rng] of [[instance.swimmer,random],...(reference?[[reference,referenceRandom]]:[])]){rng.reset();live.reset();Object.assign(live.fish,{x:.55,y:.84,z:tankCenterZ+offset,yaw:0});live.navigate({x:-.85,y:.84,z:tankCenterZ+offset});}
  let maximumCorrection=0,corrections=0;const correction=instance.containment.correction.bind(instance.containment);
  instance.containment.correction=()=>{const next=correction(),length=Math.hypot(next.x,next.y,next.z);maximumCorrection=Math.max(maximumCorrection,length);if(length>1e-7)corrections++;return next;};
  const report={rate,offset,vertices:0,meshes:new Set(),maximumOutside:0,maximumCOMError:0,maximumYaw:0,maximumBend:0,maximumSourceSkinError:0,maximumHeadSkinError:0,sourceSkinChecks:0,maximumCommittedHeadingError:0,maximumReferenceStep:0};
  const water=new Box3(new Vector3(...waterMin),new Vector3(...waterMax)),skin=instance.asset.getObjectByName('Koi_skin'),n=skin.geometry.attributes.position.count,
    indices=Array.from(new Set([0,n-1,...Array.from({length:69},(_,i)=>Math.floor(i*(n-1)/68))]));
  let warmX,previous=new Vector3(instance.fish.x,instance.fish.y,instance.fish.z),faultsChecked=false;const snapshots={};
  for(let frame=0;frame<8*rate;frame++){
    if(frame===2*rate){warmX=instance.fish.x;instance.swimmer.navigate({x:.85,y:.84,z:tankCenterZ+offset+.04});reference?.navigate({x:.85,y:.84,z:tankCenterZ+offset+.04});}
    instance.step(1/rate);reference?.step(1/rate);const fish=instance.fish,packet=store.packet;if(reference)compareFish(fish,reference.fish);
    assert.equal(instance.behavior.contacts,0,'source-informed navigation does not need geometry teleports');
    report.maximumReferenceStep=Math.max(report.maximumReferenceStep,previous.distanceTo(new Vector3(fish.x,fish.y,fish.z)));previous.set(fish.x,fish.y,fish.z);
    report.maximumYaw=Math.max(report.maximumYaw,Math.abs(fish.yaw));report.maximumBend=Math.max(report.maximumBend,Math.abs(fish.turnAnterior),Math.abs(fish.turnTrunk),Math.abs(fish.turnPosterior));
    if(packet.active)report.maximumCommittedHeadingError=Math.max(report.maximumCommittedHeadingError,Math.abs(wrap(fish.yaw-packet.headHeading)));
    if(packet.sourceReplacementWeight>1-1e-10&&packet.intensity>.3&&(frame+1)%Math.max(1,Math.round(rate/12))===0){
      const error=sourceSkinError(instance,fish,packet,native,indices);report.maximumSourceSkinError=Math.max(report.maximumSourceSkinError,error.maximum);report.maximumHeadSkinError=Math.max(report.maximumHeadSkinError,error.headMaximum);report.sourceSkinChecks++;
      assert.ok(error.maximum<3e-6,'the actual source-phase skin shares committed head/body timing and mass placement');
      if(!faultsChecked){
        assert.ok(sourceSkinError(instance,fish,{...packet,b:packet.b.map(v=>v*1.3)},native,indices).maximum>.001,'actual skin oracle distinguishes a different body phase/amplitude');
        assert.ok(sourceSkinError(instance,fish,{...packet,headHeading:packet.headHeading+.2},native,indices).headMaximum>.005,'actual head skin oracle distinguishes a different heading phase');faultsChecked=true;
      }
    }
    const time=(frame+1)/rate;
    if(geometry&&([2,2.5,3,3.5,4,6,8].some(t=>Math.abs(time-t)<1e-9))){uploadedBounds(instance,forecast.bounds(fish),report,{reference:fish});uploadedBounds(instance,water,report);assertCOM(instance,fish,markers,report);}
    if([2.5,3,4,8].some(t=>Math.abs(time-t)<1e-9)){skin.skeleton.update();snapshots[time]={fish:{...fish},skin:indices.map(i=>skinPoint(skin,i).toArray())};}
  }
  assert.equal(store.factories,1,'companion constructs its own injected source controller');assert.equal(store.commits,8*120,'every physical tick commits source heading/body once');
  assert.ok(report.maximumCommittedHeadingError<2e-10,'the live head consumes the committed exact source heading increment');
  assert.ok(warmX<.48&&instance.fish.x>warmX+.15&&report.maximumYaw>2.8&&Math.abs(wrap(instance.fish.yaw-Math.PI))<.3,'actual warmed fish completes its U-turn and travels toward the new destination');
  assert.ok(report.maximumBend>2&&report.sourceSkinChecks>5&&faultsChecked,'the full route exercises real source body loading and actual skin sync');
  assert.ok(report.maximumReferenceStep<.025,'the navigation reference advances continuously at the tested display cadence');
  assert.ok(corrections===0&&maximumCorrection<1e-7,'complete actual containment observes no correction during the qualified routes');
  if(offset)assert.ok(forecastCalls>0,'near-pane route actually consults an independent mass-aware forecast');
  report.forecastCalls=forecastCalls;report.corrections=corrections;report.maximumCorrection=maximumCorrection;report.warmX=warmX;report.end={x:instance.fish.x,y:instance.fish.y,z:instance.fish.z,yaw:instance.fish.yaw};report.meshCount=report.meshes.size;delete report.meshes;
  const output={report,snapshots,fish:{...instance.fish}};instance.dispose();return output;
}
export async function assertLiveSourceRoutes(){
  const source=await parse(),before=geometryHash(source),open=await sourceRoute({source}),near=await sourceRoute({source,offset:.4});
  const cadences=[];
  for(const rate of [30,120]){
    const trial=await sourceRoute({source,rate,geometry:false,compareMain:false});compareFish(trial.fish,open.fish);let maximumSkinDifference=0;
    for(const time of Object.keys(open.snapshots)){compareFish(trial.snapshots[time].fish,open.snapshots[time].fish);for(let i=0;i<open.snapshots[time].skin.length;i++)maximumSkinDifference=Math.max(maximumSkinDifference,new Vector3(...open.snapshots[time].skin[i]).distanceTo(new Vector3(...trial.snapshots[time].skin[i])));}
    assert.ok(maximumSkinDifference<3e-6,'30/60/120Hz redraws preserve actual source-phase skin at common physical times');cadences.push({rate,maximumSkinDifference});
  }
  const plus=createSourceSteeringController(sourceData),minus=createSourceSteeringController(sourceData);plus.request(Math.PI,0);minus.request(-Math.PI,0);let mirrorError=0;
  for(let i=0;i<4*120;i++){const a=plus.commit(plus.propose(1/120)),b=minus.commit(minus.propose(1/120));for(const key of ['headHeading','headRate','headAcceleration'])mirrorError=Math.max(mirrorError,Math.abs(a[key]+b[key]));for(const key of ['b','v','a'])for(let j=0;j<3;j++)mirrorError=Math.max(mirrorError,Math.abs(a[key][j]+b[key][j]));}
  assert.ok(mirrorError<1e-11,'source steering mirrors head and regional body channels together');assert.equal(geometryHash(source),before,'all injected live routes preserve pristine source geometry');
  return {open:open.report,nearPane:near.report,cadences,mirrorError};
}

console.log(JSON.stringify({assetSHA256,massAndFootprint:await assertLiveMassJoin(),sourceRoutes:await assertLiveSourceRoutes()}));
