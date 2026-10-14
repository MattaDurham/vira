import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Vector3,Texture,PerspectiveCamera,WebGPUCoordinateSystem,WebGLCoordinateSystem} from '../static/vendor/three.webgpu.js';
import {createSwimmer,referencePose,finActuation} from '../static/aquarium-motion.js';
import {applySwimPose,createFacialRig} from '../static/aquarium-rig.js';
import {createContainment} from '../static/aquarium-collision.js';
import {dielectricReflectance,fresnelTable,waterExtinction,updateReflectionCamera} from '../static/aquarium-water.js';

assert.ok(dielectricReflectance(1,1,1.333)>.020&&dielectricReflectance(1,1,1.333)<.021,'a near-normal air/water interface reflects roughly two percent');
assert.equal(dielectricReflectance(.5,1.333,1),1,'a submerged sixty-degree incidence undergoes total internal reflection');
assert.equal(dielectricReflectance(0,1,1),0,'equal indices do not produce a reflective boundary');
const fresnel=fresnelTable();
assert.equal(fresnel[4*Math.round(.5*1023)+1],255,'the actual GPU lookup includes the underwater TIR regime');
assert.ok(fresnel[4*1023]>=5&&fresnel[4*1023]<=6,'the GPU lookup preserves weak reflection when looking down from air');
assert.ok(waterExtinction.every(v=>v>0),'viewing and reflected paths use finite positive water extinction');
for(const system of [WebGPUCoordinateSystem,WebGLCoordinateSystem])for(const eye of [[0,1.02,4.1],[-2.6,.935,2.45],[0,3.35,1.75]]){
  const camera=new PerspectiveCamera(26,1,.06,25);camera.coordinateSystem=system;
  camera.updateProjectionMatrix();camera.position.set(...eye);camera.lookAt(0,.86,-.2);camera.updateMatrixWorld();
  const mirror=updateReflectionCamera(camera,new PerspectiveCamera());
  assert.ok(Math.abs(mirror.position.y+camera.position.y-3.58)<1e-12,'the actual reflection camera mirrors metre-scaled height at the water interface');
  const direct=camera.getWorldDirection(new Vector3()),reflected=mirror.getWorldDirection(new Vector3());
  assert.ok(Math.abs(reflected.y+direct.y)<1e-12&&Math.abs(reflected.x-direct.x)<1e-12&&Math.abs(reflected.z-direct.z)<1e-12,'reflection reverses the vertical ray direction without changing horizontal travel');
  const near=system===WebGPUCoordinateSystem?0:-1;
  assert.ok(Math.abs(new Vector3(0,1.79,-.15).project(mirror).z-near)<1e-10,'both renderer projections clip at the real water interface');
}

const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
// Node has no image decoder. Geometry tests retain real materials/UVs and
// skinning while browser verification exercises image decoding and shading.
const loader=new GLTFLoader().register(()=>({name:'GeometryOnlyImages',loadTexture:()=>Promise.resolve(new Texture())}));
const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
const joints=[],skins=[];
asset.traverse(o=>{if(o.isBone&&o.name.startsWith('spine_'))joints.push(o);if(o.isSkinnedMesh)skins.push(o);});
joints.sort((a,b)=>a.name.localeCompare(b.name));
const rest=joints.map(b=>b.quaternion.clone());
assert.equal(joints.length,34);
const vertebrae=skins.filter(m=>m.name.startsWith('Anatomy_vertebra_'));
assert.equal(vertebrae.length,32,'the reference axial example has 32 separate bony centra');
assert.ok(skins.length>20,'the authored fish must export its skin, scales and fins with the rig');
for(const mesh of skins){
  const weights=mesh.geometry.attributes.skinWeight;
  for(let i=0;i<weights.count;i++)assert.ok(Math.abs(weights.getX(i)+weights.getY(i)+weights.getZ(i)+weights.getW(i)-1)<1e-5,'exported skin weights remain normalized');
  if(mesh.name.startsWith('Overlapping_scales')){
    const p=mesh.geometry.attributes.position,n=mesh.geometry.attributes.normal;
    for(let i=0;i<p.count;i++)assert.ok(p.getZ(i)*n.getZ(i)+p.getY(i)*n.getY(i)>-.003,'scale surfaces face out of the body');
  }
}

// Exercise the actual exported rig through the production pose function.
const neutral={...referencePose,x:0,y:0,z:0,yaw:0,bank:0,phase:0,speed:.25,yawRate:0,verticalGain:0,twistGain:0};
applySwimPose(asset,joints,rest,neutral);
const tailA=joints.at(-1).getWorldPosition(new Vector3());
applySwimPose(asset,joints,rest,{...neutral,phase:Math.PI});
const tailB=joints.at(-1).getWorldPosition(new Vector3());
assert.ok(Math.abs(tailA.z-tailB.z)>.06,'the tail has a visible lateral excursion through water');
assert.ok(Math.abs(tailA.y-tailB.y)<1e-5,'a lateral tail beat must not lift the tail vertically');
assert.deepEqual(asset.scale.toArray(),[1,1,1],'depth changes never resize the fish');
const body=skins.find(m=>m.name.startsWith('Koi_skin'));
assert.ok(body.geometry.attributes.uv,'skin texture follows the authored circumference');
for(const fin of skins.filter(m=>/^(Pectoral|Pelvic|Dorsal)_membrane/.test(m.name))){
  const root=new Vector3().fromBufferAttribute(fin.geometry.attributes.position,0);
  const surface=body.geometry.attributes.position;
  let separation=Infinity;
  for(let i=0;i<surface.count;i++)separation=Math.min(separation,root.distanceTo(new Vector3().fromBufferAttribute(surface,i)));
  assert.ok(separation<.007,`${fin.name} basal tissue attaches to the actual exported skin, rather than hovering below or above it`);
  const uv=fin.geometry.attributes.uv;
  assert.ok(uv.getY(0)>.99,'fin root uses its own basal tissue coordinates rather than the body photograph');
  let lo=1,hi=0;
  for(let i=0;i<uv.count;i++){lo=Math.min(lo,uv.getY(i));hi=Math.max(hi,uv.getY(i));}
  assert.ok(lo<.01&&hi>.99,'the real membrane spans its root-to-tip tissue map');
}

const positions=body.geometry.attributes.position;
let peduncle=0;
for(let i=0;i<positions.count;i++)if(positions.getX(i)>positions.getX(peduncle))peduncle=i;
const stroke=[];
for(let phase=0;phase<Math.PI*2;phase+=Math.PI/12){
  applySwimPose(asset,joints,rest,{...neutral,phase});
  skins.forEach(m=>m.skeleton.update());
  const p=body.applyBoneTransform(peduncle,new Vector3().fromBufferAttribute(positions,peduncle));
  stroke.push(p.z);
}
assert.ok(Math.max(...stroke)-Math.min(...stroke)>.14,'exported skin vertices follow a substantial travelling body wave, beyond moving bone markers');
const boneLengths=[];
for(const phase of [0,Math.PI]){
  applySwimPose(asset,joints,rest,{...neutral,phase,verticalGain:1,twistGain:1,pitchRate:.3*Math.cos(phase),torsionDrive:.24*Math.sin(phase+.5)});
  boneLengths.push(vertebrae.map(m=>{
    m.skeleton.update();
    const p=m.geometry.attributes.position;
    const a=m.applyBoneTransform(0,new Vector3().fromBufferAttribute(p,0));
    const b=m.applyBoneTransform(p.count-1,new Vector3().fromBufferAttribute(p,p.count-1));
    return a.distanceTo(b);
  }));
}
boneLengths[0].forEach((length,i)=>assert.ok(Math.abs(length-boneLengths[1][i])<1e-6,'bony centra remain rigid while the joints and skin bend'));
// Isolate genuine vertical curvature from rigid whole-body pitch.
const vertical=[];
for(const pitchRate of [-.3,.3]){
  applySwimPose(asset,joints,rest,{...neutral,lateralGain:0,verticalGain:1,pitchRate,phase:.8});
  skins.forEach(m=>m.skeleton.update());
  vertical.push(body.applyBoneTransform(peduncle,new Vector3().fromBufferAttribute(positions,peduncle)).applyMatrix4(body.matrixWorld));
}
assert.ok(Math.abs(vertical[0].y-vertical[1].y)>.055,'actual tail skin bends vertically with the head held level');
assert.ok(Math.abs(vertical[0].z-vertical[1].z)<.005,'isolated vertical flexion does not become a sideways beat');
const twists=[];
for(const bank of [-.24,.24]){
  applySwimPose(asset,joints,rest,{...neutral,bank:0,lateralGain:0,verticalGain:0,twistGain:1,torsionDrive:bank,phase:0});
  const orientation=joints.at(-1).getWorldQuaternion(joints.at(-1).quaternion.clone());
  twists.push(orientation);
}
assert.ok(twists[0].angleTo(twists[1])>.10,'the caudal frame twists relative to a level skull');
applySwimPose(asset,joints,rest,neutral);
const faceRig=createFacialRig(asset);
assert.equal(faceRig.jointCount,6,'mouth, paired gill covers and eyes have separate deformation controls');
const lower=skins.find(m=>m.name.startsWith('Lower_labial_fold'));
assert.ok(lower,'the lip is real modeled tissue');
const lipPos=lower.geometry.attributes.position;
const breaths=[];
for(const age of [Math.PI*.5/5.3,Math.PI*1.5/5.3]){
  applySwimPose(asset,joints,rest,neutral);faceRig.pose({...neutral,age});lower.skeleton.update();
  breaths.push(lower.applyBoneTransform(0,new Vector3().fromBufferAttribute(lipPos,0)).applyMatrix4(lower.matrixWorld));
}
assert.ok(breaths[0].distanceTo(breaths[1])>.003,'breathing moves the actual lip through the lower-jaw bone');
const containment=createContainment(asset);
for(let i=0;i<48;i++){
  const swimmer=createSwimmer(()=>.5);
  Object.assign(swimmer.fish,{x:i%2?1.16:-1.16,y:i%3?.77:.47,z:i%2?.31:-.67,
    yaw:i*Math.PI/12,pitch:i%2?.42:-.42,bank:.1,pitchRate:i%2?.3:-.3,yawRate:i%3?.7:-.7,phase:i*.43,drive:.3,stroke:1});
  for(let attempt=0;attempt<5;attempt++){
    applySwimPose(asset,joints,rest,swimmer.fish);
    if(!swimmer.contain(containment.correction()))break;
  }
  applySwimPose(asset,joints,rest,swimmer.fish);
  const b=containment.measure();
  assert.ok(b.min.x>=-2&&b.max.x<=2&&b.min.y>=.02&&b.max.y<=1.79&&b.min.z>=-1.1&&b.max.z<=.69,
    'the articulated body and fins remain inside the tank when turning, pitching and bending near glass');
}

const ascent=createSwimmer(()=>.5);
Object.assign(ascent.fish,{x:.65,y:.58,z:.12,yaw:0});ascent.navigate({x:-.85,y:1.17,z:-.1});
for(let i=0;i<720;i++)ascent.step(1/60);
const elevated=ascent.fish.y;
assert.ok(elevated>.85,'the depth study swims upward through actual forward propulsion');
ascent.navigate({x:.85,y:.50,z:-.1});
for(let i=0;i<1200;i++)ascent.step(1/60);
assert.ok(ascent.fish.y<elevated-.15,'the same animal swims back down after turning');

const idle=createSwimmer(()=>.5),curious=createSwimmer(()=>.5),startled=createSwimmer(()=>.5);
curious.follow({x:-.9,y:1.25,z:.2});startled.flick({x:.3,y:.77,z:.14});
let normalMax=0,startleMax=0;
for(let i=0;i<600;i++){
  const previousVertical=curious.fish.vy;
  idle.step(1/60);curious.step(1/60);startled.step(1/60);
  normalMax=Math.max(normalMax,idle.fish.speed);startleMax=Math.max(startleMax,startled.fish.speed);
  const f=curious.fish;
  if(i>60&&f.vy>.002&&f.vy>previousVertical+.0001)assert.ok(f.pitch>0&&Math.hypot(f.vx,f.vz)>.02,'upward acceleration comes from nose-up swimming; a coast may retain small upward momentum');
}
assert.ok(curious.fish.y>idle.fish.y+.15,'a high cursor encourages an actual climb');
assert.ok(startleMax>normalMax*1.5,'a close tap creates a short, faster escape response');
curious.reset();assert.equal(curious.fish.y,referencePose.y);assert.equal(curious.fish.speed,0);

const seeded=initial=>{let value=initial;return ()=>{value=(Math.imul(value,1664525)+1013904223)>>>0;return value/4294967296;};};
const modes=new Set();let coasts=0,heldCourses=0,quietFrames=0,levelFrames=0,minDepth=2,maxDepth=0;
for(const seed of [913,481,33]){
 const routine=createSwimmer(seeded(seed));
 for(let i=0;i<12000;i++){
  const phase=routine.fish.phase;routine.step(1/60);
  const f=routine.fish,b=routine.behavior;modes.add(b.intent);
  if(b.gait==='coast'&&f.stroke<.03){
    coasts++;if(Math.abs(f.yawRate)<.12)heldCourses++;
    assert.ok(f.phase-phase<.005,'a relaxed coast does not keep beating the tail as though it were swimming hard');
  }
  if(f.speed<.025)quietFrames++;
  if(Math.abs(f.pitch)<.09)levelFrames++;
  minDepth=Math.min(minDepth,f.y);maxDepth=Math.max(maxDepth,f.y);
 }
}
assert.deepEqual([...modes].sort(),['cruise','hover','inspect'],'routine choices include travel, inspection and quiet pauses');
assert.ok(coasts>600&&heldCourses/coasts>.85,'most relaxed glides hold a course instead of continuously homing toward a random point');
assert.ok(quietFrames>600&&quietFrames<30000,'the repertoire includes quiet intervals without getting stuck for most of the runs');
assert.ok(levelFrames>18000&&maxDepth-minDepth>.20,'the fish mostly swims level but also makes slower, substantial depth excursions');
const finHover=finActuation({speed:0,brake:1,finPhase:0},1);
const finCruise=finActuation({speed:.25,brake:0,finPhase:0},1);
assert.ok(finHover.spread>finCruise.spread+.25,'paired fins open for hovering and braking, and trail in a forward burst');
assert.notEqual(finActuation({speed:0,brake:1,finPhase:0},-1).paddle,finHover.paddle,'paired fins need not paddle in mechanical synchrony');

const attentive=createSwimmer(seeded(481));
attentive.follow({x:-.9,y:1.1,z:.2});
for(let i=0;i<120;i++)attentive.step(1/60);
assert.equal(attentive.behavior.intent,'curious','the fish notices cursor movement after a short attention delay');
for(let i=0;i<1200;i++)attentive.step(1/60);
assert.ok(attentive.behavior.attention<.03,'an unmoving pointer loses its novelty rather than trapping the fish in a permanent chase');
attentive.follow({x:.7,y:.8,z:.2});
for(let i=0;i<75;i++)attentive.step(1/60);
assert.equal(attentive.behavior.intent,'curious','fresh cursor movement can attract attention again');

// Check navigation against real skinned surfaces, not only the root bounds.
// Sampling at 10 Hz keeps the asset harness affordable on CPU-only CI runners.
const turns=createSwimmer(seeded(913));let turnContacts=0,largestCorrection=0;
for(let i=0;i<1800;i++){
  turns.step(1/30);
  if(i%3)continue;
  applySwimPose(asset,joints,rest,turns.fish);
  const correction=containment.correction(),magnitude=Math.hypot(correction.x,correction.y,correction.z);
  if(magnitude>.001)turnContacts++;
  largestCorrection=Math.max(largestCorrection,magnitude);turns.contain(correction);
}
assert.ok(turnContacts<4&&largestCorrection<.005,`routine turns avoid visible collision translations: ${turnContacts} contacts, ${largestCorrection} m correction`);

let seed=913;
const roam=createSwimmer(()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;});
for(let i=0;i<18000;i++){
  if(i%400===0)roam.flick({x:-1+2*(i%800)/800,y:1.1,z:.2});
  roam.step(1/60);
  for(const v of Object.values(roam.fish))assert.ok(Number.isFinite(v),'long runs stay finite');
  const f=roam.fish;
  assert.ok(f.x>=-1.16&&f.x<=1.16&&f.y>=.34&&f.y<=1.34&&f.z>=-.67&&f.z<=.31,'the whole fish has room inside the glass');
}
console.log('Aquarium: geometry, three-axis deformation, face, pitched swimming, deliberate behavior, glides, fin control, attention, startle and actual-body turn clearance passed');
