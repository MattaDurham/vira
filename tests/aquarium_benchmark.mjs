import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Vector3} from '../static/vendor/three.webgpu.js';
import {createSwimmer,referencePose} from '../static/aquarium-motion.js';
import {applySwimPose} from '../static/aquarium-rig.js';

const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
const {scene:asset}=await new GLTFLoader().parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
const joints=[],skins=[];
asset.traverse(o=>{if(o.isBone&&o.name.startsWith('spine_'))joints.push(o);if(o.isSkinnedMesh)skins.push(o);});
joints.sort((a,b)=>a.name.localeCompare(b.name));
const rest=joints.map(b=>b.quaternion.clone());
assert.equal(joints.length,11);
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
const neutral={...referencePose,x:0,y:0,z:0,yaw:0,bank:0,phase:0,speed:.25,yawRate:0};
applySwimPose(asset,joints,rest,neutral);
const tailA=joints.at(-1).getWorldPosition(new Vector3());
applySwimPose(asset,joints,rest,{...neutral,phase:Math.PI});
const tailB=joints.at(-1).getWorldPosition(new Vector3());
assert.ok(Math.abs(tailA.z-tailB.z)>.01,'the tail sweeps sideways through water instead of twisting about its length');
assert.ok(Math.abs(tailA.y-tailB.y)<1e-5,'a lateral tail beat must not lift the tail vertically');
assert.deepEqual(asset.scale.toArray(),[1,1,1],'depth changes never resize the fish');

const idle=createSwimmer(()=>.5),curious=createSwimmer(()=>.5),startled=createSwimmer(()=>.5);
curious.follow({x:-.9,y:1.25,z:.2});startled.flick({x:.3,y:.77,z:.14});
let normalMax=0,startleMax=0;
for(let i=0;i<600;i++){
  idle.step(1/60);curious.step(1/60);startled.step(1/60);
  normalMax=Math.max(normalMax,idle.fish.speed);startleMax=Math.max(startleMax,startled.fish.speed);
  const f=curious.fish;
  if(i>60&&f.vy>.002)assert.ok(f.pitch>0&&Math.hypot(f.vx,f.vz)>.02,'rising is forward swimming with nose-up thrust');
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
console.log('Aquarium: exported geometry, lateral rig deformation, pitched swimming, curiosity, startle and five-minute confinement passed');
