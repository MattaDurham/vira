import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Vector3,Texture} from '../static/vendor/three.webgpu.js';
import {createSwimmer,referencePose} from '../static/aquarium-motion.js';
import {applySwimPose,createFacialRig} from '../static/aquarium-rig.js';
import {createContainment} from '../static/aquarium-collision.js';

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

let seed=913;
const roam=createSwimmer(()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;});
for(let i=0;i<18000;i++){
  if(i%400===0)roam.flick({x:-1+2*(i%800)/800,y:1.1,z:.2});
  roam.step(1/60);
  for(const v of Object.values(roam.fish))assert.ok(Number.isFinite(v),'long runs stay finite');
  const f=roam.fish;
  assert.ok(f.x>=-1.16&&f.x<=1.16&&f.y>=.34&&f.y<=1.34&&f.z>=-.67&&f.z<=.31,'the whole fish has room inside the glass');
}
console.log('Aquarium: exported geometry, three-axis skin deformation, facial articulation, pitched swimming, curiosity, startle and five-minute confinement passed');
