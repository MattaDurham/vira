import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Quaternion,Matrix4} from '../static/vendor/three.webgpu.js';
import {createFinYawController,trimFinAngles} from '../static/aquarium-fin-yaw.js';
import {createSwimmer,createFinTaskController,finActuation} from '../static/aquarium-motion.js';
import {createTurnController,turnResponse} from '../static/aquarium-turn.js';
import {createFinMotion,createFinFrame,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';
import {createKoiInstance} from '../static/aquarium-fish-instance.js';
import {createStudyRandom} from '../static/aquarium-study.js';
import {tankCenterZ,waterMin,waterMax} from '../static/aquarium-space.js';

const dt=1/120,wrap=a=>Math.atan2(Math.sin(a),Math.cos(a));
const fields=['pectoralPaddlePlus','pectoralPaddleMinus','pectoralForeAftPlus','pectoralForeAftMinus',
  'pelvicPaddlePlus','pelvicPaddleMinus','pelvicForeAftPlus','pelvicForeAftMinus'];
const zero=Object.fromEntries(fields.map(key=>[key,0]));
const near=(a,b,tolerance=1e-10,message='values agree')=>assert.ok(Number.isFinite(a)&&Math.abs(a-b)<=tolerance,`${message}: ${a} versus ${b}`);
const copy=value=>structuredClone(value),quiet=()=>({paddle:0,foreAft:0});
const control=(t,leading)=>trimFinAngles(t,.7,leading);
function zeroTaskFactory(){
  const c=createFinTaskController(),mask=value=>Object.assign(value,zero);
  return {reset:()=>mask(c.reset()),sample:input=>mask(c.sample(input))};
}

export function assertFinYawMechanics(make=createFinYawController){
  const controller=make(),quietGesture=createTurnController().reset();
  for(let tick=0;tick<100;tick++){
    const result=controller.sample(tick*dt,quiet(),quiet());
    near(result.moment,0,1e-14,'quiet issued motors cannot turn the head');near(result.damping,0,1e-14,'quiet motors add no motor retrieval damping');
  }
  controller.reset();let maximum=0,impulse=0,retrieval=0,omega=0;
  const mirror=make();
  for(let tick=0;tick<=120;tick++){
    const t=tick*dt,a=copy(controller.sample(t,control(t,true),control(t,false))),b=copy(mirror.sample(t,control(t,false),control(t,true)));
    assert.ok(Object.values(a).every(Number.isFinite));assert.ok(a.damping>=0,'motor retrieval has nonnegative damping');
    near(a.moment,-b.moment,1e-12,'swapping issued sides reverses actual active yaw');near(a.damping,b.damping,1e-12,'retrieval damping mirrors');
    maximum=Math.max(maximum,a.moment);impulse+=a.moment*dt;retrieval=Math.max(retrieval,a.damping);
    if(t>.36)near(a.moment,0,1e-12,'reoriented retrieval has no active yaw drive');
    const previousOmega=omega,response=turnResponse(quietGesture,omega,dt,{finMoment:a.moment,finDamping:a.damping});omega=response.yawRate;
    const rest=turnResponse(quietGesture,0,dt,{finMoment:0,finDamping:a.damping});near(rest.yawRate,0,1e-14,'retrieval cannot start rotation from rest');
    const blocked=turnResponse(quietGesture,0,dt,{finMoment:a.moment,finDamping:a.damping,allowed:0});near(blocked.yawRate,0,1e-14,'asset forecast can block active fin response');
    if(t>.36)assert.ok(Math.abs(response.yawRate)<=Math.abs(previousOmega)+1e-14,'retrieval only dissipates existing head rotation');
  }
  assert.ok(maximum>.1&&impulse>.02&&retrieval>.1,'the issued presentation/load/retrieval path has finite authority and a feathered recovery');
  near(turnResponse(quietGesture,0,dt,{finRate:1}).yawRate,0,1e-14,'a desired heading-rate option cannot bypass issued motor actuation');
  for(const held of [{paddle:.02,foreAft:.08},{paddle:0,foreAft:-.3},{paddle:-.01,foreAft:.1}]){
    controller.reset();controller.sample(0,held,quiet());
    for(let i=1;i<50;i++)near(controller.sample(i*dt,held,quiet()).moment,0,1e-14,'held or hidden/folded controls supply no active motor impulse');
  }
  const changedPresentation=make();changedPresentation.sample(0,quiet(),quiet());
  near(changedPresentation.sample(dt,{paddle:.01,foreAft:0},quiet()).moment,0,1e-14,'a moving paddle without signed fore/aft presentation cannot invent yaw sign');
  const finiteTrace=()=>{
    controller.reset();const result=[];
    for(let i=0;i<=80;i++){
      const t=i*dt,value=copy(controller.sample(t,control(t,true),control(t,false)));result.push(value);
      assert.deepEqual(controller.sample(t,quiet(),quiet()),value,'a paused age cannot consume another motor observation');
    }
    return result;
  };
  const trace=finiteTrace();assert.deepEqual(finiteTrace(),trace,'reset replays complete motor-rate history');
  for(const age of [-1,2])near(controller.sample(age,{paddle:.025,foreAft:.1},quiet()).moment,0,1e-14,'rewind and unknown command gaps seed without an impulse');
  return {maximum,impulse,retrieval};
}

function taskAt(time,make=createFinTaskController){
  const c=make();if(time<0)return c.reset();
  const events=[[0,'trim'],[.2,'body'],[.24,'trim'],[.32,'body'],[.5,'trim']];
  const times=new Set([0,time,...events.map(([t])=>t).filter(t=>t<=time)]);
  for(let tick=1;tick*dt<time;tick++)times.add(tick*dt);
  let out;
  for(const age of [...times].sort((a,b)=>a-b)){
    const mode=events.findLast(([t])=>age>=t)[1];
    out=c.sample({age,turn:.3,yawError:.1,yawRate:0,stopping:0,mode:'swim',steeringMode:mode});
  }
  return copy(out);
}
function assertTaskJoins(source){
  assert.equal(taskAt(.199999).finTaskMode,'trim');assert.equal(taskAt(.2).finTaskMode,'steer','same signed demand changes its presentation immediately through a handoff');
  assert.equal(taskAt(.32).finTaskMode,'steer','latest same-side mode cancels an obsolete queued trim');
  assert.equal(taskAt(.5).finTaskMode,'trim','body-to-trim also uses its complete finite trajectory');
  const family=[];source.traverse(mesh=>{if(mesh.name.startsWith('Pectoral_'))family.push(mesh);});
  const frames={};for(const mesh of family.filter(mesh=>mesh.name.includes('membrane'))){const f=createFinFrame(mesh.geometry.attributes.position.array,mesh.geometry.attributes.uv.array);frames[f.side]=f;}
  const bindings=family.map(mesh=>({name:mesh.name,binding:bindFinGeometry(mesh.geometry.attributes.position.array,frames),length:mesh.geometry.attributes.position.array.length}));
  for(const mesh of family.filter(mesh=>mesh.name.includes('membrane'))){
    const binding=bindFinGeometry(mesh.geometry.attributes.position.array,frames),uv=mesh.geometry.attributes.uv;
    const neutral={...zero,age:0,forwardSpeed:.1,stroke:0,muscle3:0,pitchFin:0,rollFin:0,yawRate:0,finSteer:0};
    for(const sign of [-1,1]){
      const moved={...neutral,pectoralForeAftPlus:sign*.1,pectoralForeAftMinus:sign*.1};
      const deform=fish=>{const target=new Float32Array(mesh.geometry.attributes.position.array.length);deformFinGeometry(binding,Object.fromEntries([-1,1].map(side=>[side,createFinMotion(fish,side,true,()=>0)])),target);return target;};
      const a=deform(neutral),b=deform(moved);let dx=0,count=0;
      for(let i=0;i<uv.count;i++){
        if(uv.getY(i)<.001){dx+=b[i*3]-a[i*3];count++;}
        if(uv.getY(i)>.999)near(Math.hypot(b[i*3]-a[i*3],b[i*3+1]-a[i*3+1],b[i*3+2]-a[i*3+2]),0,1e-8,'fore/aft rotation preserves each actual attachment point');
      }
      assert.ok(sign*dx/count>.002,'positive actual fore/aft presentation moves each mirrored foil caudally; negative moves it cranially');
    }
  }
  const geometry=time=>{
    const fish={...taskAt(time),age:time,forwardSpeed:.1,stroke:0,muscle3:0,pitchFin:0,rollFin:0,yawRate:0,finSteer:0};
    const motions=Object.fromEntries([-1,1].map(side=>[side,createFinMotion(fish,side,true,()=>0)]));
    return bindings.map(({binding,length})=>{const out=new Float32Array(length);deformFinGeometry(binding,motions,out);return out;});
  };
  // Use Float64 command differentiation for continuity, plus real GLB sheet
  // and ray geometry. Float32 upload quantization sets the geometry floor.
  let maximumVelocityJump=0,maximumAccelerationJump=0;
  for(const boundary of [.2,.32,.44,.5,.62]){
    const derivatives=h=>{
      const samples=[-2,-1,0,1,2].map(k=>taskAt(boundary+k*h));let v=0,a=0;
      for(const field of fields){
        v=Math.max(v,Math.abs((samples[3][field]-2*samples[2][field]+samples[1][field])/h));
        a=Math.max(a,Math.abs((samples[4][field]-2*samples[3][field]+2*samples[1][field]-samples[0][field])/(h*h)));
      }return [v,a];
    };
    const coarse=derivatives(.0002),fine=derivatives(.0001);
    fine.forEach((value,i)=>assert.ok(value<coarse[i]*.64+1e-5,`finite task ${i?'acceleration':'velocity'} joins at ${boundary}: ${value} / ${coarse[i]}`));
    maximumVelocityJump=Math.max(maximumVelocityJump,fine[0]);maximumAccelerationJump=Math.max(maximumAccelerationJump,fine[1]);
    const geometryJump=h=>{
      const samples=[-1,0,1].map(k=>geometry(boundary+k*h));let jump=0;
      for(let m=0;m<bindings.length;m++)for(let i=0;i<samples[0][m].length;i++)jump=Math.max(jump,Math.abs(samples[2][m][i]-2*samples[1][m][i]+samples[0][m][i])/h);
      return jump;
    };
    const coarseGeometry=geometryJump(.001),fineGeometry=geometryJump(.0005);
    assert.ok(fineGeometry<coarseGeometry*.70+.00015,`actual membrane/ray basal velocity joins at ${boundary}: ${fineGeometry} / ${coarseGeometry}`);
  }
  return {maximumVelocityJump,maximumAccelerationJump};
}

function rawRoute(angle,{factory=createFinTaskController,cadence=[dt],cold=false,seconds=3}={}){
  const random=createStudyRandom(913),swimmer=createSwimmer(random,{finTaskFactory:factory});random.reset();swimmer.reset();
  const fish=swimmer.fish;Object.assign(fish,{x:.55,y:.84,z:tankCenterZ,yaw:0});swimmer.navigate({x:-1.05,y:.84,z:tankCenterZ});
  for(let i=0;i<(cold?0:240);i++)swimmer.step(dt);
  const start={...fish};swimmer.navigate({x:-1.05,y:.84,z:fish.z+(fish.x+1.05)*Math.tan(angle)});
  let elapsed=0,frame=0,maximumMoment=0,maximumC=0,quietTicks=0,firstMotor=Infinity,firstHead=Infinity;
  while(elapsed<seconds-1e-12){const step=Math.min(cadence[frame++%cadence.length],seconds-elapsed);swimmer.step(step,f=>{
    const t=f.age-start.age,motor=Math.max(...fields.map(k=>Math.abs(f[k])));
    maximumMoment=Math.max(maximumMoment,Math.abs(f.finYawMoment));maximumC=Math.max(maximumC,Math.abs(f.turnAnterior),Math.abs(f.turnTrunk),Math.abs(f.turnPosterior));
    if(motor<1e-9)quietTicks++;if(motor>1e-5)firstMotor=Math.min(firstMotor,t);if(Math.abs(wrap(f.yaw-start.yaw))>.001)firstHead=Math.min(firstHead,t);
  });elapsed+=step;}
  return {yaw:wrap(fish.yaw-start.yaw),progress:start.x-fish.x,fish:{...fish},maximumMoment,maximumC,quietTicks,firstMotor,firstHead};
}
function assertLiveJoin(){
  const quiet=rawRoute(.03);near(quiet.yaw,0,1e-12,'quiet deadband leaves actual heading alone');near(quiet.maximumMoment,0,1e-12);assert.equal(quiet.firstMotor,Infinity);
  const reports=[];
  for(const angle of [.06,.10,.15,-.06,-.10]){
    // The unresolved .15 course eventually grows beyond the body's .16
    // decision threshold as the fish travels. Compare its fin-only interval.
    const route=rawRoute(angle),masked=rawRoute(angle,{factory:zeroTaskFactory,seconds:Math.abs(angle)>.12?2:3});
    assert.ok(route.yaw*angle>0&&Math.abs(route.yaw)>.5*Math.abs(angle),'finite actual motors make useful restrained course progress');
    near(masked.yaw,0,1e-12,'suppressing all eight issued motors upstream removes the actual head response');near(masked.maximumMoment,0,1e-12,'upstream zero emission removes active yaw');
    assert.ok(route.maximumC<1e-10&&masked.maximumC<1e-10,'small course progress uses fins rather than a hidden C turn');
    assert.ok(route.firstMotor<route.firstHead&&route.quietTicks>30,'visible finite motor entry precedes useful heading change and includes quiet intervals');
    assert.ok(route.progress>.1,'the correction retains forward swimming');reports.push({angle,...route,fish:undefined});
  }
  const plus=rawRoute(.06),minus=rawRoute(-.06);near(plus.yaw,-minus.yaw,1e-8,'small-course steering is mirror symmetric');
  const cold=rawRoute(.10,{cold:true});assert.ok(cold.yaw>.04&&cold.maximumMoment>.1,'a moving finite stroke has authority before appreciable forward flow');
  const repeated=rawRoute(.10);assert.deepEqual(rawRoute(.10),repeated,'seeded reset construction replays motor history');
  const cadence=rawRoute(.10,{cadence:[1/60]});near(cadence.yaw,repeated.yaw,1e-10,'render cadence preserves the same 120 Hz physical integration');
  const fine=rawRoute(.10,{cadence:[1/240]});near(fine.yaw,repeated.yaw,.004,'halving the physical step converges rather than changing steering side');
  return reports;
}

async function actualGeometry(source){
  const report=[];
  for(const angle of [.10,-.10]){
    const random=createStudyRandom(913),instance=createKoiInstance(source,{random}),fish=instance.fish;
    const head=instance.asset.getObjectByName('spine_01'),rest=head.getWorldQuaternion(new Quaternion()).premultiply(instance.asset.getWorldQuaternion(new Quaternion()).invert()).invert();
    random.reset();instance.swimmer.reset();Object.assign(fish,{x:.55,y:.84,z:tankCenterZ,yaw:0});instance.swimmer.navigate({x:-1.05,y:.84,z:tankCenterZ});
    for(let tick=0;tick<240;tick++)instance.step(dt);
    const start={...fish},headYaw=()=>{const v=new Vector3(-1,0,0).applyQuaternion(head.getWorldQuaternion(new Quaternion()).multiply(rest));return Math.atan2(v.z,-v.x);},startHead=headYaw();
    instance.swimmer.navigate({x:-1.05,y:.84,z:fish.z+(fish.x+1.05)*Math.tan(angle)});
    let firstFin=Infinity,firstHead=Infinity,maximumFin=0,minimumClearance=Infinity,vertices=0;
    const bind=new Vector3(),out=new Vector3(),post=new Matrix4();
    for(let tick=1;tick<=240;tick++){
      instance.step(dt);const time=fish.age-start.age;
      const neutral={...fish,...zero},motions=Object.fromEntries([-1,1].map(side=>[side,createFinMotion(neutral,side,true,()=>0)]));
      let movement=0;
      for(const fin of instance.fins.filter(f=>f.paired&&f.pectoral)){
        const flat=new Float32Array(fin.points.array.length);deformFinGeometry(fin.binding,motions,flat);
        for(let i=0;i<flat.length;i+=3)movement=Math.max(movement,Math.hypot(fin.points.array[i]-flat[i],fin.points.array[i+1]-flat[i+1],fin.points.array[i+2]-flat[i+2]));
      }
      maximumFin=Math.max(maximumFin,movement);if(movement>.0001)firstFin=Math.min(firstFin,time);if(Math.abs(wrap(headYaw()-startHead))>.001)firstHead=Math.min(firstHead,time);
      if(tick%30)continue;
      assert.equal(instance.behavior.contacts,0,'finite fin trim needs no final collision fallback');
      const meshes=[];instance.asset.traverse(m=>{if(m.isSkinnedMesh)meshes.push(m);});new Set(meshes.map(m=>m.skeleton)).forEach(s=>s.update());
      for(const mesh of meshes){
        const a=mesh.geometry.attributes,p=mesh.skeleton.boneMatrices;post.multiplyMatrices(mesh.matrixWorld,mesh.bindMatrixInverse);
        for(let i=0;i<a.position.count;i++){
          bind.fromBufferAttribute(a.position,i).applyMatrix4(mesh.bindMatrix);out.set(0,0,0);
          for(let slot=0;slot<4;slot++){const w=a.skinWeight.getComponent(i,slot);if(!w)continue;const e=a.skinIndex.getComponent(i,slot)*16;
            out.x+=w*(p[e]*bind.x+p[e+4]*bind.y+p[e+8]*bind.z+p[e+12]);out.y+=w*(p[e+1]*bind.x+p[e+5]*bind.y+p[e+9]*bind.z+p[e+13]);out.z+=w*(p[e+2]*bind.x+p[e+6]*bind.y+p[e+10]*bind.z+p[e+14]);}
          out.applyMatrix4(post);const clearance=Math.min(out.x-waterMin[0],waterMax[0]-out.x,out.y-waterMin[1],waterMax[1]-out.y,out.z-waterMin[2],waterMax[2]-out.z);
          minimumClearance=Math.min(minimumClearance,clearance);assert.ok(Number.isFinite(clearance)&&clearance>-.000002,`${mesh.name} actual posed vertex remains in water`);vertices++;
        }
      }
    }
    assert.ok(maximumFin>.005,'actual membranes and embedded ray tubes perform a visible finite stroke');
    assert.ok(firstFin<firstHead,'actual GLB fin presentation begins before the actual skull turns one milliradian');
    report.push({angle,firstFin,firstHead,maximumFin,minimumClearance,vertices,yaw:wrap(fish.yaw-start.yaw)});instance.dispose();
  }
  return report;
}

export async function assertFinYaw(){
  const mechanics=assertFinYawMechanics(),live=assertLiveJoin();
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const source=(await new GLTFLoader().register(()=>({name:'FinYawGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())})).parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;
  const joins=assertTaskJoins(source),geometry=await actualGeometry(source);
  // These injectable motor-response regressions exercise outcomes, without
  // mutating a production file or asserting an implementation's spelling.
  const mutations={
    suppressedDrive:()=>{const c=createFinYawController();return {...c,sample(...args){return {...c.sample(...args),moment:0};}};},
    reversedDrive:()=>{const c=createFinYawController();return {...c,sample(...args){const r=c.sample(...args);return {...r,moment:-r.moment};}};},
    quietServo:()=>{const c=createFinYawController();return {...c,sample(...args){const r=c.sample(...args);return {...r,moment:r.moment+.01};}};},
    ignoredPresentation:()=>{const c=createFinYawController();return {...c,sample(age,plus,minus){return c.sample(age,{...plus,foreAft:.1},{...minus,foreAft:.1});}};},
    recoveryDrive:()=>{const c=createFinYawController();return {...c,sample(...args){const r=c.sample(...args);return {...r,moment:r.moment+.1*r.damping};}};}
  };
  for(const [name,make] of Object.entries(mutations))assert.throws(()=>assertFinYawMechanics(make),undefined,`${name} must fail a causal motor-response outcome`);
  return {mechanics,joins,live,geometry,mutations:Object.keys(mutations)};
}
if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){const start=performance.now(),report=await assertFinYaw();console.log(`Fin yaw passed: ${JSON.stringify({seconds:(performance.now()-start)/1000,report})}`);}
