import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {Vector3,PerspectiveCamera,Texture,WebGPUCoordinateSystem,WebGLCoordinateSystem} from '../static/vendor/three.webgpu.js';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {applySwimPose} from '../static/aquarium-rig.js';
import {projectResearchPoint,researchFinAnchor} from '../static/aquarium-research.js';
import {researchTopics,buildChapters} from '../static/aquarium-research-data.js';

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
}
const ids=new Set(researchTopics.map(topic=>topic.id));
assert.equal(ids.size,6);
for(const topic of researchTopics){
  assert.ok(topic.claim&&topic.implementation&&topic.limitation&&topic.sources.length,'every observation has evidence and its implementation scope');
  topic.sources.forEach(source=>assert.equal(new URL(source.url).protocol,'https:'));
}
buildChapters.forEach(chapter=>chapter.topics.forEach(id=>assert.ok(ids.has(id),'story links resolve to actual cited research topics')));
console.log('Research overlay: actual rig/camera attachment and cited story links passed.');
