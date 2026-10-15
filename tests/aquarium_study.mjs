import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture} from '../static/vendor/three.webgpu.js';
import {createSwimmer} from '../static/aquarium-motion.js';
import {applySwimPose,createFacialRig} from '../static/aquarium-rig.js';
import {createContainment} from '../static/aquarium-collision.js';
import {createFinController,createFinFrame,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';
import {createStudyRandom,createTurnStudy} from '../static/aquarium-study.js';
import {tankCenterZ,waterMin,waterMax} from '../static/aquarium-space.js';

const rate=120,duration=30,lastTick=rate*duration;
const checkpoints=new Set([1,120,239,240,241,360,480,720,1200,1800,2400,3000,lastTick]);
const copy=value=>ArrayBuffer.isView(value)?Array.from(value):value;

function controllers(){
  return Object.fromEntries(['pectoral','pelvic'].map(kind=>[kind,
    Object.fromEntries([-1,1].map(side=>[side,createFinController(side,kind==='pectoral')]))]));
}

function sampleFins(control,fish){
  return Object.fromEntries(['pectoral','pelvic'].map(kind=>[kind,
    Object.fromEntries([-1,1].map(side=>[side,control[kind][side].sample(fish,'swimming turn')]))]));
}

function snapshot(fish,motions){
  return {fish:{...fish},fins:Object.fromEntries(Object.entries(motions).map(([kind,sides])=>
    [kind,Object.fromEntries(Object.entries(sides).map(([side,motion])=>
      [side,Object.fromEntries(Object.entries(motion).map(([key,value])=>[key,copy(value)]))]))]))};
}

function harness(seed,makeRandom,makeStudy,onGeometry){
  const random=makeRandom(seed),swimmer=createSwimmer(random),fish=swimmer.fish;
  let study,control,motions,digest,observed=0,resets=0,samples=[],navigation=[];
  const navigate=swimmer.navigate;
  swimmer.navigate=point=>{navigation.push({age:fish.age,tick:study?.tick??0,point:{...point}});return navigate(point);};
  study=makeStudy(swimmer,{random,onReset:()=>{
    control=controllers();motions=sampleFins(control,fish);resets++;
    assert.equal(fish.age,0,'a turn-study reset returns the live swimmer to its initial simulation age');
    assert.equal(fish.x,.55,'the study starts in the shared central swimming lane');
    assert.equal(fish.y,.84,'the swimming turn begins at a fixed physical height');
    assert.equal(fish.z,tankCenterZ,'the swimming turn begins in the real tank centre');
    onGeometry?.(fish,motions,0);
  },onTick:(sample,tick)=>{
    assert.equal(sample,fish,'the study advances the real swimming state rather than a held-pose substitute');
    observed++;
    assert.equal(tick,observed,'each fixed physics tick has exactly one ordered command observation');
    assert.ok(Math.abs(fish.age-tick/rate)<1e-9,'every fin-history observation follows a 120 Hz swimming step');
    motions=sampleFins(control,fish);
    // Hash every observation, including transient fin history between the
    // readable checkpoint snapshots, without retaining millions of numbers.
    digest.update(JSON.stringify(fish));
    for(const kind of ['pectoral','pelvic'])for(const side of [-1,1]){
      for(const value of Object.values(motions[kind][side])){
        if(ArrayBuffer.isView(value))digest.update(new Uint8Array(value.buffer,value.byteOffset,value.byteLength));
        else digest.update(JSON.stringify(value));
      }
    }
    if(checkpoints.has(tick))samples.push({tick,...snapshot(fish,motions)});
    if(tick%24===0)onGeometry?.(fish,motions,tick);
  }});
  return {study,swimmer,random,
    reset(){observed=0;samples=[];navigation=[];digest=createHash('sha256');study.reset();},
    get samples(){return samples;},get observed(){return observed;},get resets(){return resets;},
    get navigation(){return navigation;},get final(){return snapshot(fish,motions);},
    get digest(){return digest.copy().digest('hex');}};
}

function runFrames(test,cadence,seconds=duration){
  let elapsed=0,frame=0;
  while(elapsed<seconds-1e-10){
    const dt=Math.min(cadence[frame++%cadence.length],seconds-elapsed);
    test.study.step(dt);elapsed+=dt;
  }
}

async function geometryReviewer(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  // Real exported weights and all fin vertices are retained. Rendering and
  // image decoding belong to the browser review, not this geometry harness.
  const loader=new GLTFLoader().register(()=>({name:'StudyGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const joints=[],fins=[];
  asset.traverse(mesh=>{
    if(mesh.isBone&&mesh.name.startsWith('spine_'))joints.push(mesh);
    if(!mesh.isSkinnedMesh||!/^(Pectoral|Pelvic|Caudal)_/.test(mesh.name))return;
    fins.push({mesh,base:Float32Array.from(mesh.geometry.attributes.position.array),
      paired:!mesh.name.startsWith('Caudal'),pectoral:mesh.name.startsWith('Pectoral')});
  });
  joints.sort((a,b)=>a.name.localeCompare(b.name));
  assert.equal(joints.length,34,'the reproducible study exercises the actual authored axial rig');
  const rest=joints.map(joint=>joint.quaternion.clone()),face=createFacialRig(asset),containment=createContainment(asset);
  for(const pectoral of [true,false]){
    const family=fins.filter(fin=>fin.paired&&fin.pectoral===pectoral);
    const frames=Object.fromEntries(family.filter(fin=>fin.mesh.name.includes('membrane')).map(fin=>{
      const frame=createFinFrame(fin.base,fin.mesh.geometry.attributes.uv.array);return [frame.side,frame];
    }));
    for(const fin of family)fin.binding=bindFinGeometry(fin.base,frames);
  }
  const report={measurements:0,maximumCorrection:0,minimumClearance:Infinity,minimumX:Infinity,
    maximumX:-Infinity,maximumYawRate:0,maximumTurnBend:0,maximumTailExcursion:0};
  return {report,review(fish,motions,tick){
    applySwimPose(asset,joints,rest,fish);face.pose(fish);
    for(const fin of fins){
      const points=fin.mesh.geometry.attributes.position;
      if(fin.paired)deformFinGeometry(fin.binding,motions[fin.pectoral?'pectoral':'pelvic'],points.array);
      else for(let i=0;i<points.count;i++){
        const x=fin.base[i*3],y=fin.base[i*3+1],z=fin.base[i*3+2];
        // The live caudal membrane shares the existing small distal tissue
        // compliance, in addition to its skinned axial motion.
        points.setXYZ(i,x,y,z+.035*(fish.stroke??1)*Math.sin(x*12-fish.phase-1.1)*Math.max(0,x-.48));
      }
    }
    const correction=containment.correction();
    const magnitude=Math.max(...Object.values(correction).map(Math.abs));
    report.maximumCorrection=Math.max(report.maximumCorrection,magnitude);
    assert.ok(magnitude<1e-8,`the actual fish needs no render-timed collision correction at study tick ${tick} (${(tick/rate).toFixed(3)} s): ${JSON.stringify(correction)}`);
    const bounds=containment.measure();
    for(let axis=0;axis<3;axis++){
      const key=['x','y','z'][axis];
      report.minimumClearance=Math.min(report.minimumClearance,bounds.min[key]-waterMin[axis],waterMax[axis]-bounds.max[key]);
    }
    assert.deepEqual(asset.scale.toArray(),[1,1,1],'depth and turn manoeuvres never resize the actual fish');
    report.minimumX=Math.min(report.minimumX,fish.x);report.maximumX=Math.max(report.maximumX,fish.x);
    report.maximumYawRate=Math.max(report.maximumYawRate,Math.abs(fish.yawRate));
    report.maximumTurnBend=Math.max(report.maximumTurnBend,Math.abs(fish.turnBend));
    report.maximumTailExcursion=Math.max(report.maximumTailExcursion,Math.abs(fish.yawLag3));
    report.measurements++;
  }};
}

// Factories are injectable so repeatability and physical clearance can be
// mutation-checked without changing production files between assertions.
export async function assertSwimmingStudy(makeRandom=createStudyRandom,makeStudy=createTurnStudy){
  const rng=makeRandom(913),sequence=Array.from({length:24},()=>rng());
  assert.ok(sequence.every(value=>value>=0&&value<1)&&new Set(sequence).size>20,'study randomness provides a nonconstant unit-interval sequence');
  rng.reset();
  assert.deepEqual(Array.from({length:24},()=>rng()),sequence,'resetting study randomness replays the identical seed sequence');
  const other=makeRandom(481);
  assert.notDeepEqual(Array.from({length:24},()=>other()),sequence,'different study seeds produce different future choices');

  const baseline=harness(913,makeRandom,makeStudy);baseline.reset();runFrames(baseline,[1/60]);
  assert.equal(baseline.study.tick,lastTick,'thirty displayed seconds contain exactly 3,600 swimming ticks');
  assert.equal(baseline.observed,lastTick,'fin command history observes all 3,600 ticks');
  assert.equal(baseline.navigation.length,2,'the finite comparison route starts once and reverses once');
  assert.equal(baseline.navigation[0].age,0,'the first destination is set before any swimming step');
  assert.ok(baseline.navigation[0].point.x<0&&baseline.navigation[1].point.x>0,'the route asks the swimming fish to reverse its travel direction');
  assert.ok(Math.abs(baseline.navigation[1].age-2)<1e-10,'the direction reversal happens at two simulation seconds');
  for(const cadence of [[1/15],[1/30],[1/144],[1/59],[.011,.027,.004,.053,.008,.019]]){
    const comparison=harness(913,makeRandom,makeStudy);comparison.reset();runFrames(comparison,cadence);
    assert.equal(comparison.study.tick,lastTick,'a different renderer cadence neither loses nor invents simulation ticks');
    assert.deepEqual(comparison.samples,baseline.samples,'equal simulation ticks reproduce the complete swimmer and all four delayed fin fields across render cadences');
    assert.deepEqual(comparison.final,baseline.final,'display cadence cannot alter the final swimming or fin pose');
    assert.equal(comparison.digest,baseline.digest,'all 3,600 swimming states and complete delayed fin fields match between display cadences');
  }
  const remembered=baseline.samples,final=baseline.final,digest=baseline.digest;
  baseline.reset();
  assert.equal(baseline.study.tick,0,'restart rewinds the integer simulation clock');
  assert.ok(Math.abs(baseline.study.accumulator)<1e-12,'restart clears fractional display time');
  runFrames(baseline,[1/30]);
  assert.deepEqual(baseline.samples,remembered,'restart replays the same choices, activation and fin-command history');
  assert.deepEqual(baseline.final,final,'restart finishes in the identical physical state');
  assert.equal(baseline.digest,digest,'restart replays every swimming and fin-history observation');
  assert.equal(baseline.resets,2,'reset hooks rebuild the command histories for each replay');

  const changedSeed=harness(481,makeRandom,makeStudy);changedSeed.reset();runFrames(changedSeed,[1/60]);
  assert.notDeepEqual(changedSeed.samples,remembered,'the actual swimmer consumes the study seed rather than ambient randomness or a hardcoded trace');
  const accumulation=harness(913,makeRandom,makeStudy);accumulation.reset();
  for(const invalid of [0,-1,NaN,Infinity])accumulation.study.step(invalid);
  assert.equal(accumulation.study.tick,0,'invalid or nonpositive elapsed times do not advance the study');
  accumulation.study.step(.004);accumulation.study.step(.004);
  assert.equal(accumulation.study.tick,0,'sub-tick frame times remain accumulated');
  accumulation.study.step(.001);
  assert.equal(accumulation.study.tick,1,'fractional frame time eventually advances exactly one fixed step');
  accumulation.reset();accumulation.study.step(1);
  assert.equal(accumulation.study.tick,30,'a suspended display cannot inject more than a quarter-second at once');

  const geometry=await geometryReviewer(),physical=harness(913,makeRandom,makeStudy,geometry.review);
  physical.reset();runFrames(physical,[1/60]);
  assert.equal(geometry.report.measurements,151,'the entire thirty-second route is checked against the real mesh every fifth of a second');
  assert.ok(geometry.report.maximumX-geometry.report.minimumX>.15,'the study actually swims through the tank');
  assert.ok(geometry.report.maximumYawRate>.15&&geometry.report.maximumTurnBend>.03,'the comparison contains an actual loaded turn rather than a static geometry pose');
  assert.ok(geometry.report.maximumTailExcursion>.02,'the tail follows the physical turning history');
  return geometry.report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  console.log(`Reproducible swimming turn passed: ${JSON.stringify(await assertSwimmingStudy())}`);
}
