import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {Vector3,PerspectiveCamera,Texture,WebGPUCoordinateSystem,WebGLCoordinateSystem} from '../static/vendor/three.webgpu.js';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {applySwimPose} from '../static/aquarium-rig.js';
import {projectResearchPoint,researchFinAnchor,researchCaudalContour,researchJointPitch} from '../static/aquarium-research.js';
import {createSwimmer} from '../static/aquarium-motion.js';
import {createCaudalFrame,createCaudalMotion,bindCaudalGeometry,deformCaudalGeometry} from '../static/aquarium-caudal.js';
import {researchTopics,buildChapters} from '../static/aquarium-research-data.js';
import {createResearchTrace,traceLimit,createPitchResearchTrace} from '../static/aquarium-research-trace.js';

const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
const loader=new GLTFLoader().register(()=>({name:'GeometryOnlyImages',loadTexture:()=>Promise.resolve(new Texture())}));
const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
const joints=[];asset.traverse(o=>{if(o.isBone&&o.name.startsWith('spine_'))joints.push(o);});
joints.sort((a,b)=>a.name.localeCompare(b.name));const rest=joints.map(b=>b.quaternion.clone());
const pectoral=asset.getObjectByName('Pectoral_membrane'),anchor=researchFinAnchor(asset,'Pectoral_membrane');
assert.ok(pectoral?.isSkinnedMesh,'annotations attach to the actual exported fin');
for(const system of [WebGPUCoordinateSystem,WebGLCoordinateSystem])for(const [width,height] of [[1450,1552],[1100,800],[402,852]]){
  const camera=new PerspectiveCamera(40,width/height,.06,25);camera.coordinateSystem=system;
  camera.position.set(-.6,1.2,4.1);camera.lookAt(0,.8,-.2);camera.updateProjectionMatrix();camera.updateMatrixWorld();
  camera.setViewOffset(width,height,width>650?179:0,width<=650?100:0,width,height);
  let previous;
  for(const pose of [{x:-.3,y:.9,z:-.1,yaw:.3,pitch:.2},{x:.4,y:.6,z:-.7,yaw:-.8,pitch:-.18}]){
    applySwimPose(asset,joints,rest,{...pose,phase:1.7,age:3,bank:.08,stroke:.6,speed:.2,yawRate:.12,pitchRate:.04});
    assert.ok(Math.abs(researchJointPitch(joints[1])-pose.pitch)<1e-7,
      'head pitch comes from the actual posed bone with neutral arch removed, independent of yaw and bank');
    const actualFin=pectoral.getVertexPosition(0,new Vector3()).applyMatrix4(pectoral.matrixWorld);
    assert.ok(anchor().distanceTo(actualFin)<1e-12,'the fin label follows its deformed membrane rather than an approximate local point');
    const world=joints[17].getWorldPosition(new Vector3()),screen=projectResearchPoint(world,camera,width,height);
    assert.ok(screen.visible,'an annotation on the shipped moving spine stays inside the current view');
    // Reconstruct its viewing ray using the browser camera. A label computed
    // from a rest/local point would miss the actual articulated joint here.
    const ray=new Vector3(screen.x/width*2-1,1-screen.y/height*2,.5).unproject(camera).sub(camera.position).normalize();
    const expected=world.clone().sub(camera.position).normalize();
    assert.ok(ray.distanceTo(expected)<1e-10,'the overlay and rendered joint share the exact viewing ray');
    if(previous)assert.ok(Math.hypot(screen.x-previous.x,screen.y-previous.y)>20,'annotations follow actual swimming and depth rather than a fixed picture');
    previous=screen;
  }
  assert.equal(projectResearchPoint(camera.position.clone().add(camera.getWorldDirection(new Vector3()).multiplyScalar(-1)),camera,width,height).visible,false,'features behind the camera cannot leave visible overlay artifacts');
  const above=projectResearchPoint(camera.position.clone().add(camera.getWorldDirection(new Vector3()).multiplyScalar(2)).add(new Vector3(0,100,0).applyQuaternion(camera.quaternion)),camera,width,height);
  assert.equal(above.visible,false,'offscreen features do not receive visible pins');
  assert.equal(above.inFront,true,'a true projected depth segment can continue toward the offscreen surface');
}
const ids=new Set(researchTopics.map(topic=>topic.id));
const caudal=asset.getObjectByName('Caudal_membrane'),contour=researchCaudalContour(asset);
const base=Float32Array.from(caudal.geometry.attributes.position.array),uv=caudal.geometry.attributes.uv;
const binding=bindCaudalGeometry(base,createCaudalFrame(base,uv.array));
const edge=Array.from({length:uv.count},(_,i)=>i).filter(i=>uv.getY(i)<.001).sort((a,b)=>uv.getX(a)-uv.getX(b));
let previousContour;
for(const controls of [{speed:.2,stroke:1,phase:1},{speed:.2,stroke:0,phase:1},{speed:.2,stroke:0,brake:1,phase:1}]){
  deformCaudalGeometry(binding,createCaudalMotion({...controls,poseHistory:false}),caudal.geometry.attributes.position.array);
  const actual=contour();assert.equal(actual.length,39,'the outline follows the complete exported distal tail edge');
  actual.forEach((p,i)=>assert.ok(p.distanceTo(caudal.getVertexPosition(edge[i],new Vector3()).applyMatrix4(caudal.matrixWorld))<1e-12,
    'tail annotations follow the same deformed and skinned membrane as rendering'));
  if(previousContour)assert.ok(actual.some((p,i)=>p.distanceTo(previousContour[i])>.004),
    'the research contour changes with actual kick, glide and braking shapes');
  previousContour=actual;
}
assert.equal(ids.size,6);
for(const topic of researchTopics){
  assert.ok(topic.claim&&topic.implementation&&topic.limitation&&topic.sources.length,'every observation has evidence and its implementation scope');
  topic.sources.forEach(source=>assert.equal(new URL(source.url).protocol,'https:'));
}
buildChapters.forEach(chapter=>chapter.topics.forEach(id=>assert.ok(ids.has(id),'story links resolve to actual cited research topics')));
const history=createResearchTrace();
const sample=Array.from({length:34},()=>({lateral:0,vertical:0,twist:0}));
sample[24]={lateral:traceLimit,vertical:-traceLimit/2,twist:.1};
sample[33]={lateral:-traceLimit,vertical:traceLimit/2,twist:-.1};
history.record(0,sample);history.record(0,sample);
assert.equal(history.samples.length,1,'paused renders cannot invent motion history');
assert.equal(history.paths(0,0,0),'M286.00,14.00','the plotted trunk sample uses its actual angular value on a fixed scale');
assert.equal(history.paths(0,1,0),'M286.00,36.00','the dotted caudal trace samples a separate station');
assert.equal(history.paths(1,0,0),'M286.00,60.50','vertical orientation retains its own signed axis');
history.record(.05,sample);history.record(1,sample);
assert.equal((history.paths(0,0,1).match(/M/g)||[]).length,2,'unobserved gaps do not become invented continuous motion');
history.record(4.1,sample);
assert.ok(history.samples.every(s=>s.age>=.1),'the history expires by simulation time');
history.record(.01,sample);
assert.equal(history.samples.length,1,'restarting the swimmer clears the previous trajectory');
history.clear();assert.equal(history.paths(0,0,0),'','closing/reopening starts an honest new observation window');
const pitchHistory=createPitchResearchTrace(),swimmer=createSwimmer(()=>.5);
Object.assign(swimmer.fish,{x:.6,y:.84,z:-.205,yaw:0});swimmer.navigate({x:-.9,y:1.18,z:-.205});
for(let tick=0;tick<60;tick++){
  swimmer.step(1/120);
  if(tick%6)continue;
  const fish=swimmer.fish;applySwimPose(asset,joints,rest,fish);
  const head=researchJointPitch(joints[1]),tail=researchJointPitch(joints.at(-1));
  pitchHistory.record(fish.age,fish.pitchFin,head,tail);
  assert.deepEqual(pitchHistory.samples.at(-1).values,[fish.pitchFin,head,tail],
    'the causal chart retains the actual shared fin command and two separately posed world frames');
  assert.ok(Math.abs(head-fish.pitch)<1e-7,'a rigid skull reports live head rotation, not its zero axial offset');
}
assert.ok(pitchHistory.samples.some(s=>Math.abs(s.values[1]-s.values[2])>.01),
  'the chart exposes head and posterior follow-through as distinct real rig signals');
const count=pitchHistory.samples.length,last=pitchHistory.samples.at(-1);
pitchHistory.record(last.age,0,0,0);assert.equal(pitchHistory.samples.length,count,'pausing does not replace live pitch history with new values');
pitchHistory.clear();pitchHistory.record(0,.15,Math.PI/6,-Math.PI/6);
assert.equal(pitchHistory.paths(0,0),'M286.00,13.00','fin command has a distinct fixed 8.6-degree scale');
assert.equal(pitchHistory.paths(1,0),'M286.00,49.00');
assert.equal(pitchHistory.paths(2,0),'M286.00,73.00','head and tail use the same signed 30-degree attitude scale');
pitchHistory.record(.05,0,0,0);pitchHistory.record(1,0,0,0);
assert.equal((pitchHistory.paths(0,1).match(/M/g)||[]).length,2,'unobserved pitch gaps are not connected');
pitchHistory.record(4.1,0,0,0);assert.ok(pitchHistory.samples.every(s=>s.age>=.1));
pitchHistory.record(.01,0,0,0);assert.equal(pitchHistory.samples.length,1,'rewinding clears all three causal signals together');
console.log('Research overlay: actual rig/camera attachment and cited story links passed.');
