import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Quaternion,Euler} from '../static/vendor/three.webgpu.js';
import {createSwimmer,finPitchMoment,finRollMoment} from '../static/aquarium-motion.js';
import {applySwimPose,createFacialRig} from '../static/aquarium-rig.js';
import {createContainment} from '../static/aquarium-collision.js';
import {createFinController,createFinMotion,createFinFrame,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';
import {createCaudalController,createCaudalFrame,bindCaudalGeometry,deformCaudalGeometry} from '../static/aquarium-caudal.js';
import {createStudyRandom} from '../static/aquarium-study.js';
import {tankCenterZ} from '../static/aquarium-space.js';

const dt=1/120;
const neutral={x:0,y:0,z:0,yaw:0,pitch:0,bank:0,bankRate:0,bankLag1:0,bankLag2:0,bankLag3:0,
  pitchFin:0,rollFin:0,pitchRate:0,yawRate:0,stroke:0,muscle1:0,muscle2:0,muscle3:0,age:0,phase:0,
  speed:0,forwardSpeed:0,lateralGain:0,verticalGain:0};
const held=fish=>Object.fromEntries(['pectoral','pelvic'].map(kind=>[kind,
  Object.fromEntries([-1,1].map(side=>[side,createFinMotion(fish,side,kind==='pectoral')]))]));
const controllers=()=>({pectoral:{'-1':createFinController(-1,true),'1':createFinController(1,true)},
  pelvic:{'-1':createFinController(-1,false),'1':createFinController(1,false)},caudal:createCaudalController()});
const sample=(control,fish)=>({pectoral:{'-1':control.pectoral[-1].sample(fish),'1':control.pectoral[1].sample(fish)},
  pelvic:{'-1':control.pelvic[-1].sample(fish),'1':control.pelvic[1].sample(fish)},caudal:control.caudal.sample(fish)});

async function geometry(deform,pose){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'RollGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const joints=[],fins=[];
  asset.traverse(mesh=>{
    if(mesh.isBone&&mesh.name.startsWith('spine_'))joints.push(mesh);
    if(mesh.isSkinnedMesh&&/^(Pectoral|Pelvic|Caudal)_/.test(mesh.name))fins.push({mesh,
      base:Float32Array.from(mesh.geometry.attributes.position.array),paired:!mesh.name.startsWith('Caudal'),pectoral:mesh.name.startsWith('Pectoral')});
  });
  joints.sort((a,b)=>a.name.localeCompare(b.name));
  assert.equal(joints.length,34,'roll retains the real articulated skeleton');
  const rest=joints.map(joint=>joint.quaternion.clone()),face=createFacialRig(asset),body=asset.getObjectByName('Koi_skin');
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
  function apply(fish,motions){
    for(const fin of fins){
      const output=fin.mesh.geometry.attributes.position.array;
      if(fin.paired)deform(fin.binding,motions[fin.pectoral?'pectoral':'pelvic'],output);
      else if(motions.caudal)deformCaudalGeometry(fin.binding,motions.caudal,output);
    }
    pose(asset,joints,rest,fish);face.pose(fish);
  }
  function foilAngles(fish){
    apply(neutral,held(fish));
    return fins.filter(fin=>fin.paired&&fin.mesh.name.includes('membrane')).map(({mesh,base,pectoral})=>{
      mesh.skeleton.update();const normal=new Vector3(),indices=mesh.geometry.index.array;
      for(let i=0;i<indices.length;i+=3){
        const points=[0,1,2].map(offset=>mesh.getVertexPosition(indices[i+offset],new Vector3()).applyMatrix4(mesh.matrixWorld));
        normal.add(points[1].sub(points[0]).cross(points[2].sub(points[0])));
      }
      normal.normalize();if(normal.y<0)normal.negate();
      return {pectoral,side:Math.sign(base[2]),angle:Math.atan2(normal.x,normal.y)};
    });
  }
  const changes=fish=>{
    const zero=foilAngles({...fish,rollFin:0}),actual=foilAngles(fish);
    return actual.map((value,i)=>({...value,change:value.angle-zero[i].angle}));
  };
  const positions=body.geometry.attributes.position;
  const meridians=[-.50,-.25,0,.50].map(x=>{
    const xs=Array.from({length:positions.count},(_,i)=>positions.getX(i));
    const actualX=xs.reduce((a,b)=>Math.abs(a-x)<Math.abs(b-x)?a:b);
    const ring=xs.map((value,i)=>Math.abs(value-actualX)<1e-6?i:-1).filter(i=>i>=0);
    return {top:ring.reduce((a,b)=>positions.getY(a)>positions.getY(b)?a:b),bottom:ring.reduce((a,b)=>positions.getY(a)<positions.getY(b)?a:b)};
  });
  apply(neutral,held(neutral));
  const skullRest=joints[0].getWorldQuaternion(new Quaternion()).invert();
  function attitude(fish){
    apply(fish,held(fish));body.skeleton.update();
    const headingInverse=new Quaternion().setFromEuler(new Euler(0,fish.yaw,-fish.pitch,'YXZ')).invert();
    const dorsal=new Vector3(0,1,0).applyQuaternion(joints[0].getWorldQuaternion(new Quaternion()).multiply(skullRest)).applyQuaternion(headingInverse);
    const skull=Math.atan2(dorsal.z,dorsal.y);
    const skin=meridians.map(({top,bottom})=>{
      const a=body.getVertexPosition(top,new Vector3()).applyMatrix4(body.matrixWorld);
      const b=body.getVertexPosition(bottom,new Vector3()).applyMatrix4(body.matrixWorld);
      const direction=a.sub(b).applyQuaternion(headingInverse);
      return Math.atan2(direction.z,direction.y);
    });
    return {skull,skin};
  }
  return {asset,apply,changes,attitude,containment:createContainment(asset)};
}

// These bounds exercise the authored control/geometry join. They do not impose
// a universal inward bank or claim measured koi fin forces and response times.
export async function assertRollControl({create=createSwimmer,deform=deformFinGeometry,pose=applySwimPose}={}){
  const model=await geometry(deform,pose),report={mirrored:[],recovery:[],maximumCorrection:0};
  for(const pitchFin of [-.10,.10])for(const sign of [-1,1]){
    const fish={...neutral,pitchFin,rollFin:sign*.08,finPhase:2.1,speed:.14,forwardSpeed:.14,stroke:.4,muscle3:.4};
    const changes=model.changes(fish);
    for(const fin of changes)assert.ok(fin.change*(-fin.side*sign)>(fin.pectoral?.02:.001),
      'the actual left and right foils present opposing signed roll incidence around their pitch-only neutral');
    assert.ok(finRollMoment(fish)*sign>.4,'the same differential incidences create the corresponding roll control moment');
    assert.ok(Math.abs(finPitchMoment(fish)-finPitchMoment({...fish,rollFin:0}))<1e-12,
      'differential roll cancels in the common pitch command channel');
    report.mirrored.push({pitchFin,sign,moment:finRollMoment(fish),changes});
  }
  // Yaw rate alone cannot create a tail twist in the absence of an actual bank
  // history, driven compliance or an explicit torsion command.
  const still=model.attitude(neutral);
  for(const sign of [-1,1]){
    const yawOnly=model.attitude({...neutral,yawRate:sign*.6});
    assert.ok(yawOnly.skin.every((angle,i)=>Math.abs(angle-still.skin[i])<1e-7),
      'yaw cannot inject an unobserved instantaneous torsional bank into the actual skin');
  }

  for(const sign of [-1,1]){
    const swimmer=create(()=>.5),control=controllers();
    Object.assign(swimmer.fish,{x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0,bank:sign*.06});
    swimmer.navigate({...swimmer.fish});sample(control,swimmer.fish);
    const initial=model.attitude({...swimmer.fish,lateralGain:0,verticalGain:0,stroke:0,muscle1:0,muscle2:0,muscle3:0});
    assert.ok(initial.skin.every(angle=>Math.abs(angle-initial.skin[0])<1e-4),
      'a settled bank with no roll history does not invent a permanent counter-twist');
    const onset={fin:null,head:null,shoulder:null,trunk:null,tail:null};
    let previousAngle=initial.skull,previousRate=0,maximumMomentError=0,final;
    for(let frame=0;frame<360;frame++){
      swimmer.step(dt);const fish=swimmer.fish;
      const changes=model.changes(fish).filter(fin=>fin.pectoral);
      if(onset.fin===null&&changes.every(fin=>fin.change*fin.side*sign>.001))onset.fin=fish.age;
      const actual=model.attitude({...fish,lateralGain:0,verticalGain:0,stroke:0,muscle1:0,muscle2:0,muscle3:0});
      const rate=(actual.skull-previousAngle)/dt,moment=(rate-previousRate)/dt+8*previousRate;
      assert.ok(Math.abs(rate-fish.bankRate)<1e-6,'the roll moment accelerates the actual skull');
      maximumMomentError=Math.max(maximumMomentError,Math.abs(moment-fish.rollMoment));
      for(const [i,key] of ['head','shoulder','trunk','tail'].entries()){
        if(onset[key]===null&&(initial.skin[i]-actual.skin[i])*sign>.0005)onset[key]=fish.age;
      }
      previousAngle=actual.skull;previousRate=rate;final={...fish,skin:actual.skin};
    }
    assert.ok(maximumMomentError<1e-4,'actual angular acceleration follows the shared fin roll moment and damping');
    assert.ok(Object.values(onset).every(Number.isFinite),'actual fins, skull and all axial regions respond to roll unloading');
    assert.ok(onset.fin<onset.head&&onset.head<onset.shoulder&&onset.shoulder<onset.trunk&&onset.trunk<onset.tail,
      'signed fin loading leads the inertial head and causal axial roll follow-through');
    assert.ok(Math.abs(final.bank)<.001&&Math.abs(final.rollFin)<.001&&final.skin.every(angle=>Math.abs(angle)<.001),
      'fin control and actual head/trunk roll settle together after unloading');
    report.recovery.push({sign,onset,maximumMomentError,bank:final.bank,rollFin:final.rollFin});
  }
  for(const phase of [0,1.5,3,4.5]){
    const fish={...neutral,speed:.24,forwardSpeed:.24,finPhase:phase};
    assert.equal(finRollMoment(fish),0,'a quiet glide has no decorative roll moment');
    assert.ok(model.changes(fish).every(fin=>Math.abs(fin.change)<1e-10),'a quiet glide has no free-running differential trim');
  }
  for(const seed of [913,481,33]){
    const swimmer=create(createStudyRandom(seed)),control=controllers();
    Object.assign(swimmer.fish,{x:.65,y:.58,z:tankCenterZ,yaw:0,pitch:0});
    swimmer.navigate({x:-.85,y:1.17,z:tankCenterZ-.1});
    let peakBank=0,peakPitch=0;
    for(let frame=0;frame<80*120;frame++){
      if(frame===12*120)swimmer.navigate({x:.85,y:.50,z:tankCenterZ+.1});
      if(frame===32*120)swimmer.navigate({x:-.8,y:.98,z:tankCenterZ-.2});
      if(frame===52*120)swimmer.navigate({x:.8,y:.61,z:tankCenterZ+.1});
      swimmer.step(dt);const fish=swimmer.fish,motions=sample(control,fish);
      peakBank=Math.max(peakBank,Math.abs(fish.bank));peakPitch=Math.max(peakPitch,Math.abs(fish.pitch));
      assert.ok(Math.abs(fish.bank)<=.13&&Math.abs(fish.rollFin)<=.15,'roll state retains its authored attitude and actuator bounds');
      if(frame%24)continue;
      model.apply(fish,motions);
      const correction=model.containment.correction(),magnitude=Math.hypot(correction.x,correction.y,correction.z);
      report.maximumCorrection=Math.max(report.maximumCorrection,magnitude);
      assert.ok(magnitude<1e-6,`combined pitch, bank, twist and all deformed fins clear the tank for seed ${seed}`);
      assert.deepEqual(model.asset.scale.toArray(),[1,1,1],'roll does not change actual fish scale');
    }
    assert.ok(peakBank>.003&&peakPitch>.25,'the seeded geometry check actually combines banked turns and vertical swimming');
  }
  return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  console.log(`Causal roll control passed: ${JSON.stringify(await assertRollControl())}`);
}
