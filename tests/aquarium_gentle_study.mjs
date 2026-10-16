import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Matrix4} from '../static/vendor/three.webgpu.js';
import {createSwimmer} from '../static/aquarium-motion.js';
import {createFinController} from '../static/aquarium-fins.js';
import {createCaudalController} from '../static/aquarium-caudal.js';
import {createMedianController} from '../static/aquarium-median.js';
import {createKoiInstance} from '../static/aquarium-fish-instance.js';
import {createStudyRandom} from '../static/aquarium-study.js';
import {tankCenterZ,waterMin,waterMax} from '../static/aquarium-space.js';
import {createGentleStudy} from '../static/aquarium-gentle-study.js';

const dt=1/120,lastTick=1920,wrap=x=>Math.atan2(Math.sin(x),Math.cos(x));
const checkpoints=new Set([1,239,240,241,599,600,601,959,960,961,1439,1440,1441,lastTick]);
const near=(a,b,message,tolerance=1e-9)=>assert.ok(Number.isFinite(a)&&Math.abs(a-b)<tolerance,`${message}: ${a} versus ${b}`);
function clockContract(make){
  let armed=false,physics=false,resets=0,randomResets=0,observed=0,steps=0,navigations=[];
  const raw={age:7,x:.1,y:.7,z:0,yaw:.17,pitch:.02},fish=new Proxy(raw,{set(target,key,value){assert.ok(!armed||physics,'study choices cannot write a live position or head pose');target[key]=value;return true;}});
  const driver={fish,reset(){armed=false;resets++;Object.assign(fish,{age:0,x:0,y:0,z:0,yaw:0,pitch:0});},
    navigate(point){navigations.push({age:fish.age,from:{...fish},point:{...point}});},
    step(elapsed){near(elapsed,dt,'the study issues fixed physical ticks');steps++;physics=true;fish.age+=elapsed;fish.x-=.02*elapsed;fish.yaw+=.004*elapsed;physics=false;}};
  const random=()=>{throw Error('the route does not consume ambient randomness');};random.reset=()=>randomResets++;
  const before={...fish},study=make(driver,{random,onReset(sample){assert.strictEqual(sample,fish);armed=true;observed=0;},onTick(sample,tick){assert.strictEqual(sample,fish);observed++;assert.equal(tick,observed);near(fish.age,tick*dt,'callback follows the completed physical tick');}});
  assert.deepEqual({...fish},before,'constructing the study leaves the current scene alone');assert.equal(resets,0);assert.equal(randomResets,0);
  study.step(.25);assert.equal(steps,0,'a study stays inactive until explicit reset');assert.equal(navigations.length,0);
  study.reset();assert.equal(resets,1);assert.equal(randomResets,1);assert.equal(study.duration,16);assert.equal(study.complete,false);
  assert.deepEqual([fish.x,fish.y,fish.z,fish.yaw],[.85,.84,tankCenterZ,0]);
  for(const invalid of [0,-.1,NaN,Infinity,-Infinity])study.step(invalid);
  assert.equal(study.tick,0);study.step(1/240);assert.equal(study.tick,0);study.step(1/240);assert.equal(study.tick,1);
  armed=false;study.reset();navigations=[];steps=0;study.step(.004);study.step(.5);assert.equal(study.tick,30,'stall cap preserves an existing fractional remainder');near(study.accumulator,.004,'fractional remainder survives the cap');study.step(.005);assert.equal(study.tick,31);
  armed=false;study.reset();navigations=navigations.slice(-1);steps=0;
  for(let i=0;i<8;i++)study.step(.25);
  assert.equal(study.tick,240);assert.equal(navigations.length,1,'the new decision waits for the first tick after its boundary');
  study.step(dt);assert.equal(navigations.length,2);near(navigations[1].age,2,'the correction samples the actual two-second state');
  for(let i=0;i<56;i++)study.step(.25);
  assert.equal(study.tick,lastTick);assert.equal(observed,lastTick);assert.equal(steps,lastTick);assert.equal(study.complete,true);assert.equal(navigations.length,5);
  const held={fish:{...fish},tick:study.tick,stage:study.stage,accumulator:study.accumulator,commands:structuredClone(study.commands)};
  for(let i=0;i<100;i++)study.step(.25);
  assert.deepEqual({fish:{...fish},tick:study.tick,stage:study.stage,accumulator:study.accumulator,commands:study.commands},held,'completion holds without an implicit loop or extra time');
  const commands=study.commands;commands[0].point.x=99;commands[0].from.yaw=99;commands.push({});assert.deepEqual(study.commands,held.commands,'issued command snapshots own their nested points and origins');
}
const controls=()=>({pectoral:{'-1':createFinController(-1,true),'1':createFinController(1,true)},pelvic:{'-1':createFinController(-1,false),'1':createFinController(1,false)},
  caudal:createCaudalController(),median:{dorsal:createMedianController('dorsal'),anal:createMedianController('anal')}});
function sample(control,fish){return {pectoral:Object.fromEntries([-1,1].map(s=>[s,control.pectoral[s].sample(fish)])),pelvic:Object.fromEntries([-1,1].map(s=>[s,control.pelvic[s].sample(fish)])),
  caudal:control.caudal.sample(fish),median:Object.fromEntries(['dorsal','anal'].map(k=>[k,control.median[k].sample(fish)]))};}
function harness(make,seed=913,physical){
  const random=createStudyRandom(seed),instance=physical?.(random),swimmer=instance?.swimmer??createSwimmer(random),fish=swimmer.fish;
  const motions={pectoral:{},pelvic:{},caudal:null,median:{}};
  if(instance){
    const capture=(controller,save)=>{const original=controller.sample.bind(controller);controller.sample=(...args)=>{const value=original(...args);save(value);return value;};};
    for(const kind of ['pectoral','pelvic'])for(const side of [-1,1])capture(instance.pairedControllers[kind][side],m=>motions[kind][side]=m);
    capture(instance.caudalController,m=>motions.caudal=m);for(const kind of ['dorsal','anal'])capture(instance.medianControllers[kind],m=>motions.median[kind]=m);
  }
  let digest,control,count=0,resetCount=0,samples=[],navigation=[],lastPhysics=null,stats;
  const navigate=swimmer.navigate.bind(swimmer),step=swimmer.step.bind(swimmer);
  const driver={fish,reset:()=>swimmer.reset(),navigate(point){
    if(lastPhysics&&fish.age>0)assert.deepEqual(fish,lastPhysics,'issuing a destination cannot pre-rotate the actual head');
    navigation.push({age:fish.age,from:{...fish},point:{...point}});return navigate(point);
  },step(elapsed){
    if(lastPhysics)assert.deepEqual(fish,lastPhysics,'the study cannot force the root or pose between physical ticks');
    if(instance)instance.step(elapsed);else step(elapsed);lastPhysics={...fish};
  }};
  const study=make(driver,{random,onReset(){
    count=0;resetCount++;samples=[];digest=createHash('sha256');control=controls();lastPhysics={...fish};
    if(instance){instance.pose();instance.updateVisual();}else sample(control,fish);
    stats={minimumX:fish.x,maximumX:fish.x,levelMaximumHeightError:0,climbStartY:0,maximumC:0,maximumYaw:0,maximumPitch:0,maximumVy:0,maximumPaddle:0,quietTicks:0};
    physical?.review?.(instance,0);
  },onTick(observed,tick){
    assert.strictEqual(observed,fish);assert.deepEqual(fish,lastPhysics,'observers see the physical result without head or path decoration');
    count++;assert.equal(tick,count);near(fish.age,tick*dt,'every physical tick is observed exactly once');
    for(const value of Object.values(fish))if(typeof value==='number')assert.ok(Number.isFinite(value));
    const m=instance?motions:sample(control,fish);digest.update(JSON.stringify(fish));digest.update(JSON.stringify(m));
    if(checkpoints.has(tick))samples.push({tick,fish:{...fish},motions:structuredClone(m)});
    stats.minimumX=Math.min(stats.minimumX,fish.x);stats.maximumX=Math.max(stats.maximumX,fish.x);
    stats.maximumC=Math.max(stats.maximumC,Math.abs(fish.turnAnterior),Math.abs(fish.turnTrunk),Math.abs(fish.turnPosterior));stats.maximumYaw=Math.max(stats.maximumYaw,Math.abs(fish.yaw));
    stats.maximumPitch=Math.max(stats.maximumPitch,Math.abs(fish.pitch));stats.maximumVy=Math.max(stats.maximumVy,Math.abs(fish.vy));
    for(const family of ['pectoral','pelvic'])for(const side of ['Plus','Minus'])stats.maximumPaddle=Math.max(stats.maximumPaddle,Math.abs(fish[family+'Paddle'+side]));
    if(tick<=960)stats.levelMaximumHeightError=Math.max(stats.levelMaximumHeightError,Math.abs(fish.y-.84));
    if(tick===960)stats.climbStartY=fish.y;if(fish.stroke<.01)stats.quietTicks++;
    if(tick%30===0||checkpoints.has(tick))physical?.review?.(instance,tick);
  }});
  return {study,fish,instance,get count(){return count;},get resets(){return resetCount;},get samples(){return samples;},get navigation(){return navigation;},get stats(){return stats;},get digest(){return digest.copy().digest('hex');},
    reset(){navigation=[];lastPhysics=null;study.reset();},get final(){return {...fish};}};
}
function run(test,cadence){let elapsed=0,frame=0;while(elapsed<16-1e-10){const amount=Math.min(cadence[frame++%cadence.length],16-elapsed);test.study.step(amount);elapsed+=amount;}}
function commands(test){
  assert.equal(test.navigation.length,5);const times=[0,2,5,8,12],corrections=[0,.06,-.10,.10,-.06];
  test.navigation.forEach(({age,from,point},i)=>{
    near(age,times[i],'commands use the scheduled simulation age');assert.equal(point.x,-1.05);assert.equal(point.y,i<3?.84:.92);
    const bearing=Math.atan2(point.z-from.z,from.x-point.x);near(wrap(bearing-from.yaw),corrections[i],'a correction is incremental from the actual current heading');
    assert.ok(point.z>=-.55&&point.z<=.24,'the actual destination is inside navigate bounds and is not silently clipped');
    const issued=test.study.commands[i];assert.deepEqual(issued.point,point);assert.deepEqual(issued.from,{x:from.x,y:from.y,z:from.z,yaw:from.yaw});
  });
}
async function physicalFactory(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url)),source=(await new GLTFLoader().register(()=>({name:'GentleStudyGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())})).parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;
  const sourceHash=()=>{const h=createHash('sha256');source.traverse(n=>{h.update(JSON.stringify([n.position.toArray(),n.quaternion.toArray(),n.scale.toArray()]));if(n.isMesh)for(const a of Object.values(n.geometry.attributes))h.update(new Uint8Array(a.array.buffer,a.array.byteOffset,a.array.byteLength));});return h.digest('hex');};
  const initial=sourceHash(),report={measurements:0,vertices:0,minimumClearance:Infinity,maximumCorrection:0,meshes:new Set()};
  const factory=random=>createKoiInstance(source,{random});
  factory.review=(instance,tick)=>{
    assert.equal(instance.behavior.contacts,0,'gentle motion must not rely on a final collision fallback');
    const correction=instance.containment.correction();report.maximumCorrection=Math.max(report.maximumCorrection,...Object.values(correction).map(Math.abs));assert.ok(report.maximumCorrection<1e-7,'the actual skin and membranes require no render-timed correction');
    const meshes=[];instance.asset.traverse(m=>{if(m.isSkinnedMesh)meshes.push(m);});new Set(meshes.map(m=>m.skeleton)).forEach(s=>s.update());
    const bind=new Vector3(),out=new Vector3(),post=new Matrix4();
    for(const mesh of meshes){report.meshes.add(mesh.name);const a=mesh.geometry.attributes,p=mesh.skeleton.boneMatrices;post.multiplyMatrices(mesh.matrixWorld,mesh.bindMatrixInverse);
      for(let i=0;i<a.position.count;i++){
        bind.fromBufferAttribute(a.position,i).applyMatrix4(mesh.bindMatrix);out.set(0,0,0);
        for(let slot=0;slot<4;slot++){const w=a.skinWeight.getComponent(i,slot);if(!w)continue;const e=a.skinIndex.getComponent(i,slot)*16;
          out.x+=w*(p[e]*bind.x+p[e+4]*bind.y+p[e+8]*bind.z+p[e+12]);out.y+=w*(p[e+1]*bind.x+p[e+5]*bind.y+p[e+9]*bind.z+p[e+13]);out.z+=w*(p[e+2]*bind.x+p[e+6]*bind.y+p[e+10]*bind.z+p[e+14]);}
        out.applyMatrix4(post);if(!Number.isFinite(out.x)||!Number.isFinite(out.y)||!Number.isFinite(out.z))assert.fail('every actual uploaded vertex remains finite');
        const clearance=Math.min(out.x-waterMin[0],waterMax[0]-out.x,out.y-waterMin[1],waterMax[1]-out.y,out.z-waterMin[2],waterMax[2]-out.z);report.minimumClearance=Math.min(report.minimumClearance,clearance);
        if(clearance<-.000002)assert.fail(`actual ${mesh.name} vertex ${i} crosses water at gentle tick ${tick}`);report.vertices++;
      }
    }
    assert.deepEqual(instance.asset.scale.toArray(),[1,1,1]);report.measurements++;
  };
  factory.finish=()=>{assert.equal(sourceHash(),initial,'physical replay leaves the pristine GLB source untouched');report.meshCount=report.meshes.size;delete report.meshes;return report;};return factory;
}

export async function assertGentleStudy(make=createGentleStudy){
  clockContract(make);
  const baseline=harness(make);baseline.reset();run(baseline,[1/60]);commands(baseline);assert.equal(baseline.count,lastTick);assert.equal(baseline.study.complete,true);
  for(const cadence of [[1/30],[1/144],[.011,.027,.004,.053,.008,.019]]){
    const other=harness(make);other.reset();run(other,cadence);assert.equal(other.count,lastTick);assert.equal(other.digest,baseline.digest,'cadence preserves every complete swimmer state and all seven fin histories');assert.deepEqual(other.samples,baseline.samples);assert.deepEqual(other.final,baseline.final);commands(other);
  }
  const first={digest:baseline.digest,final:baseline.final,samples:baseline.samples};baseline.reset();run(baseline,[1/30]);assert.equal(baseline.digest,first.digest,'reset replays all actual choices and fin histories');assert.deepEqual(baseline.final,first.final);assert.deepEqual(baseline.samples,first.samples);
  const changed=harness(make,481);changed.reset();run(changed,[1/60]);assert.notEqual(changed.digest,baseline.digest,'the real swimmer consumes resettable seeded choices');
  const factory=await physicalFactory(),physical=harness(make,913,factory);physical.reset();run(physical,[1/60]);commands(physical);
  assert.equal(physical.digest,baseline.digest,'the actual factory observes the identical complete physical and fin-control histories');assert.deepEqual(physical.final,baseline.final);
  const s=physical.stats;assert.ok(s.maximumX-s.minimumX>1,'the study makes useful forward swimming progress');assert.ok(s.maximumYaw>.02&&s.maximumYaw<.25,'both restrained course choices move the actual heading');assert.ok(s.maximumC<.30,'gentle corrections do not recruit a strong C manoeuvre');
  assert.ok(s.quietTicks>120,'the sequence includes real quiet gliding intervals');assert.ok(s.maximumPaddle>1e-5,'small course choices reach actual fin motors');assert.ok(s.levelMaximumHeightError<.002,'the first level stages do not fabricate a climb');
  assert.ok(physical.final.y-s.climbStartY>.02&&s.maximumPitch>.01&&s.maximumVy>.001,'the actual pitched swimmer makes useful progress toward the higher destination');
  physical.instance.dispose();return {...factory.finish(),stats:s,commands:physical.study.commands,digest:physical.digest};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){const start=performance.now(),report=await assertGentleStudy();console.log(`Gentle study passed: ${JSON.stringify({seconds:(performance.now()-start)/1000,report})}`);}
