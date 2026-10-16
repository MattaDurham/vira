import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Quaternion} from '../static/vendor/three.webgpu.js';
import {createSwimmer,finPitchMoment} from '../static/aquarium-motion.js';
import {applySwimPose,createFacialRig} from '../static/aquarium-rig.js';
import {createContainment} from '../static/aquarium-collision.js';
import {createFinController,createFinMotion,createFinFrame,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';
import {createCaudalController,createCaudalFrame,bindCaudalGeometry,deformCaudalGeometry} from '../static/aquarium-caudal.js';
import {createStudyRandom} from '../static/aquarium-study.js';
import {tankCenterZ} from '../static/aquarium-space.js';

const dt=1/120;
const neutral={x:0,y:0,z:0,yaw:0,pitch:0,bank:0,age:0,phase:0,stroke:0,
  pitchRate:0,yawRate:0,pitchFin:0,speed:0,forwardSpeed:0,
  lateralGain:0,verticalGain:0,twistGain:0};
const controllers=()=>({pectoral:{'-1':createFinController(-1,true),'1':createFinController(1,true)},
  pelvic:{'-1':createFinController(-1,false),'1':createFinController(1,false)},caudal:createCaudalController()});
const sample=(control,fish)=>({pectoral:{'-1':control.pectoral[-1].sample(fish),'1':control.pectoral[1].sample(fish)},
  pelvic:{'-1':control.pelvic[-1].sample(fish),'1':control.pelvic[1].sample(fish)},caudal:control.caudal.sample(fish)});
const held=fish=>Object.fromEntries(['pectoral','pelvic'].map(kind=>[kind,
  Object.fromEntries([-1,1].map(side=>[side,createFinMotion(fish,side,kind==='pectoral')]))]));

async function geometry(deform){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'PitchGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const joints=[],fins=[];
  asset.traverse(mesh=>{
    if(mesh.isBone&&mesh.name.startsWith('spine_'))joints.push(mesh);
    if(mesh.isSkinnedMesh&&/^(Pectoral|Pelvic|Caudal)_/.test(mesh.name))fins.push({mesh,
      base:Float32Array.from(mesh.geometry.attributes.position.array),paired:!mesh.name.startsWith('Caudal'),
      pectoral:mesh.name.startsWith('Pectoral')});
  });
  joints.sort((a,b)=>a.name.localeCompare(b.name));
  assert.equal(joints.length,34,'pitch control retains the actual axial skeleton');
  const rest=joints.map(joint=>joint.quaternion.clone()),face=createFacialRig(asset);
  for(const pectoral of [true,false]){
    const family=fins.filter(fin=>fin.paired&&fin.pectoral===pectoral);
    const frames=Object.fromEntries(family.filter(fin=>fin.mesh.name.includes('membrane')).map(fin=>{
      const frame=createFinFrame(fin.base,fin.mesh.geometry.attributes.uv.array);return [frame.side,frame];
    }));
    for(const fin of family)fin.binding=bindFinGeometry(fin.base,frames);
  }
  const tail=fins.find(fin=>fin.mesh.name.startsWith('Caudal_membrane'));
  const tailFrame=createCaudalFrame(tail.base,tail.mesh.geometry.attributes.uv.array);
  for(const fin of fins.filter(fin=>!fin.paired))fin.binding=bindCaudalGeometry(fin.base,tailFrame);
  const membranes=fins.filter(fin=>fin.paired&&fin.mesh.name.includes('membrane'));
  function apply(fish,motions){
    for(const fin of fins){
      const output=fin.mesh.geometry.attributes.position.array;
      if(fin.paired)deform(fin.binding,motions[fin.pectoral?'pectoral':'pelvic'],output);
      else if(motions.caudal)deformCaudalGeometry(fin.binding,motions.caudal,output);
    }
    applySwimPose(asset,joints,rest,fish);face.pose(fish);
  }
  function incidence(motions){
    apply(neutral,motions);
    return membranes.map(({mesh,pectoral,base})=>{
      mesh.skeleton.update();
      const normal=new Vector3(),a=new Vector3(),b=new Vector3(),c=new Vector3(),indices=mesh.geometry.index.array;
      for(let i=0;i<indices.length;i+=3){
        mesh.getVertexPosition(indices[i],a).applyMatrix4(mesh.matrixWorld);
        mesh.getVertexPosition(indices[i+1],b).applyMatrix4(mesh.matrixWorld);
        mesh.getVertexPosition(indices[i+2],c).applyMatrix4(mesh.matrixWorld);
        normal.add(b.sub(a).cross(c.sub(a)));
      }
      normal.normalize();if(normal.y<0)normal.negate();
      return {pectoral,side:Math.sign(base[2]),angle:Math.atan2(normal.x,normal.y),normal:normal.toArray()};
    });
  }
  const noIncidence=motions=>Object.fromEntries(['pectoral','pelvic'].map(kind=>[kind,
    Object.fromEntries([-1,1].map(side=>[side,{...motions[kind][side],incidence:0}]))]));
  const changes=motions=>{
    const zero=incidence(noIncidence(motions)),actual=incidence(motions);
    return actual.map((value,i)=>({...value,change:value.angle-zero[i].angle}));
  };
  apply(neutral,held(neutral));
  const stations=[0,14,24,33],origins=stations.map(i=>joints[i].getWorldQuaternion(new Quaternion()).invert());
  const pitches=()=>stations.map((i,j)=>{
    const direction=new Vector3(1,0,0).applyQuaternion(joints[i].getWorldQuaternion(new Quaternion()).multiply(origins[j]));
    return -Math.asin(Math.max(-1,Math.min(1,direction.y)));
  });
  return {asset,apply,changes,pitches,containment:createContainment(asset)};
}

// Geometry and locomotion are injectable for regression mutations. All angles,
// gains and timing bounds describe the authored model, not measured koi data.
export async function assertPitchControl({create=createSwimmer,deform=deformFinGeometry}={}){
  const model=await geometry(deform),report={mirrored:[],onsets:[],maximumCorrection:0};
  for(const sign of [-1,1]){
    const fish={...neutral,age:2,speed:.14,forwardSpeed:.14,stroke:.4,muscle3:.4,
      finPhase:2.1,pitchFin:sign*.12};
    const changes=model.changes(held(fish));
    for(const fin of changes){
      const expected=sign*(fin.pectoral?1:-1);
      assert.ok(fin.change*expected>(fin.pectoral?.03:.001),
        'the actual paired membrane reverses signed neutral-relative incidence for ascent and descent');
    }
    const moment=finPitchMoment(fish);
    assert.ok(moment*sign>.5,'the same signed presentations create the corresponding authored pitch moment');
    report.mirrored.push({sign,moment,changes});
  }
  for(let i=0;i<4;i++)assert.ok(Math.abs(report.mirrored[0].changes[i].change+report.mirrored[1].changes[i].change)<.02,
    'opposite signed commands produce comparable opposite foil presentations on each actual side');

  for(const sign of [-1,1]){
    const swimmer=create(()=>.5),control=controllers();
    Object.assign(swimmer.fish,{x:0,y:.88,z:tankCenterZ,yaw:0,pitch:0});
    swimmer.navigate({x:-.85,y:.88+sign*.3,z:tankCenterZ});
    sample(control,swimmer.fish);
    const onset={fin:null,head:null,trunk:null,posterior:null},keys=['head','trunk','posterior'];
    let previousAngle=0,previousRate=0,largestMomentError=0;
    for(let frame=0;frame<120;frame++){
      swimmer.step(dt);const fish=swimmer.fish,motions=sample(control,fish);
      const changes=model.changes(motions).filter(fin=>fin.pectoral);
      if(onset.fin===null&&changes.every(fin=>fin.change*sign>.001))onset.fin=fish.age;
      model.apply(fish,motions);
      const angles=model.pitches(),rate=(angles[0]-previousAngle)/dt;
      // Read the actual skull pose, then infer angular effort from its change.
      // This rejects control fields that never rotate the exported skeleton.
      const moment=(rate-previousRate)/dt+3*previousRate;
      largestMomentError=Math.max(largestMomentError,Math.abs(moment-fish.pitchMoment));
      assert.ok(Math.abs(rate-fish.pitchRate)<1e-6,'the moment accelerates the actual skull rather than only a diagnostic field');
      for(const [i,key] of keys.entries()){
        const angle=angles[[0,1,3][i]];
        if(onset[key]===null&&angle*sign>.005)onset[key]=fish.age;
      }
      previousAngle=angles[0];previousRate=rate;
    }
    assert.ok(largestMomentError<1e-4,'actual pitch acceleration follows the neutral-relative fin moment and angular damping');
    assert.ok(Object.values(onset).every(Number.isFinite),'the actual fins, head, trunk and posterior all respond');
    assert.ok(onset.fin<onset.head&&onset.head<onset.trunk&&onset.trunk<onset.posterior,
      'the signed basal foil response leads the inertial head, then its articulated axial follow-through');
    assert.ok(swimmer.fish.pitch*sign>.10,'the signed fin moment actually changes the chosen swimming attitude');
    report.onsets.push({sign,...onset,largestMomentError,pitch:swimmer.fish.pitch});
  }

  const quiet={...neutral,speed:.24,forwardSpeed:.24};
  for(const phase of [0,Math.PI/2,Math.PI,Math.PI*1.5]){
    const motions=held({...quiet,finPhase:phase});
    assert.equal(finPitchMoment(quiet),0,'an undemanded glide has no decorative pitch moment');
    assert.ok(model.changes(motions).every(fin=>Math.abs(fin.change)<1e-10),'an undemanded glide has no perpetual signed fin feathering');
  }

  for(const seed of [913,481,33]){
    const swimmer=create(createStudyRandom(seed)),control=controllers();
    Object.assign(swimmer.fish,{x:.65,y:.58,z:tankCenterZ,yaw:0,pitch:0});
    swimmer.navigate({x:-.85,y:1.17,z:tankCenterZ-.1});
    let minY=2,maxY=0,maxPitch=0;
    for(let frame=0;frame<80*120;frame++){
      if(frame===12*120)swimmer.navigate({x:.85,y:.50,z:tankCenterZ+.1});
      if(frame===32*120)swimmer.navigate({x:-.8,y:.98,z:tankCenterZ-.2});
      if(frame===52*120)swimmer.navigate({x:.8,y:.61,z:tankCenterZ+.1});
      swimmer.step(dt);const fish=swimmer.fish,motions=sample(control,fish);
      minY=Math.min(minY,fish.y);maxY=Math.max(maxY,fish.y);maxPitch=Math.max(maxPitch,Math.abs(fish.pitch));
      if(frame%24)continue;
      model.apply(fish,motions);
      const correction=model.containment.correction(),magnitude=Math.hypot(correction.x,correction.y,correction.z);
      report.maximumCorrection=Math.max(report.maximumCorrection,magnitude);
      assert.ok(magnitude<1e-6,`signed fins, real axial tissue and deformed caudal geometry clear the tank for pitched seed ${seed}`);
      assert.deepEqual(model.asset.scale.toArray(),[1,1,1],'signed pitching never changes the actual fish scale');
    }
    assert.ok(maxY-minY>.2&&maxPitch>.25,'each seeded join includes substantial real climbs and descents');
  }
  return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  console.log(`Causal pitch control passed: ${JSON.stringify(await assertPitchControl())}`);
}
