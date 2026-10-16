// Actual-asset regression for source-guided prescribed animation and isolation.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Quaternion} from '../static/vendor/three.webgpu.js';
import {createAxialFrame} from '../static/aquarium-axial-frame.js';
import {createObservedTurnCue} from '../static/aquarium-reference-turn-cue.js';
import {createReferenceAnimationClip} from '../static/aquarium-reference-motion.js';

const data=JSON.parse(await readFile(new URL('../static/aquarium/reference-turn.json',import.meta.url),'utf8'));
function freeze(value){if(value&&typeof value==='object'){for(const child of Object.values(value))freeze(child);Object.freeze(value);}return value;}
freeze(data);
const profile=JSON.parse(await readFile(new URL('../static/aquarium/body-mass.json',import.meta.url),'utf8'));
const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
const asset=(await new GLTFLoader().register(()=>({name:'ReferenceAnimationGeometry',loadTexture:()=>Promise.resolve(new Texture())}))
 .parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;
const sampler=createAxialFrame(asset),source=data.trials.fast,frames=source.frames,first=frames[0].time,last=frames.at(-1).time;
function runAssertions(factory=createReferenceAnimationClip){
const fullDuration=last-first,L=1.496383,Y=new Vector3(0,1,0),anchor=new Vector3(.31,.8,-.22);
const fixed=new Quaternion().setFromAxisAngle(new Vector3(.2,.9,-.3).normalize(),.47);
const create=options=>factory({data,sampler,profile,targetLength:L,...options});
const clip=create({anchorCOM:anchor,sourceFrameQuaternion:fixed}),take=(c,t)=>c.sample(t,c.createPacket());
const cue=createObservedTurnCue(data,{start:clip.metadata.sourceStart,duration:fullDuration});
const array=v=>Array.isArray(v)?v:v.toArray(),maxError=(a,b)=>Math.max(...array(a).map((x,i)=>Math.abs(x-array(b)[i])));
const near=(a,b,tol,message)=>assert.ok(maxError(a,b)<tol,`${message}: ${maxError(a,b)}`);
const snapshot=p=>JSON.stringify({positions:p.positions.map(array),rotations:p.rotations.map(array),shape:p.shape,
 rootReference:Object.fromEntries(Object.entries(p.rootReference).map(([key,value])=>[key,value?.toArray?value.toArray():value])),source:p.source});
assert.equal(frames.length,69,'public reference fixture retains all padded fast observations');
const pristine=()=>JSON.stringify({joints:sampler.joints.map(j=>[...j.position.toArray(),...j.quaternion.toArray()]),centers:sampler.centers.map(array),rest:sampler.rest.map(array),profile});
const pristineBefore=pristine();
const sourceAge=(c,time)=>c.metadata.sourceStart+(time-first)*c.metadata.duration/fullDuration;
const getCOM=f=>new Vector3(f.authorCOMOverL[0]-frames[0].authorCOMOverL[0],0,-f.authorCOMOverL[1]+frames[0].authorCOMOverL[1]);
function derivative(index,read,order=1){
 const a=order===1?Math.max(0,index-1):index===0?0:index===frames.length-1?index-2:index-1;
 if(order===1){
  if(index===0)return read(frames[0]).map((x,j)=>(-3*x+4*read(frames[1])[j]-read(frames[2])[j])/(frames[2].time-frames[0].time));
  if(index===frames.length-1)return read(frames[index]).map((x,j)=>(3*x-4*read(frames[index-1])[j]+read(frames[index-2])[j])/(frames[index].time-frames[index-2].time));
  const b=Math.min(frames.length-1,index+1);return read(frames[b]).map((x,j)=>(x-read(frames[a])[j])/(frames[b].time-frames[a].time));
 }
 const b=a+1,c=a+2,h1=frames[b].time-frames[a].time,h2=frames[c].time-frames[b].time;
 return read(frames[a]).map((_,j)=>2*((read(frames[c])[j]-read(frames[b])[j])/h2-(read(frames[b])[j]-read(frames[a])[j])/h1)/(h1+h2));
}
let maxRootKnotError=0,maxRootRateError=0,maxRootAccelerationError=0,maxShapeKnotError=0;
for(let i=0;i<frames.length;i++){
 const f=frames[i],age=sourceAge(clip,f.time),p=take(clip,age),root=p.rootReference;
 const expectedCOM=getCOM(f).multiplyScalar(L).applyQuaternion(fixed).add(anchor);
 const expectedQ=fixed.clone().multiply(new Quaternion().setFromAxisAngle(Y,f.headReference));
 maxRootKnotError=Math.max(maxRootKnotError,maxError(root.comWorld,expectedCOM),root.orientation.angleTo(expectedQ));
 near(root.comWorld,expectedCOM,2e-11,'every retained source COM knot uses one fixed frame');
 assert.ok(root.orientation.angleTo(expectedQ)<1e-7,'every source head knot, including full post-turn orientation, is retained');
 assert.ok(Math.abs(p.source.sourceTime-f.time)<2e-12,'packet source clock identifies each actual observation');
 const rate=derivative(i,f=>[f.headReference,...f.authorCOMOverL]),acceleration=derivative(i,f=>[f.headReference,...f.authorCOMOverL],2);
 const expectedV=new Vector3(rate[1],0,-rate[2]).multiplyScalar(L).applyQuaternion(fixed);
 const expectedA=new Vector3(acceleration[1],0,-acceleration[2]).multiplyScalar(L).applyQuaternion(fixed);
 const expectedOmega=Y.clone().applyQuaternion(fixed).multiplyScalar(rate[0]);
 const expectedAlpha=Y.clone().applyQuaternion(fixed).multiplyScalar(acceleration[0]);
 maxRootRateError=Math.max(maxRootRateError,maxError(root.comVelocityWorld,expectedV),maxError(root.omegaWorld,expectedOmega));
 maxRootAccelerationError=Math.max(maxRootAccelerationError,maxError(root.comAccelerationWorld,expectedA),maxError(root.alphaWorld,expectedAlpha));
 near(root.comVelocityWorld,expectedV,2e-9,'source COM derivatives survive at every knot');
 near(root.omegaWorld,expectedOmega,2e-9,'angular velocity uses fixed mapped Y');
 near(root.comAccelerationWorld,expectedA,2e-7,'source COM acceleration survives at every knot');
 near(root.alphaWorld,expectedAlpha,2e-7,'source angular acceleration survives at every knot');
 const expectedShape=cue.sample(age);
 for(const key of ['b','v','a']){
  const tolerance=key==='a'?2e-7:key==='v'?2e-9:2e-11;
  maxShapeKnotError=Math.max(maxShapeKnotError,maxError(p.shape[key],expectedShape[key]));
  near(p.shape[key],expectedShape[key],tolerance,'relative shape remains the unchanged observed cue');
 }
}
assert.ok(frames.at(-1).headReference>Math.PI+.15,'fixture contains the complete ~190.95 degree post-turn head orientation');
near(take(clip,clip.metadata.sourceStart).rootReference.comWorld,anchor,1e-12,'source first COM anchors exactly');
for(const time of [clip.metadata.markedOnset,clip.metadata.markedEnd])assert.equal(take(clip,time).source.measuredPhase,true,'exact marked boundaries are classified as measured despite floating point clocks');
assert.equal(take(clip,clip.metadata.sourceStart).source.observedRoot,true,'retained margins keep observed root samples');
assert.equal(take(clip,clip.metadata.sourceStart-.01).source.observedRoot,false,'authored root entry is labelled separately');

const mirrored=create({mirror:-1,anchorCOM:anchor,sourceFrameQuaternion:fixed});
const slower=create({duration:fullDuration*2,anchorCOM:anchor,sourceFrameQuaternion:fixed});
for(let i=0;i<=120;i++){
 const offset=fullDuration*i/120,t=clip.metadata.sourceStart+offset,p=take(clip,t);
 const m=take(mirrored,mirrored.metadata.sourceStart+offset),s=take(slower,slower.metadata.sourceStart+2*offset);
 for(const key of ['b','v','a']){
  near(m.shape[key],p.shape[key].map(x=>-x),2e-7,'mirroring reverses all relative shape channels');
  near(s.shape[key].map(x=>x*(key==='b'?1:key==='v'?2:4)),p.shape[key],2e-7,'duration scales shape derivatives');
 }
 const local=p.rootReference.comWorld.clone().sub(anchor).applyQuaternion(fixed.clone().invert());local.z*=-1;
 near(m.rootReference.comWorld,local.applyQuaternion(fixed).add(anchor),2e-11,'mirror reflects transverse COM in the fixed plane');
 const reflectedHead=fixed.clone().multiply(fixed.clone().invert().multiply(p.rootReference.orientation).invert());
 assert.ok(m.rootReference.orientation.angleTo(reflectedHead)<1e-7,'mirrored heading uses the same fixed reference plane');
 for(const key of ['comVelocityWorld','comAccelerationWorld']){
  const reflected=p.rootReference[key].clone().applyQuaternion(fixed.clone().invert());reflected.z*=-1;
  near(m.rootReference[key],reflected.applyQuaternion(fixed),2e-7,'mirroring reverses transverse path derivatives');
 }
 for(const key of ['omegaWorld','alphaWorld'])near(m.rootReference[key],p.rootReference[key].clone().multiplyScalar(-1),2e-7,'mirroring reverses angular derivatives');
 for(const [key,factor]of [['comWorld',1],['comVelocityWorld',2],['comAccelerationWorld',4],['omegaWorld',2],['alphaWorld',4]]){
  const expected=p.rootReference[key],actual=s.rootReference[key].clone();if(key!=='comWorld')actual.multiplyScalar(factor);
  near(actual,expected,2e-7,'duration scales root derivatives independently of the entry duration');
 }
 assert.ok(s.rootReference.orientation.angleTo(p.rootReference.orientation)<1e-7,'duration preserves corresponding orientations');
}
const Qstart=new Quaternion().setFromAxisAngle(new Vector3(1,2,3).normalize(),.63),aligned=create({initialHeadQuaternion:Qstart});
assert.ok(take(aligned,aligned.metadata.sourceStart).rootReference.orientation.angleTo(Qstart)<1e-7,'initial head option removes the nonzero first common-frame heading');

// Independent 29-region material transport, using the actual GLB neutral frame.
const totalMass=profile.bodies29.reduce((sum,r)=>sum+r.mass,0);
function worldCentroid(p){
 const sum=new Vector3();
 for(const region of profile.bodies29){
  const panel=region.regions[0],R=p.rotations[panel].clone().multiply(sampler.rest[panel].clone().invert()).normalize();
  const point=new Vector3().fromArray(region.com).sub(sampler.centers[panel]).applyQuaternion(R).add(p.positions[panel]);
  sum.addScaledVector(point,region.mass);
 }
 return sum.divideScalar(totalMass);
}
const posedCentroid=p=>worldCentroid(p).sub(p.rootReference.originWorld).applyQuaternion(p.rootReference.orientation.clone().invert());
const renderedCOM=p=>worldCentroid(p);
const assertCOM=p=>near(renderedCOM(p),p.rootReference.comWorld,1e-8,'rendered 29-region centroid equals the prescribed COM');
let maximumCentroidError=0,maximumBoneLengthError=0,maximumCranialRotationError=0,maximumNeutralCOMShift=0;
const neutralCOM=profile.bodies29.reduce((sum,r)=>sum.addScaledVector(new Vector3().fromArray(r.com),r.mass),new Vector3()).divideScalar(totalMass);
for(let i=0;i<=240;i++){
 const p=take(clip,clip.metadata.end*i/240);assert.equal(p.positions.length,34);assert.equal(p.rotations.length,34);assertCOM(p);
 maximumCentroidError=Math.max(maximumCentroidError,renderedCOM(p).distanceTo(p.rootReference.comWorld));
 maximumNeutralCOMShift=Math.max(maximumNeutralCOMShift,posedCentroid(p).distanceTo(neutralCOM));
 for(let j=1;j<34;j++)maximumBoneLengthError=Math.max(maximumBoneLengthError,Math.abs(p.positions[j].distanceTo(p.positions[j-1])-sampler.centers[j].distanceTo(sampler.centers[j-1])));
 for(let j=0;j<5;j++){
  const deformation=p.rotations[j].clone().multiply(sampler.rest[j].clone().invert()).normalize();
  maximumCranialRotationError=Math.max(maximumCranialRotationError,deformation.angleTo(p.rootReference.orientation));
  near(p.positions[j],sampler.centers[j].clone().applyQuaternion(p.rootReference.orientation).add(p.rootReference.originWorld),2e-12,'cranial material stations follow one rigid root transform');
 }
 assert.ok([...p.shape.b,...p.shape.v,...p.shape.a,...p.rootReference.comWorld.toArray(),...p.rootReference.originWorld.toArray()].every(Number.isFinite),'all sampled packets stay finite');
}
assert.ok(maximumBoneLengthError<1e-8,`all 33 material bone lengths remain fixed within native GLB quaternion precision: ${maximumBoneLengthError}`);
assert.ok(maximumCranialRotationError<1e-7,'cranial stations 0 through 4 remain rigid');
assert.ok(maximumNeutralCOMShift>.02,'fixture meaningfully distinguishes the posed and neutral centroids');

// Root references may feed only the explicit animation lane. They cannot
// change the relative shape cue; shape changes cannot rewrite observed roots.
const rootPoisoned=structuredClone(data),shapePoisoned=structuredClone(data);
for(const f of rootPoisoned.trials.fast.frames){
 f.headReference+=.27+.13*Math.sin(f.time*2);f.tailReference-=.41+.2*f.time;
 f.authorCOMOverL[0]+=.2*f.time;f.authorCOMOverL[1]-=.1*Math.sin(f.time*3);
}
for(const f of shapePoisoned.trials.fast.frames)f.b=f.b.map(x=>x*1.35);
const isolatedCue=createObservedTurnCue(rootPoisoned,{start:clip.metadata.sourceStart,duration:fullDuration});
const changedRoot=create({data:rootPoisoned,anchorCOM:anchor,sourceFrameQuaternion:fixed});
const changedShape=create({data:shapePoisoned,anchorCOM:anchor,sourceFrameQuaternion:fixed});
let rootDifference=0,shapeDifference=0,originDifference=0;
for(let i=0;i<=40;i++){
 const age=clip.metadata.sourceStart+fullDuration*i/40,p=take(clip,age),r=take(changedRoot,age),s=take(changedShape,age);
 const reference=cue.sample(age),isolated=isolatedCue.sample(age);
 assert.deepEqual(Object.keys(isolated).sort(),['a','b','v'],'motor cue API contains only relative shape channels');
 for(const key of ['b','v','a'])assert.deepEqual(isolated[key],reference[key],'observed root edits cannot enter relative shape commands');
 const rootKeys=['comWorld','comVelocityWorld','comAccelerationWorld','omegaWorld','alphaWorld'];
 for(const key of rootKeys)near(s.rootReference[key],p.rootReference[key],2e-10,'shape edits cannot change observed root trajectory');
 assert.ok(s.rootReference.orientation.angleTo(p.rootReference.orientation)<1e-7,'shape edits cannot change observed head orientation');
 rootDifference=Math.max(rootDifference,r.rootReference.comWorld.distanceTo(p.rootReference.comWorld));
 shapeDifference=Math.max(shapeDifference,maxError(s.shape.b,p.shape.b));
 originDifference=Math.max(originDifference,s.rootReference.originWorld.distanceTo(p.rootReference.originWorld));
 const savedShape=JSON.stringify({b:[...reference.b],v:[...reference.v],a:[...reference.a]});
 cue.reference(clip.metadata.sourceEnd-age/3);cue.sample(age);
 assert.equal(JSON.stringify(cue.sample(age)),savedShape,'reference lookups cannot change issued shape after arbitrary seeking');
}
assert.ok(rootDifference>.05&&shapeDifference>.1&&originDifference>.005,'isolation fixtures exercise root, body shape and posed-COM placement');
const originalReference=cue.reference(clip.metadata.markedOnset+.31),alteredReference=isolatedCue.reference(clip.metadata.markedOnset+.31);
assert.ok(Math.abs(originalReference.head-alteredReference.head)>.1,'root-channel perturbation also exercises the separate reference API');
assert.equal(pristine(),pristineBefore,'sampling must not mutate actual rig joints, neutral frame or fixed mass profile');

const packet=clip.createPacket(),paused=clip.sample(.61,packet),saved=snapshot(paused);
for(const time of [.13,clip.metadata.end+1,.92,-1,.61])clip.sample(time,packet);
assert.equal(snapshot(clip.sample(.61,packet)),saved,'arbitrary seeks and pause revisits are deterministic');
assert.equal(clip.sample(.61,packet),packet,'caller-owned packet storage is reused');

// At each source knot and authored boundary, all supplied rates approach one
// value as the independent observation interval is refined.
const joins=[0,clip.metadata.sourceStart,clip.metadata.sourceEnd,clip.metadata.end,clip.metadata.markedOnset,clip.metadata.markedEnd,...frames.map(f=>sourceAge(clip,f.time))];
function continuity(sample,whichJoins=joins){
 return [1e-5,5e-6,2.5e-6].map(h=>{
  const errors={position:0,velocity:0,acceleration:0,orientation:0,omega:0,alpha:0,shape:0,shapeVelocity:0,shapeAcceleration:0};
  for(const t of whichJoins){
   const a=sample(t-h),b=sample(t+h),ra=a.rootReference,rb=b.rootReference;
   for(const [key,fields]of [['position',['comWorld','originWorld']],['velocity',['comVelocityWorld','originVelocityWorld']],['acceleration',['comAccelerationWorld','originAccelerationWorld']],['omega',['omegaWorld']],['alpha',['alphaWorld']]])
    for(const field of fields)errors[key]=Math.max(errors[key],maxError(ra[field],rb[field]));
   errors.orientation=Math.max(errors.orientation,ra.orientation.angleTo(rb.orientation));
   for(const [key,field]of [['shape','b'],['shapeVelocity','v'],['shapeAcceleration','a']])errors[key]=Math.max(errors[key],maxError(a.shape[field],b.shape[field]));
  }
  return {h,...errors};
 });
}
function assertC2(rows){
 for(let i=1;i<rows.length;i++)for(const key of Object.keys(rows[i]).filter(key=>key!=='h')){
  const floor=key==='acceleration'?2e-5:key==='orientation'?1e-7:1e-8;
  assert.ok(rows[i][key]<Math.max(floor,rows[i-1][key]*.64),`${key} converges across all joins: ${JSON.stringify(rows)}`);
 }
}
const convergence=continuity(t=>take(clip,t));assertC2(convergence);
for(const join of joins)assertC2(continuity(t=>take(clip,t),[join]));
let maximumOriginVelocityDifference=0,maximumOriginAccelerationDifference=0;
for(const age of [.11,clip.metadata.sourceStart+.371,clip.metadata.sourceStart+.643,clip.metadata.sourceEnd+.17]){
 const h=2e-5,a=take(clip,age-h),p=take(clip,age),b=take(clip,age+h);
 const velocity=b.rootReference.originWorld.clone().sub(a.rootReference.originWorld).multiplyScalar(1/(2*h));
 const acceleration=b.rootReference.originWorld.clone().add(a.rootReference.originWorld).addScaledVector(p.rootReference.originWorld,-2).multiplyScalar(1/(h*h));
 maximumOriginVelocityDifference=Math.max(maximumOriginVelocityDifference,velocity.distanceTo(p.rootReference.originVelocityWorld));
 maximumOriginAccelerationDifference=Math.max(maximumOriginAccelerationDifference,acceleration.distanceTo(p.rootReference.originAccelerationWorld));
}
assert.ok(maximumOriginVelocityDifference<2e-6,'origin velocity independently differentiates actual origin placement');
assert.ok(maximumOriginAccelerationDifference<3e-3,'origin acceleration independently differentiates actual origin placement');
for(const age of [-1,0,clip.metadata.end,clip.metadata.end+1]){
 const p=take(clip,age);
 for(const key of ['comVelocityWorld','comAccelerationWorld','omegaWorld','alphaWorld','originVelocityWorld','originAccelerationWorld'])near(p.rootReference[key],new Vector3(),2e-7,'finite entry/exit reaches rest without clamped endpoint rates');
}

// Counterexamples establish that the independent oracles reject the three
// historical root-placement/endpoint errors, without changing the module.
const bent=take(clip,clip.metadata.markedOnset+.45),wrongNeutral=take(clip,clip.metadata.markedOnset+.45);
const oldOrigin=wrongNeutral.rootReference.originWorld.clone();
wrongNeutral.rootReference.originWorld.copy(wrongNeutral.rootReference.comWorld).sub(neutralCOM.clone().applyQuaternion(wrongNeutral.rootReference.orientation));
const wrongShift=wrongNeutral.rootReference.originWorld.clone().sub(oldOrigin);
wrongNeutral.positions.forEach(p=>p.add(wrongShift));
assert.throws(()=>assertCOM(wrongNeutral),assert.AssertionError,'neutral-COM placement must fail under this actual bend');
const wrongPath=bent.rootReference.comWorld.clone().sub(anchor).applyQuaternion(fixed.clone().invert()).applyQuaternion(bent.rootReference.orientation).add(anchor);
assert.throws(()=>near(wrongPath,bent.rootReference.comWorld,2e-10,'fixed path oracle'),assert.AssertionError,'rotating source path by the current head must fail');
const clamped=t=>{
 const age=Math.max(clip.metadata.sourceStart,Math.min(clip.metadata.sourceEnd,t)),p=take(clip,age);
 if(t<clip.metadata.sourceStart||t>clip.metadata.sourceEnd)for(const key of ['comVelocityWorld','comAccelerationWorld','omegaWorld','alphaWorld','originVelocityWorld','originAccelerationWorld'])p.rootReference[key].set(0,0,0);
 return p;
};
assert.throws(()=>assertC2(continuity(clamped,[clip.metadata.sourceStart,clip.metadata.sourceEnd])),assert.AssertionError,'old clamped endpoints must fail C2 refinement');

return {referenceAnimation:'passed',sourceKnots:frames.length,maximumRootKnotError:maxRootKnotError,maximumRootRateError:maxRootRateError,
 maximumRootAccelerationError:maxRootAccelerationError,maximumShapeKnotError:maxShapeKnotError,maximumCentroidError,maximumNeutralCOMShift,maximumBoneLengthError,
 maximumCranialRotationError,maximumOriginVelocityDifference,maximumOriginAccelerationDifference,convergence,negativeFixtures:['neutral COM','current-head path rotation','clamped source endpoints']};
}

const report=runAssertions();
// Mutations execute altered production modules in memory. Their only expected
// rejection is a behavioral assertion from the same actual-asset harness.
const moduleURL=new URL('../static/aquarium-reference-motion.js',import.meta.url);
const productionSource=await readFile(moduleURL,'utf8');
const mutations={
 neutralCOM:source=>source.replace('moment.copy(c).applyQuaternion(head)',
  'moment.copy(profile.bodies29.reduce((sum,r)=>sum.addScaledVector(new Vector3(...r.com),r.mass),new Vector3()).divideScalar(mass)).applyQuaternion(head)'),
 currentHeadPath:source=>source.replace('temp.set(s.p[1]-firstCOM[0],0,s.p[2]-firstCOM[1]).applyQuaternion(fixedFrame).add(anchorCOM);',
  'temp.set(s.p[1]-firstCOM[0],0,s.p[2]-firstCOM[1]).applyQuaternion(fixedFrame.clone().multiply(new Quaternion().setFromAxisAngle(Y,s.p[0]))).add(anchorCOM);'),
 clampedEndpoints:source=>source.replace("if(age<=start){for(const key of ['p','v','a'])root[key].splice(0,4,...before[key]);region='held-before';}",
  "if(age<sourceStart){root.p.splice(0,4,...first.p);root.v.fill(0);root.a.fill(0);region='held-before';}")
  .replace("else if(age<end){evaluate(exit,age-sourceEnd,exitDuration,root);region='root-exit';}",
   "else if(age>sourceEnd){root.p.splice(0,4,...last.p);root.v.fill(0);root.a.fill(0);region='held-after';}"),
};
const rejectedMutants=[];
for(const [name,mutate]of Object.entries(mutations)){
 const source=mutate(productionSource);
 assert.notEqual(source,productionSource,`test injector must apply the ${name} fault`);
 const resolved=source.replace(/from (['"])(\.{1,2}\/[^'"]+)\1/g,(_,quote,path)=>'from '+JSON.stringify(new URL(path,moduleURL).href));
 const mutant=await import('data:text/javascript;base64,'+Buffer.from(resolved).toString('base64'));
 assert.throws(()=>runAssertions(mutant.createReferenceAnimationClip),assert.AssertionError,`${name} must fail the actual invariant harness`);
 rejectedMutants.push(name);
}
console.log(JSON.stringify({...report,rejectedMutants}));
