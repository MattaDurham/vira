import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {performance} from 'node:perf_hooks';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,BufferGeometry,Float32BufferAttribute,Uint16BufferAttribute,Vector3} from '../static/vendor/three.webgpu.js';
import {createFinNormalSmoother} from '../static/aquarium-fin-normals.js';
import {createFinFrame,createFinMotion,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';

const radiansToDegrees=180/Math.PI;
function angle(a,b){
  const length=Math.hypot(...a)*Math.hypot(...b);
  assert.ok(length>0,'surface normals remain nonzero');
  return Math.acos(Math.max(-1,Math.min(1,a.reduce((sum,value,i)=>sum+value*b[i],0)/length)))*radiansToDegrees;
}
const vector=(attribute,i)=>[attribute.getX(i),attribute.getY(i),attribute.getZ(i)];
const maxSeamAngle=(geometry,pairs)=>Math.max(...pairs.map(([a,b])=>angle(vector(geometry.attributes.normal,a),vector(geometry.attributes.normal,b))));
const assertSmooth=(geometry,pairs)=>assert.ok(maxSeamAngle(geometry,pairs)<=.1,'authored smooth UV seams stay smooth after deformation');
const snapshot=geometry=>Object.fromEntries(['uv','skinIndex','skinWeight'].map(key=>[key,geometry.attributes[key].array.slice()]).concat([['index',geometry.index.array.slice()]]));
function assertTopology(geometry,before){
  for(const key of ['uv','skinIndex','skinWeight'])assert.deepEqual(geometry.attributes[key].array,before[key],`${key} remains unchanged`);
  assert.deepEqual(geometry.index.array,before.index,'triangle topology remains unchanged');
}

const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
const {scene}=await new GLTFLoader().register(()=>({name:'GeometryOnlyImages',loadTexture:()=>Promise.resolve(new Texture())}))
  .parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
const meshes=[];scene.traverse(mesh=>{if(mesh.isMesh&&/^(Pectoral|Pelvic|Caudal)_/.test(mesh.name))meshes.push(mesh);});
const timings=[],results=[];
for(const type of ['Pectoral','Pelvic','Caudal']){
  const mesh=meshes.find(item=>item.name===`${type}_branched_rays`),geometry=mesh.geometry.clone();
  assert.ok(geometry.index,'the shipped ray tubes retain indexed topology');
  const original=geometry.attributes.normal.clone(),base=geometry.attributes.position.array.slice(),before=snapshot(geometry);
  // Identify the actual wrapped UV seam independently of the smoother's cache.
  const positions=new Map(),pairs=[];
  for(let i=0;i<geometry.attributes.position.count;i++){
    const key=vector(geometry.attributes.position,i).join(',');
    if(!positions.has(key))positions.set(key,[]);positions.get(key).push(i);
  }
  for(const indices of positions.values())for(let a=0;a<indices.length;a++)for(let b=a+1;b<indices.length;b++){
    const i=indices[a],j=indices[b],uv=geometry.attributes.uv;
    const wraps=Math.abs(uv.getX(i)-uv.getX(j))>.99||Math.abs(uv.getY(i)-uv.getY(j))>.99;
    if(wraps&&angle(vector(original,i),vector(original,j))<.001)pairs.push([i,j]);
  }
  assert.ok(pairs.length>(type==='Pectoral'?1000:500),'the regression covers the real tube seams, not an empty selection');
  const reconcile=createFinNormalSmoother(geometry);
  geometry.computeVertexNormals();
  const broken=maxSeamAngle(geometry,pairs);
  assert.ok(broken>44&&broken<46,'ordinary normal recomputation reproduces the real forty-five-degree shading seam');
  // Mutation: skipping reconciliation must fail on this same exported asset.
  assert.throws(()=>assertSmooth(geometry,pairs),/authored smooth UV seams/);
  const normalBuffer=geometry.attributes.normal.array;
  assert.equal(reconcile(normalBuffer),normalBuffer,'the raw normal buffer is reused in place');
  assert.deepEqual(geometry.attributes.position.array,base,'neutral reconciliation does not move exported vertices');
  assertSmooth(geometry,pairs);
  const neutralError=Math.max(...pairs.flatMap(pair=>pair.map(i=>angle(vector(original,i),vector(geometry.attributes.normal,i)))));
  assert.ok(neutralError<.2,`neutral seam normals remain within 0.2 degrees of the authored surface (${neutralError})`);
  const frames={};
  for(const tissue of meshes.filter(item=>item.name.startsWith(type+'_membrane'))){
    const frame=createFinFrame(tissue.geometry.attributes.position.array,tissue.geometry.attributes.uv.array);frames[frame.side]=frame;
  }
  const binding=type==='Caudal'?null:bindFinGeometry(base,frames),poses=[];
  for(const phase of [.7,3.4]){
    const fish={age:2,finPhase:phase,forwardSpeed:.04,speed:.04,stroke:.3,muscle3:.3,brake:.7,yawRate:.2,pitchRate:.1,pitch:.1};
    if(type==='Caudal'){
      // The live caudal field bends its actual exported ray tubes in bind space.
      for(let i=0;i<geometry.attributes.position.count;i++){
        const x=base[i*3],y=base[i*3+1],z=base[i*3+2];
        geometry.attributes.position.setXYZ(i,x,y,z+.035*fish.stroke*Math.sin(x*12-phase-1.1)*Math.max(0,x-.48));
      }
    }else{
      const motions=Object.fromEntries([-1,1].map(side=>[side,createFinMotion(fish,side,type==='Pectoral')]));
      deformFinGeometry(binding,motions,geometry.attributes.position.array);
    }
    const deformedPositions=geometry.attributes.position.array.slice();
    geometry.computeVertexNormals();reconcile();assertSmooth(geometry,pairs);
    assert.deepEqual(geometry.attributes.position.array,deformedPositions,'normal reconciliation does not alter the production fin deformation');
    for(const i of new Set(pairs.flat()))assert.ok(Math.abs(new Vector3().fromBufferAttribute(geometry.attributes.normal,i).length()-1)<1e-5,'reconciled surface normals remain unit length');
    poses.push(geometry.attributes.normal.clone());
  }
  const changed=Math.max(...pairs.flatMap(pair=>pair.map(i=>angle(vector(poses[0],i),vector(poses[1],i)))));
  assert.ok(changed>.5,`the production fin deformation updates normal directions rather than freezing authored normals (${type}: ${changed} degrees)`);
  assertTopology(geometry,before);
  for(let i=0;i<100;i++)reconcile();
  const start=performance.now();for(let i=0;i<1000;i++)reconcile();
  timings.push((performance.now()-start)/1000);
  results.push({type,seams:pairs.length,brokenDegrees:broken,neutralErrorDegrees:neutralError,poseChangeDegrees:changed});
}

// Coincidence alone must not smooth a crease or join distinct skin influences.
const fixture=new BufferGeometry();
fixture.setAttribute('position',new Float32BufferAttribute([0,0,0,1,0,0,2,0,0,0,0,0,1,0,0,2,0,0],3));
fixture.setAttribute('normal',new Float32BufferAttribute([0,0,1,0,0,1,0,0,1,0,1,0,0,0,1,0,0,1],3));
fixture.setAttribute('skinIndex',new Uint16BufferAttribute([0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,1,0,0,0,0,0,0,0],4));
fixture.setAttribute('skinWeight',new Float32BufferAttribute(Array.from({length:6},()=>[1,0,0,0]).flat(),4));
const reconcileFixture=createFinNormalSmoother(fixture),normal=fixture.attributes.normal;
normal.array.set([1,0,0,1,0,0,1,0,0,0,0,1,0,1,0,0,1,0]);
reconcileFixture();
assert.deepEqual(vector(normal,0),[1,0,0]);assert.deepEqual(vector(normal,3),[0,0,1],'an authored ninety-degree crease remains sharp');
assert.deepEqual(vector(normal,1),[1,0,0]);assert.deepEqual(vector(normal,4),[0,1,0],'different nonzero skin influences remain independent');
assert.ok(angle(vector(normal,2),vector(normal,5))<.001,'the eligible fixture pair is reconciled');
console.log(JSON.stringify({results,reconcileMillisecondsPerFrame:timings.reduce((sum,value)=>sum+value,0)}));
