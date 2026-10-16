import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Matrix4,Quaternion,BufferAttribute} from '../static/vendor/three.webgpu.js';
import {createResearchShape} from '../static/aquarium-research-shape.js';
import {applySwimPose} from '../static/aquarium-rig.js';
import {createCaudalFrame,createCaudalMotion,bindCaudalGeometry,deformCaudalGeometry} from '../static/aquarium-caudal.js';

const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
const loader=new GLTFLoader().register(()=>({name:'ShapeGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
const joints=[];asset.traverse(object=>{if(object.isBone&&object.name.startsWith('spine_'))joints.push(object);});
joints.sort((a,b)=>a.name.localeCompare(b.name));const rest=joints.map(bone=>bone.quaternion.clone());
const body=asset.getObjectByName('Koi_skin'),tail=asset.getObjectByName('Caudal_membrane');
const a=body.geometry.attributes,tailBase=Float32Array.from(tail.geometry.attributes.position.array);
const tailBinding=bindCaudalGeometry(tailBase,createCaudalFrame(tailBase,tail.geometry.attributes.uv.array));
const shape=createResearchShape(asset),snapshot=()=>JSON.parse(JSON.stringify(shape.sample()));
const groups=new Map();
for(let i=0;i<a.position.count;i++){
  const x=a.position.getX(i);if(!groups.has(x))groups.set(x,[]);groups.get(x).push(i);
}
const rings=[...groups].sort(([x],[y])=>x-y).map(([,ids])=>({all:ids,
  unique:[...new Map(ids.map(i=>[[a.position.getX(i),a.position.getY(i),a.position.getZ(i)].join(','),i])).values()]}));

// Independent per-vertex shader evaluation. The sampler instead reduces the
// immutable material points to per-ring/per-bone weighted centroids.
function vertex(mesh,index){
  mesh.skeleton.update();const attrs=mesh.geometry.attributes;
  const point=new Vector3().fromBufferAttribute(attrs.position,index).applyMatrix4(mesh.bindMatrix);
  const out=new Vector3(),palette=mesh.skeleton.boneMatrices;
  for(let slot=0;slot<4;slot++){
    const weight=attrs.skinWeight.getComponent(index,slot);if(!weight)continue;
    const transform=new Matrix4().fromArray(palette,16*attrs.skinIndex.getComponent(index,slot));
    out.addScaledVector(point.clone().applyMatrix4(transform),weight);
  }
  return out.applyMatrix4(mesh.bindMatrixInverse).applyMatrix4(mesh.matrixWorld);
}
const vector=point=>new Vector3(point.x,point.y,point.z);
const same=(a,b,tolerance,message)=>assert.ok(vector(a).distanceTo(vector(b))<tolerance,message);
const average=(mesh,ids)=>ids.reduce((out,index)=>out.add(vertex(mesh,index)),new Vector3()).multiplyScalar(1/ids.length);
const zero={x:0,y:0,z:0,yaw:0,pitch:0,bank:0,phase:0,stroke:0,drive:0,speed:0,
  yawRate:0,pitchRate:0,bankRate:0,muscle1:0,muscle2:0,muscle3:0,verticalGain:0,twistGain:0};
for(const prefix of ['yawLag','pitchLag','bankLag','turnBend','turnRecruitment'])for(let i=0;i<4;i++)zero[prefix+(i||'')]=0;
function pose(fish){applySwimPose(asset,joints,rest,fish);}

const neutral=snapshot();assert.equal(neutral.ringCount,281);assert.equal(neutral.head.curve.length,101);
assert.equal(neutral.head.bodyCurve.length,101);assert.equal(neutral.coherence,null,'a nearly straight shape does not expose a noisy curvature ratio');
assert.ok(neutral.maximumDOverL<.001,'the real neutral tail is close to the anterior axis');
let snout=Infinity,caudal=-Infinity,barbel=Infinity;
asset.traverse(mesh=>{
  if(!mesh.isSkinnedMesh||mesh.name.startsWith('Anatomy'))return;
  for(let i=0;i<mesh.geometry.attributes.position.count;i++){
    const x=vertex(mesh,i).x;
    if(/barbel/i.test(mesh.name))barbel=Math.min(barbel,x);else snout=Math.min(snout,x);
    if(mesh.name.startsWith('Caudal'))caudal=Math.max(caudal,x);
  }
});
assert.ok(barbel<snout-.02,'this asset has barbels beyond its actual snout, making the length distinction meaningful');
assert.ok(Math.abs(neutral.length-(caudal-snout))<1e-10,'L is the actual fixed neutral snout-to-full-caudal extent, excluding barbels');
assert.ok(Math.abs(neutral.world.snout.x-snout)<1e-7,'the head origin attaches to the actual rostral surface');

let maximumPaletteError=0,maximumSeamBias=0;
const bend={...zero,turnBend:.5,turnBend1:.5,turnBend2:.5,turnBend3:.5};
for(const fish of [zero,{...bend,x:.23,y:.81,z:-.2,yaw:.73,pitch:.18,bank:-.11}]){
  pose(fish);const result=snapshot();
  for(let i=0;i<rings.length;i++){
    const expected=average(body,rings[i].unique),raw=average(body,rings[i].all);
    maximumPaletteError=Math.max(maximumPaletteError,expected.distanceTo(vector(result.world.body[i])));
    maximumSeamBias=Math.max(maximumSeamBias,expected.distanceTo(raw));
  }
}
assert.ok(maximumPaletteError<2e-12,'every actual ring centroid joins the current Float32 skin palette');
// Split one rendered material vertex into identical per-triangle seam records.
// Triangle positions/weights and thus the surface stay exactly the same, while
// a raw draw-vertex average becomes biased toward that material location.
pose(zero);const beforeSeam=snapshot(),savedAttributes={...body.geometry.attributes},savedIndex=body.geometry.index;
const seamRing=rings.reduce((best,ring,i)=>Math.abs(a.position.getX(ring.unique[0]))<Math.abs(a.position.getX(rings[best].unique[0]))?i:best,0);
const seamIndex=rings[seamRing].unique[0];
const uses=[];for(let i=0;i<savedIndex.count;i++)if(savedIndex.getX(i)===seamIndex)uses.push(i);
assert.ok(uses.length>=3,'the seam fixture splits a real shared surface vertex');
for(const [name,attribute] of Object.entries(savedAttributes)){
  const array=new attribute.array.constructor(attribute.array.length+uses.length*attribute.itemSize);array.set(attribute.array);
  for(let j=0;j<uses.length;j++)for(let k=0;k<attribute.itemSize;k++)array[attribute.array.length+j*attribute.itemSize+k]=attribute.getComponent(seamIndex,k);
  body.geometry.setAttribute(name,new BufferAttribute(array,attribute.itemSize,attribute.normalized));
}
const splitIndices=savedIndex.array.slice();uses.forEach((slot,j)=>{splitIndices[slot]=a.position.count+j;});
body.geometry.setIndex(new BufferAttribute(splitIndices,1));
const afterSeam=JSON.parse(JSON.stringify(createResearchShape(asset).sample()));
afterSeam.world.body.forEach((point,i)=>same(point,beforeSeam.world.body[i],2e-12,'identical material seam records cannot alter the observed centerline'));
const duplicated=[...rings[seamRing].all,...uses.map((_,j)=>a.position.count+j)];
maximumSeamBias=average(body,duplicated).distanceTo(average(body,rings[seamRing].unique));
assert.ok(maximumSeamBias>1e-5,'the equivalent-surface fixture would measurably bias a raw draw-vertex average');
for(const [name,attribute] of Object.entries(savedAttributes))body.geometry.setAttribute(name,attribute);
body.geometry.setIndex(savedIndex);

pose(bend);const base=snapshot(),baseWorld=asset.matrixWorld.clone();
assert.ok(base.maximumDOverL>.12,'an imposed substantial actual rig bend changes the diagram displacement');
assert.ok(base.coherence>.85&&base.chordShortening>.005,'the shape analysis distinguishes a coherent bow from a straight rig');
for(const attitude of [{yaw:.8,pitch:.3,bank:.2},{yaw:-2,pitch:-.42,bank:-.3},{yaw:2.8,pitch:.7,bank:1.1}]){
  pose({...bend,x:.4,y:.9,z:-.3,...attitude});const current=snapshot();
  const delta=new Matrix4().multiplyMatrices(asset.matrixWorld,baseWorld.clone().invert());
  for(let i=0;i<base.head.body.length;i++){
    same(current.head.body[i],base.head.body[i],6e-7,'actual head coordinates remove combined yaw, pitch and roll');
    same(current.world.body[i],vector(base.world.body[i]).applyMatrix4(delta),6e-7,'world data still follows the rendered surface');
  }
  for(const name of ['upper','lower','midpoint']){
    same(current.head.tips[name],base.head.tips[name],6e-7,'tail tips share the same actual skull coordinate frame');
    assert.ok(Math.abs(current.dOverL[name]-base.dOverL[name])<5e-7,'d/L cannot grow just because the whole fish rotates');
  }
  assert.ok(Math.abs(current.coherence-base.coherence)<5e-5,'coherence is independent of root attitude');
  assert.equal(current.length,neutral.length,'a curled or rotated fish never renormalizes its reference length');
}

// Rotate the actual root bone, leaving the asset transform alone. A sampler
// that uses only inverse(asset.matrixWorld) cannot pass this skull-frame join.
pose(bend);const beforeBoneRotation=snapshot();
joints[0].quaternion.premultiply(new Quaternion().setFromAxisAngle(new Vector3(0,1,0),.4));asset.updateMatrixWorld(true);
const afterBoneRotation=snapshot();
beforeBoneRotation.head.body.forEach((point,i)=>same(point,afterBoneRotation.head.body[i],6e-7,'the head frame follows the actual skeleton rather than a root-pose shortcut'));
assert.ok(vector(afterBoneRotation.world.body.at(-1)).distanceTo(vector(beforeBoneRotation.world.body.at(-1)))>.1,
  'the independent root-bone fixture actually moves the rendered skin');

pose(bend);const positive=snapshot();pose({...bend,turnBend:-.5,turnBend1:-.5,turnBend2:-.5,turnBend3:-.5});const negative=snapshot();
assert.ok(positive.dOverL.midpoint*negative.dOverL.midpoint<0,'mirrored rig bends retain signed offsets');
assert.ok(Math.abs(positive.dOverL.midpoint+negative.dOverL.midpoint)<2e-5,'mirror signs do not change the measured neutral length or anterior sampling');

// Test the fitted axis by its independent covariance/eigenvector properties,
// including the exact anterior-half centroid. Fitting the whole curved animal
// instead would reduce the apparent tail displacement and fail this oracle.
const result=negative,anterior=result.head.curve.slice(0,51),mean={x:0,z:0};
anterior.forEach(p=>{mean.x+=p.x/anterior.length;mean.z+=p.z/anterior.length;});
assert.ok(Math.hypot(mean.x-result.head.axis.origin.x,mean.z-result.head.axis.origin.z)<1e-12);
let xx=0,xz=0,zz=0;for(const p of anterior){const x=p.x-mean.x,z=p.z-mean.z;xx+=x*x;xz+=x*z;zz+=z*z;}
const direction=result.head.axis.direction;
assert.ok(Math.abs(direction.x*(xz*direction.x+zz*direction.z)-direction.z*(xx*direction.x+xz*direction.z))<1e-11,
  'the anterior axis is the principal line of the actual material points');
assert.ok(direction.x*(xx*direction.x+xz*direction.z)+direction.z*(xz*direction.x+zz*direction.z)>(xx+zz)/2,
  'the fit selects the longitudinal principal direction');
for(const name of ['upper','lower','midpoint']){
  const tip=result.head.tips[name],foot=result.head.feet[name];
  assert.ok(Math.abs((tip.x-foot.x)*direction.x+(tip.z-foot.z)*direction.z)<1e-12,'each foot is an orthogonal horizontal projection');
  assert.ok(Math.abs(Math.hypot(tip.x-foot.x,tip.z-foot.z)/result.length-Math.abs(result.dOverL[name]))<1e-12,
    'the displayed offset segment and numeric normalized displacement agree');
}

// Live caudal deformation must be read after geometry updates. The reference
// length stays neutral; the actual tip samples and resulting distance change.
pose(bend);const beforeTail=snapshot(),saved=Float32Array.from(tail.geometry.attributes.position.array);
deformCaudalGeometry(tailBinding,createCaudalMotion({poseHistory:false,speed:.2,stroke:1,phase:1.1}),tail.geometry.attributes.position.array);
const afterTail=snapshot();
assert.ok(vector(beforeTail.world.tips.upper).distanceTo(vector(afterTail.world.tips.upper))>.003,
  'the outline reads the current deformed caudal surface instead of a neutral cache');
const tipIndices={};for(const [name,side] of [['upper',1],['lower',-1]]){
  let maximum=-Infinity,index;
  for(let i=0;i<tailBase.length/3;i++)if(Math.sign(tailBase[i*3+1])===side&&tailBase[i*3]>maximum){maximum=tailBase[i*3];index=i;}
  tipIndices[name]=index;same(afterTail.world.tips[name],vertex(tail,index),1e-12,'the reported actual caudal lobe tip uses the rendered skin palette and position attribute');
}
assert.equal(afterTail.length,neutral.length);afterTail.world.body.forEach((point,i)=>same(point,beforeTail.world.body[i],1e-12,'intrinsic tail motion cannot rewrite the body material observations'));
tail.geometry.attributes.position.array.set(saved);

// Sampling paused geometry is idempotent and leaves all authored geometry and
// bone transforms intact. It only updates derived render matrices/palettes.
const transforms=joints.map(joint=>[...joint.position.toArray(),...joint.quaternion.toArray(),...joint.scale.toArray()]);
const positions=Float32Array.from(a.position.array),tailPositions=Float32Array.from(tail.geometry.attributes.position.array);
const paused=snapshot();for(let i=0;i<8;i++)assert.deepEqual(snapshot(),paused,'repeated observations do not invent motion or alter a paused pose');
assert.deepEqual(joints.map(joint=>[...joint.position.toArray(),...joint.quaternion.toArray(),...joint.scale.toArray()]),transforms);
assert.deepEqual(a.position.array,positions);assert.deepEqual(tail.geometry.attributes.position.array,tailPositions);
const started=performance.now();for(let i=0;i<200;i++)shape.sample();const sampleMilliseconds=(performance.now()-started)/200;
console.log(JSON.stringify({researchShape:'passed',rings:rings.length,length:neutral.length,maximumPaletteError,maximumSeamBias,sampleMilliseconds}));
