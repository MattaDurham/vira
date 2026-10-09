// Inspect the production render geometry using the bundled Three.js math.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {fishGeometry,renderSize,waterGeometry} from '../static/koi-pond-3d.js';
const require=createRequire(import.meta.url),model=require('../static/koi-pond.js');
const basin=waterGeometry(model.space.depth),normals=basin.attributes.normal,position=basin.attributes.position;
assert.ok(Math.abs(Math.min(...Array.from({length:position.count},(_,i)=>position.getY(i)))+1.8)<1e-6);
for(let i=0;i<normals.count;i++)assert.ok(normals.getY(i)>.01,'the floor must face upward and populate the underwater depth target');
// The refracted floor must be sampled locally; four distant corner vertices
// would interpolate a false shallow surface in front of the actual fish.
for(const x of [-2.5,-1.25,0,1.25,2.5])for(const z of [-2,-.5,1,2]){
  let nearest=Infinity;
  for(let i=0;i<position.count;i++)nearest=Math.min(nearest,Math.hypot(position.getX(i)-x,position.getZ(i)-z));
  assert.ok(nearest<.75,'floor vertices resolve the occupied swimming area for refracted occlusion');
}
const pond=model.create(()=>.5),f=pond.fish[0];Object.assign(f,{heading:0,bank:0,pitch:0,depth:.7});
const shape=fishGeometry(model,f),points=shape.attributes.position,skinNormals=shape.attributes.normal;
for(let ring=1;ring<24;ring++){
  const s=ring/24*.83,center=model.bodyPoint(f,s);
  for(let j=0;j<18;j++){
    const i=ring*19+j;
    const dot=(points.getX(i)-center.x)*skinNormals.getX(i)+(points.getY(i)-center.y)*skinNormals.getY(i)+(points.getZ(i)-center.z)*skinNormals.getZ(i);
    assert.ok(dot>0,'every flank normal must point out of the fish, rather than disappear under backface culling');
  }
}
assert.equal(model.anatomy.vertebrae,34);
assert.equal(model.segments.filter(s=>s.region==='skull').length,1);
assert.deepEqual(['abdominal','transition','caudal'].map(region=>model.segments.filter(s=>s.region===region).length),[11,6,17]);
let seed=371;
const swimming=model.create(()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;});
for(let i=0;i<2400;i++){
  if(i%800===0)swimming.flick(.5,.5);
  const before=swimming.fish.map(f=>f.depth);swimming.step(.025);
  for(let k=0;k<swimming.fish.length;k++){
    const fish=swimming.fish[k];
    assert.ok(Math.abs(fish.depth-before[k])<=.42*.025+1e-9,'basin collision cannot teleport a fish upward');
    if(i%20===0){
      const hull=model.volumeMesh(fish).positions;
      for(let j=0;j<hull.length;j+=3)assert.ok(hull[j+1]>=model.floorAt(hull[j],hull[j+2])-.002,'the whole bending hull must stay above the solid pond floor');
      const bone=model.skeletalMesh(fish);
      assert.ok(bone.positions.every(Number.isFinite));
      assert.ok(bone.indices.every(i=>i<bone.positions.length/3));
    }
  }
}
const still=model.create(()=>.5);still.fish.splice(1);
Object.assign(still.fish[0],{x:.5,y:.5,depth:.9,depthVelocity:0,pitch:0,pitchVelocity:0,vx:0,vy:0,drive:0,pace:0,mode:'hover',timer:100,targetDepth:.2});
for(let i=0;i<240;i++)still.step(1/120);
assert.equal(still.fish[0].depth,.9,'a relaxed fish has no independent elevator force');
const climbing=model.create(()=>.5);climbing.fish.splice(1);
Object.assign(climbing.fish[0],{x:.5,y:.5,depth:1.4,depthVelocity:0,pitch:0,pitchVelocity:0,vx:0,vy:0});
Object.assign(climbing.fish[0],{target:{x:.8,y:.5},targetDepth:.2,pace:.05,mode:'cruise',timer:100});let traveled=0,wasPitched=false;
for(let i=0;i<720;i++){
  const start=model.world(climbing.fish[0]);climbing.step(1/120);const fish=climbing.fish[0],end=model.world(fish);
  traveled+=Math.hypot(end.x-start.x,end.z-start.z);
  if(fish.depthVelocity<-.05){wasPitched=true;assert.ok(model.bodyPoint(fish,.05).y>model.bodyPoint(fish,.75).y,'a climbing fish raises its head, not just its centre');}
}
assert.ok(wasPitched && climbing.fish[0].depth<1,'the tail must actually propel the fish upward');
assert.ok(traveled>1.4-climbing.fish[0].depth,'a climb is a swimming path with substantial forward travel');
basin.dispose();shape.dispose();
console.log('Outward render normals, anatomical regions, solid basin collisions and thrust-driven climbs pass.');

const fourK=renderSize(3840,2160,1);
assert.equal(fourK.width,3840);assert.equal(fourK.height,2160,'4K monitors must render at native resolution');
const retina=renderSize(1920,1080,2);
assert.equal(retina.width,3840);assert.equal(retina.height,2160,'retina desktops retain fine scale detail');
const huge=renderSize(7680,4320,2);
assert.ok(huge.width*huge.height<=8388608,'extreme monitors retain a bounded offscreen budget');
const limited=renderSize(3840,2160,2,2048);
assert.ok(limited.width<=2048 && limited.height<=2048,'targets respect the graphics device texture limit');
const surface=waterGeometry(),surfacePoints=surface.attributes.position;
assert.ok(Math.min(...Array.from({length:surfacePoints.count},(_,i)=>surfacePoints.getX(i)))<-50);
assert.ok(Math.max(...Array.from({length:surfacePoints.count},(_,i)=>surfacePoints.getZ(i)))>50,'water covers the camera frustum rather than the roaming ellipse');
surface.dispose();
