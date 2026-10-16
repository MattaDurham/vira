// Fixed material markers join placement math to the actual exported hierarchy.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Group,Matrix4,Quaternion,Texture,Vector3} from '../static/vendor/three.webgpu.js';
import {createAxialFrame} from '../static/aquarium-axial-frame.js';
import {createMassPlacement} from '../static/aquarium-mass-placement.js';

const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
const asset=(await new GLTFLoader().register(()=>({name:'MassPlacementActualGeometry',loadTexture:()=>Promise.resolve(new Texture())}))
 .parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;
const sampler=createAxialFrame(asset),profile=JSON.parse(await readFile(new URL('../static/aquarium/body-mass.json',import.meta.url),'utf8'));
const totalMass=profile.bodies29.reduce((sum,r)=>sum+r.mass,0);
asset.updateMatrixWorld(true);
// Save the profile material points in REAL rest-bone coordinates using matrices,
// rather than copying the placement helper's rest-quaternion transport formula.
const markers=profile.bodies29.map(region=>({mass:region.mass,bone:sampler.joints[region.regions[0]],
 point:new Vector3(...region.com).applyMatrix4(asset.matrixWorld)
  .applyMatrix4(sampler.joints[region.regions[0]].matrixWorld.clone().invert())}));
const materialCOM=()=>markers.reduce((sum,r)=>sum.addScaledVector(r.point.clone().applyMatrix4(r.bone.matrixWorld),r.mass),new Vector3()).divideScalar(totalMass);
const quiet={phase:0,stroke:0,drive:0,muscle1:0,muscle2:0,muscle3:0,poseHistory:true,
 yawLag1:0,yawLag2:0,yawLag3:0,pitchLag1:0,pitchLag2:0,pitchLag3:0,bankLag1:0,bankLag2:0,bankLag3:0};
const shapes=[
 ['neutral',{}],['lateral',{turnAnterior:2.4,turnTrunk:2.4,turnPosterior:2.4}],
 ['vertical',{lateralGain:0,pitchLag1:-.12,pitchLag2:-.27,pitchLag3:-.35}],
 ['twist',{lateralGain:0,verticalGain:0,torsionDrive:.28}],
 ['combined',{turnAnterior:2.4,turnTrunk:.9,turnPosterior:-.6,pitchLag1:-.08,pitchLag2:-.18,pitchLag3:-.29,torsionDrive:.21}],
].map(([name,pose])=>({name,frame:sampler.capture({...quiet,...pose},sampler.createFrame())}));
const nativeShape=shapes.map(({frame})=>JSON.stringify({positions:frame.positions.map(v=>v.toArray()),rotations:frame.rotations.map(q=>q.toArray())}));
const pristine=JSON.stringify({centers:sampler.centers.map(v=>v.toArray()),rest:sampler.rest.map(q=>q.toArray()),profile});
const near=(a,b,tolerance,message)=>assert.ok(a.distanceTo(b)<tolerance,`${message}: ${a.distanceTo(b)}`);
const Q=new Quaternion().setFromAxisAngle(new Vector3(.3,.8,-.2).normalize(),.73),F=new Vector3(.32,.81,-.29),C=new Vector3(-.24,.94,.18);
function setShape(frame){
 asset.updateMatrixWorld(true);const rootQ=asset.getWorldQuaternion(new Quaternion());
 for(let i=0;i<34;i++){
  const bone=sampler.joints[i],worldPoint=frame.positions[i].clone().applyMatrix4(asset.matrixWorld);
  const worldQ=rootQ.clone().multiply(frame.rotations[i]).normalize();
  bone.position.copy(bone.parent.worldToLocal(worldPoint));
  bone.quaternion.copy(bone.parent.getWorldQuaternion(new Quaternion()).invert().multiply(worldQ)).normalize();
  bone.updateMatrixWorld(true);
 }
 asset.updateMatrixWorld(true);
}
function parentFixture(transformed=false){
 const parent=new Group();
 if(transformed){parent.position.set(.38,-.24,.11);parent.rotation.set(.18,.6,-.07);parent.scale.setScalar(1.7);}
 parent.add(asset);parent.updateMatrixWorld(true);return parent;
}
function runAssertions(factory=createMassPlacement){
 assert.throws(()=>factory({centers:sampler.centers,rest:sampler.rest}),Error,'a missing mass profile cannot silently fall back to geometric centering');
 const placement=factory({centers:sampler.centers,rest:sampler.rest,profile}),capture=placement.createFrame();
 assert.ok(Math.abs(placement.totalMass-totalMass)<1e-12,'all original region masses retain their fixed physical weights');
 const expectedNeutral=profile.bodies29.reduce((sum,r)=>sum.addScaledVector(new Vector3(...r.com),r.mass),new Vector3()).divideScalar(totalMass);
 near(placement.neutralCOM,expectedNeutral,1e-12,'neutral centroid uses the public fixed mass profile');
 const returnedNeutral=placement.neutralCOM;returnedNeutral.set(99,99,99);
 near(placement.neutralCOM,expectedNeutral,1e-12,'neutral COM cannot be rewritten by a caller');
 let maximumCentroidError=0,maximumReferenceError=0,maximumCOMTargetError=0,maximumCapturePositionError=0,maximumNeutralRootShift=0;
 let largestBendShift=0,twistShift=0;
 for(const transformed of [false,true])for(const scale of [1,.42]){
  const parent=parentFixture(transformed);asset.position.copy(F);asset.quaternion.copy(Q);asset.scale.setScalar(scale);
  setShape(shapes[0].frame);const neutralWorldCOM=materialCOM();
  const nativeNeutralBody=asset.getObjectByName('Koi_skin'),positions=nativeNeutralBody.geometry.attributes.position;
  nativeNeutralBody.skeleton.update();const neutralPoints=[];
  for(let i=0;i<positions.count;i+=Math.max(1,Math.floor(positions.count/53)))
   neutralPoints.push([i,nativeNeutralBody.getVertexPosition(i,new Vector3()).applyMatrix4(nativeNeutralBody.matrixWorld)]);
  for(const {name,frame}of shapes){
   asset.position.copy(F);asset.quaternion.copy(Q);asset.scale.setScalar(scale);setShape(frame);
   assert.equal(placement.captureAsset(asset,sampler.joints,capture),capture,'native capture reuses caller-owned frame storage');
   for(let i=0;i<34;i++)maximumCapturePositionError=Math.max(maximumCapturePositionError,capture.positions[i].distanceTo(frame.positions[i]));
   const centroid=placement.centroid(capture.positions,capture.rotations,new Vector3());
   const actualNative=asset.worldToLocal(materialCOM());maximumCentroidError=Math.max(maximumCentroidError,centroid.distanceTo(actualNative));
   near(centroid,actualNative,2e-7,'captured native centroid matches independently transported actual GLB material markers');
   const displacement=placement.displacement(capture.positions,capture.rotations,new Vector3());
   near(displacement,expectedNeutral.clone().sub(actualNative),2e-7,'displacement retains the neutral rest convention');
   largestBendShift=Math.max(largestBendShift,displacement.length());if(name==='twist')twistShift=Math.max(twistShift,displacement.length());
   const options={positions:capture.positions,rotations:capture.rotations,referencePosition:F,orientation:Q,scale,parentMatrix:parent.matrixWorld};
   const root=placement.rootFromReference(options,new Vector3());
   if(name==='neutral')maximumNeutralRootShift=Math.max(maximumNeutralRootShift,root.distanceTo(F));
   asset.position.copy(root);asset.updateMatrixWorld(true);
   const referenceError=materialCOM().distanceTo(neutralWorldCOM);maximumReferenceError=Math.max(maximumReferenceError,referenceError);
   assert.ok(referenceError<5e-7,'bent actual mass centroid keeps the independently measured neutral reference placement');
   if(name==='neutral'){
    nativeNeutralBody.skeleton.update();
    for(const [index,point]of neutralPoints)near(nativeNeutralBody.getVertexPosition(index,new Vector3()).applyMatrix4(nativeNeutralBody.matrixWorld),point,5e-7,'neutral rendered material placement stays invariant');
   }
   const comRoot=placement.rootFromCOM({...options,comPosition:C},new Vector3());
   asset.position.copy(comRoot);asset.updateMatrixWorld(true);
   const expectedWorld=parent.localToWorld(C.clone()),comError=materialCOM().distanceTo(expectedWorld);
   maximumCOMTargetError=Math.max(maximumCOMTargetError,comError);
   assert.ok(comError<5e-7,'explicit parent-local COM target reaches the actual material centroid without another parent/world rotation or scale');
   const shiftedFrame=placement.captureAsset(asset,sampler.joints,placement.createFrame());
   near(placement.centroid(shiftedFrame.positions,shiftedFrame.rotations),centroid,2e-7,'capture removes arbitrary root placement, orientation and both uniform scales');
  }
  asset.removeFromParent();
 }
 assert.ok(maximumNeutralRootShift<2e-8,'reference placement never reinterprets the neutral rig anchor as COM');
 assert.ok(maximumCapturePositionError<2e-8,'native capture cancels actual root and parent transforms exactly once');
 assert.ok(largestBendShift>.1&&twistShift>.001,'lateral/vertical/twist fixtures meaningfully exercise posed mass transport');
 // Proper parent transforms may be translated/rotated/scaled. Unsupported
 // transforms fail instead of returning a plausible but incorrect quaternion.
 const invalidParents=[['nonuniform',()=>{const p=new Group();p.scale.set(1,1.4,1);return p;}],
  ['reflection',()=>{const p=new Group();p.scale.set(-1,1,1);return p;}],
  ['singular',()=>{const p=new Group();p.scale.setScalar(0);return p;}],
  ['equal-column shear',()=>{const p=new Group();p.matrixAutoUpdate=false;p.matrix.set(1,.3,0,.2,0,Math.sqrt(.91),0,.4,0,0,1,-.1,0,0,0,1);return p;}]];
 for(const [label,make]of invalidParents){
  const parent=make();parent.add(asset);asset.scale.setScalar(1);parent.updateMatrixWorld(true);
  assert.throws(()=>placement.captureAsset(asset,sampler.joints,placement.createFrame()),Error,`actual ${label} parent capture must be rejected`);
  const options={positions:shapes[1].frame.positions,rotations:shapes[1].frame.rotations,referencePosition:F,comPosition:C,orientation:Q,scale:1,parentMatrix:parent.matrixWorld};
  assert.throws(()=>placement.rootFromReference(options),Error,`${label} reference placement must be rejected`);
  assert.throws(()=>placement.rootFromCOM(options),Error,`${label} COM placement must be rejected`);
  asset.removeFromParent();
 }
 parentFixture();
 for(const scale of [new Vector3(1,2,1),new Vector3(-1,1,1),new Vector3(0,0,0)]){
  asset.scale.copy(scale);asset.updateMatrixWorld(true);
  assert.throws(()=>placement.captureAsset(asset,sampler.joints,placement.createFrame()),Error,'invalid actual asset scale must be rejected');
 }
 asset.scale.setScalar(1);asset.removeFromParent();
 assert.equal(JSON.stringify({centers:sampler.centers.map(v=>v.toArray()),rest:sampler.rest.map(q=>q.toArray()),profile}),pristine,'placement preserves pristine rest frames and public mass evidence');
 shapes.forEach(({frame},i)=>assert.equal(JSON.stringify({positions:frame.positions.map(v=>v.toArray()),rotations:frame.rotations.map(q=>q.toArray())}),nativeShape[i],'placement leaves supplied shape frames unchanged'));
 return {maximumCentroidError,maximumReferenceError,maximumCOMTargetError,maximumCapturePositionError,maximumNeutralRootShift,largestBendShift,twistShift};
}
const report=runAssertions();
const moduleURL=new URL('../static/aquarium-mass-placement.js',import.meta.url),source=await readFile(moduleURL,'utf8');
const mutations={
 noCompensation:s=>s.replace('return out.copy(referencePosition).add(correction);','return out.copy(referencePosition);'),
 doubleScale:s=>s.replaceAll('correction.multiplyScalar(scale)','correction.multiplyScalar(scale*scale)'),
 doubleParentRotation:s=>s.replaceAll('rotation.copy(orientation).normalize()',
  'rotation.copy(orientation).premultiply(new Quaternion().setFromRotationMatrix(parentMatrix??new Matrix4())).normalize()'),
 referenceAsCOM:s=>s.replace('displacement(positions,rotations,correction);','centroid(positions,rotations,correction).multiplyScalar(-1);'),
 bypassParentValidation:s=>s.replace('function uniformParent(matrix){','function uniformParent(matrix){return;'),
};
const rejectedMutants=[];
for(const [name,mutate]of Object.entries(mutations)){
 const changed=mutate(source);assert.notEqual(changed,source,`test injector applies ${name}`);
 const resolved=changed.replace(/from (['"])(\.{1,2}\/[^'"]+)\1/g,(_,quote,path)=>'from '+JSON.stringify(new URL(path,moduleURL).href));
 const mutant=await import('data:text/javascript;base64,'+Buffer.from(resolved).toString('base64'));
 assert.throws(()=>runAssertions(mutant.createMassPlacement),assert.AssertionError,`${name} must fail the actual material-marker harness`);
 rejectedMutants.push(name);
}
console.log(JSON.stringify({massPlacement:'passed',...report,rejectedMutants}));
