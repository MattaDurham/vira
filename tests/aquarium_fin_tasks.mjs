import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Matrix4} from '../static/vendor/three.webgpu.js';
import {createFinFrame,createFinMotion,createFinController,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';
import {createFinTaskController,createSwimmer,finActuation,finPhaseRate,finPitchMoment,finRollMoment} from '../static/aquarium-motion.js';
import {tankCenterZ} from '../static/aquarium-space.js';

const fields=['pectoralPaddlePlus','pectoralPaddleMinus','pectoralForeAftPlus','pectoralForeAftMinus',
  'pelvicPaddlePlus','pelvicPaddleMinus','pelvicForeAftPlus','pelvicForeAftMinus'];
const foreAftFields=fields.filter(name=>name.includes('ForeAft')),zeroFields=Object.fromEntries(fields.map(name=>[name,0]));
const dt=1/120,tau=2*Math.PI,clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const physical={speed:.14,forwardSpeed:.14,brake:0,stroke:.6,muscle3:.6,pitch:0,pitchRate:0,pitchFin:0,rollFin:0};
const copyMotion=motion=>({...motion,samples:Float64Array.from(motion.samples)});
const motorValues=value=>fields.map(name=>value[name]);
const magnitude=values=>Math.max(...values.map(Math.abs));
const quietInput=age=>({age,turn:0,yawError:0,yawRate:0,stopping:0,mode:'swim'});
function cue(t,sign=1,base=2){
  const gate=t<=0||t>=1?0:t<.2?Math.sin(Math.PI*.5*t/.2)**2:t<.8?1:Math.cos(Math.PI*.5*(t-.8)/.2)**2;
  return {age:base+t,turn:sign*.36*gate,yawError:sign*.18*gate,yawRate:sign*.12*gate,stopping:0,mode:'swim'};
}
function fishFrom(input,values,finPhase=0){
  return {...physical,...values,age:input.age,yawRate:input.yawRate,finSteer:(input.turn-.9*input.yawRate)/.35,finPhase};
}

async function geometry(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'TaskGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  asset.position.set(0,.8,tankCenterZ);asset.rotation.set(0,.3,-.12,'YXZ');asset.rotateX(.04);asset.updateMatrixWorld(true);
  const records=[];
  asset.traverse(mesh=>{if(mesh.isSkinnedMesh&&/^(Pectoral|Pelvic)_/.test(mesh.name))records.push({mesh,name:mesh.name,
    family:mesh.name.startsWith('Pectoral')?'pectoral':'pelvic',base:Float32Array.from(mesh.geometry.attributes.position.array),uv:Float32Array.from(mesh.geometry.attributes.uv.array)});});
  assert.equal(records.length,6,'task timing is measured on all actual paired membranes and embedded ray meshes');
  for(const family of ['pectoral','pelvic']){
    const fins=records.filter(record=>record.family===family),frames={};
    for(const record of fins.filter(record=>record.name.includes('membrane'))){const frame=createFinFrame(record.base,record.uv);frames[frame.side]=frame;}
    for(const record of fins){
      record.binding=bindFinGeometry(record.base,frames);record.mesh.skeleton.update();
      const a=record.mesh.geometry.attributes,ids=new Set();
      for(let i=0;i<a.skinWeight.count;i++){assert.equal(a.skinWeight.getX(i),1,'actual paired task geometry retains its rigid basal attachment');ids.add(a.skinIndex.getX(i));}
      assert.equal(ids.size,1);const bone=new Matrix4().fromArray(record.mesh.skeleton.boneMatrices,[...ids][0]*16);
      record.matrix=new Matrix4().multiplyMatrices(record.mesh.matrixWorld,record.mesh.bindMatrixInverse).multiply(bone).multiply(record.mesh.bindMatrix);
      record.groups=[];
      for(const side of [-1,1]){
        const indices=[];for(let i=0;i<record.base.length/3;i++)if(Math.sign(record.base[i*3+2])===side&&record.binding.values[i*7+3]>.64)indices.push(i);
        if(indices.length)record.groups.push({side,indices,key:family+':'+(record.name.includes('rays')?'rays':'membrane')+':'+side});
      }
    }
  }
  return records;
}
function controls(create){return Object.fromEntries(['pectoral','pelvic'].map(family=>[family,
  Object.fromEntries([-1,1].map(side=>[side,create(side,family==='pectoral')]))]));}
function observeControls(controllers,fish,key){return Object.fromEntries(['pectoral','pelvic'].map(family=>[family,
  Object.fromEntries([-1,1].map(side=>[side,copyMotion(controllers[family][side].sample(fish,key))]))]));}
function authoredMotions(fish,motion){return Object.fromEntries(['pectoral','pelvic'].map(family=>[family,
  Object.fromEntries([-1,1].map(side=>[side,motion(fish,side,family==='pectoral')]))]));}
function capture(records,motions,deform,uploaded=true){
  return records.map(record=>{
    const values=uploaded?new Float32Array(record.base.length):new Float64Array(record.base.length);deform(record.binding,motions[record.family],values);const e=record.matrix.elements,out=new Float64Array(values.length);
    for(let i=0;i<values.length;i+=3){const x=values[i],y=values[i+1],z=values[i+2];out[i]=e[0]*x+e[4]*y+e[8]*z+e[12];out[i+1]=e[1]*x+e[5]*y+e[9]*z+e[13];out[i+2]=e[2]*x+e[6]*y+e[10]*z+e[14];}
    assert.ok(out.every(Number.isFinite),'actual uploaded task geometry remains finite');return out;
  });
}
function signals(records,current,baseline){
  const values={};for(let n=0;n<records.length;n++)for(const group of records[n].groups){
    let squared=0;for(const i of group.indices)for(let axis=0;axis<3;axis++)squared+=(current[n][i*3+axis]-baseline[n][i*3+axis])**2;
    values[group.key]=Math.sqrt(squared/group.indices.length);
  }return values;
}
function selectedPositions(records,positions){const out=[];for(let n=0;n<records.length;n++)for(const group of records[n].groups){
  for(let j=0;j<8;j++){const i=group.indices[Math.floor(j*(group.indices.length-1)/7)];for(let axis=0;axis<3;axis++)out.push(positions[n][i*3+axis]);}
}return out;}
function difference(a,b){return Math.max(...a.map((value,i)=>Math.abs(value-b[i])));}

function taskContinuity({records,task,controller,deform,capture,physical}){
  const paddleNames=['pectoralPaddlePlus','pectoralPaddleMinus'];
  const eventStart=1,requestEnd=1.2;
  let reversal;
  const request=t=>t>=eventStart&&t<requestEnd?.15:reversal!==undefined&&t>=reversal&&t<reversal+.2?-.15:0;
  const input=t=>({age:t,turn:request(t),yawError:request(t),yawRate:0,stopping:0,mode:'swim'});
  const times=(hz,end,extra=[])=>{
    const values=[...Array.from({length:Math.floor(end*hz)+1},(_,i)=>i/hz),eventStart,requestEnd,...extra];
    if(reversal!==undefined)values.push(reversal,reversal+.2);
    values.push(end);values.sort((a,b)=>a-b);
    return values.filter((v,i)=>v>=0&&v<=end&&(!i||Math.abs(v-values[i-1])>1e-11));
  };
  // Observe recovery rather than duplicating the stroke's support or duration.
  {
    const engine=task(),history=[];let leading,peak=0;
    for(const age of times(960,5)){
      const value={...engine.sample(input(age))};
      if(!leading)leading=paddleNames.find(name=>value[name]>1e-6);
      if(!leading)continue;
      peak=Math.max(peak,value[leading]);history.push({age,value:value[leading]});
      if(history.length<3)continue;
      const [a,b,c]=history.slice(-3);
      if(b.value<-peak*.05&&b.value<a.value&&b.value<=c.value){reversal=b.age;break;}
    }
    assert.ok(reversal,'the isolated task has an observed recovery extremum');
  }
  function replay(hz,end,wanted=[],fixed,basal=false){
    const engine=task(),controls=Object.fromEntries(['pectoral','pelvic'].map(family=>[family,Object.fromEntries([-1,1].map(side=>[side,controller(side,family==='pectoral')]))]));
    const shots=[],uploads=[],trace=[];let motions;
    for(const age of times(hz,end,wanted)){
      const values=engine.sample(input(age)),fish={...physical,...values,age,yawRate:0,finSteer:0,finPhase:1.2};
      motions=Object.fromEntries(['pectoral','pelvic'].map(family=>[family,Object.fromEntries([-1,1].map(side=>[side,controls[family][side].sample(fish)]))]));
      trace.push({age,paddles:paddleNames.map(name=>values[name]),middle:[-1,1].map(side=>{const s=motions.pectoral[side].samples;return s[(Math.floor(s.length/4))*2+1];})});
      if(wanted.some(t=>Math.abs(age-t)<1e-11)){
        const observed=fixed?Object.fromEntries(['pectoral','pelvic'].map(family=>[family,Object.fromEntries([-1,1].map(side=>{
          const samples=Float64Array.from(fixed[family][side].samples),actual=motions[family][side].samples;
          if(basal)return [side,{...motions[family][side],samples}];
          const index=Math.floor(samples.length/4)*2;
          // Isolate one actual observed material response; every other load and
          // basal actuator stays at the observed center, avoiding unrelated
          // history-interpolation knots in other material regions.
          samples[index]=actual[index];samples[index+1]=actual[index+1];
          return [side,{...motions[family][side],...fixed[family][side],samples}];
        }))])):motions;
        shots.push(capture(records,observed,deform,false));uploads.push(capture(records,observed,deform,true));
      }
    }
    return {shots,uploads,trace,motions};
  }
  function measurements(shots,h,side){
    const result={velocity:0,acceleration:0};
    for(let n=0;n<records.length;n++){
      if(records[n].family!=='pectoral')continue;
      for(let i=0;i<records[n].base.length/3;i++){
        if(Math.sign(records[n].base[i*3+2])!==side)continue;
        const v=[],a=[];
        for(let k=0;k<3;k++){
          const j=i*3+k,[x0,x1,x2,x3,x4]=shots.map(s=>s[n][j]);
          v.push((x3-2*x2+x1)/h);a.push((x4-2*x3+2*x1-x0)/(h*h));
        }
        result.velocity=Math.max(result.velocity,Math.hypot(...v));result.acceleration=Math.max(result.acceleration,Math.hypot(...a));
      }
    }
    return result;
  }
  const report={reversal,maximumUploadedPositionError:0,cases:[],boundaries:[]};
  for(const side of [-1,1]){
    const levels=[];
    for(const hz of [480,960]){
      const pilot=replay(hz,reversal+2),channel=side<0?0:1;let bracket;
      for(let i=1;i<pilot.trace.length;i++){
        const a=pilot.trace[i-1],b=pilot.trace[i];
        if(a.age>reversal&&a.middle[channel]<0&&b.middle[channel]>=0){bracket=[a.age,b.age];break;}
      }
      assert.ok(bracket,'the observed distal response crosses recovery into the new power stroke');
      let [low,high]=bracket;
      for(let i=0;i<32;i++){
        const middle=(low+high)/2,value=replay(hz,middle).trace.at(-1).middle[channel];
        if(value<0)low=middle;else high=middle;
      }
      const center=(low+high)/2,h=1/hz,wanted=[-2,-1,0,1,2].map(n=>center+n*h);
      const fixed=replay(hz,center).motions;
      const observed=replay(hz,wanted.at(-1),wanted,fixed),values=measurements(observed.shots,h,side);
      let epsilon=0;
      for(let t=0;t<observed.shots.length;t++)for(let n=0;n<records.length;n++)for(let i=0;i<records[n].base.length;i+=3){
        epsilon=Math.max(epsilon,Math.hypot(...[0,1,2].map(k=>observed.shots[t][n][i+k]-observed.uploads[t][n][i+k])));
      }
      report.maximumUploadedPositionError=Math.max(report.maximumUploadedPositionError,epsilon);
      const uploaded=measurements(observed.uploads,h,side);
      assert.ok(uploaded.velocity<=values.velocity+4*epsilon/h+1e-9,'actual uploaded task geometry respects its represented position velocity bound');
      assert.ok(uploaded.acceleration<=values.acceleration+6*epsilon/(h*h)+1e-7,'actual uploaded task geometry respects its represented position acceleration bound');
      levels.push({hz,center,...values});
    }
    const [coarse,fine]=levels;
    report.cases.push({side,coarse,fine});
    assert.ok(fine.velocity<coarse.velocity*.7+1e-8,'actual task membrane/ray velocity joins at an observed delayed reversal zero '+JSON.stringify(levels));
    assert.ok(fine.acceleration<coarse.acceleration*.7+1e-6,'actual task membrane/ray acceleration joins at an observed delayed reversal zero '+JSON.stringify(levels));
  }
  // Observe the end of the last motor excursion. This does not duplicate its
  // support duration, and it also tests the actual basal entry/exit geometry.
  const ending=replay(960,reversal+3).trace;
  const last=ending.findLastIndex(row=>row.paddles.some(value=>value!==0));
  assert.ok(last>=0&&last<ending.length-1,'the reversal sequence has an observed complete motor return');
  let low=ending[last].age,high=ending[last+1].age;
  for(let i=0;i<32;i++){
    const middle=(low+high)/2,value=replay(960,middle).trace.at(-1).paddles;
    if(value.some(angle=>angle!==0))low=middle;else high=middle;
  }
  for(const center of [eventStart,(low+high)/2])for(const side of [-1,1]){
    const levels=[];
    for(const hz of [480,960]){
      const h=1/hz,wanted=[-2,-1,0,1,2].map(n=>center+n*h),fixed=replay(hz,center).motions;
      const observed=replay(hz,wanted.at(-1),wanted,fixed,true);
      levels.push({hz,...measurements(observed.shots,h,side)});
    }
    const [coarse,fine]=levels;report.boundaries.push({center,side,coarse,fine});
    assert.ok(fine.velocity<coarse.velocity*.7+1e-8,'actual basal tissue/ray velocity joins at the observed task entry and return');
    assert.ok(fine.acceleration<coarse.acceleration*.7+1e-6,'actual basal tissue/ray acceleration joins at the observed task entry and return');
  }
  return report;
}

export async function assertFinTasks({task=createFinTaskController,controller=createFinController,motion=createFinMotion,deform=deformFinGeometry,
  swimmer=createSwimmer,actuation=finActuation,pitchMoment=finPitchMoment,rollMoment=finRollMoment}={}){
  const records=await geometry(),report={maximumPhaseCommandDifference:0,maximumPhaseGeometryDifference:0,minimumActiveExcursion:Infinity,
    maximumQuietExcursion:0,maximumForeAftDisplacement:0,realActiveFrames:0,quietRealFrames:0};
  function run(sign=1,phaseOffset=0,baseAge=2){
    const engine=task(),fins=controls(controller),trace={commands:[],frames:[],groups:{},peakFish:null},phaseStart=phaseOffset;
    let phase=phaseStart,foreAftPeak=0;
    for(let tick=-48;tick<=4*120;tick++){
      const t=tick*dt,input=cue(t,sign,baseAge),values={...engine.sample(input)};
      assert.ok(motorValues(values).every(Number.isFinite),'the task emits all eight finite motor angles');
      const fish=fishFrom(input,values,phase),motions=observeControls(fins,fish);
      phase+=finPhaseRate(fish)*dt;
      if(t<0)continue;
      trace.commands.push(motorValues(values));
      const sweep=magnitude(foreAftFields.map(name=>fish[name]));if(sweep>foreAftPeak){foreAftPeak=sweep;trace.peakFish={...fish};}
      if(tick%3)continue;
      const current=capture(records,motions,deform),neutral=capture(records,authoredMotions({...fish,...zeroFields},motion),deform);
      const noCompliance=Object.fromEntries(['pectoral','pelvic'].map(family=>[family,Object.fromEntries([-1,1].map(side=>[side,
        {...motions[family][side],samples:new Float64Array(motions[family][side].samples.length)}]))]));
      const relaxed=capture(records,noCompliance,deform),compliance=signals(records,current,relaxed),active=signals(records,current,neutral);
      const instantCompliance=signals(records,capture(records,authoredMotions(fish,motion),deform),relaxed);
      trace.frames.push({t,positions:selectedPositions(records,current)});
      for(const key of Object.keys(active)){
        if(!trace.groups[key])trace.groups[key]=[];
        const [family,,side]=key.split(':');
        trace.groups[key].push({t,value:active[key],compliance:compliance[key],instantCompliance:instantCompliance[key],paddle:actuation(fish,+side,family==='pectoral').paddle});
      }
    }
    for(const [key,series] of Object.entries(trace.groups)){
      const peak=Math.max(...series.map(row=>row.value));report.minimumActiveExcursion=Math.min(report.minimumActiveExcursion,peak);
      assert.ok(peak>.00005,`actual ${key} participates in the finite steering gesture`);
      const late=series.filter(row=>row.t>2.8),quiet=Math.max(...late.map(row=>row.value));report.maximumQuietExcursion=Math.max(report.maximumQuietExcursion,quiet);
      assert.ok(quiet<Math.max(1e-8,peak*.001),'completed commands and their observed tissue history settle into a quiet glide');
      const firstPaddle=series.find(row=>Math.abs(row.paddle)>1e-6)?.t,peakCompliance=Math.max(...series.map(row=>row.compliance));
      assert.ok(peakCompliance>1e-6,'the finite basal command actually recruits distal membrane and ray compliance');
      const firstCompliance=series.find(row=>row.compliance>Math.max(1e-8,peakCompliance*.001))?.t;
      assert.ok(firstCompliance>firstPaddle+dt,'actual distal compliance follows an observed basal task pulse rather than invented past loading');
      assert.ok(series.some(row=>row.instantCompliance>1e-6&&row.compliance<row.instantCompliance*.001),
        'actual distal material waits while an instantaneous projection would already load it');
      assert.ok(series.some(row=>Math.abs(row.paddle)<1e-10&&row.compliance>1e-6),
        'actual distal material completes its observed response after the basal task pulse has ended');
    }
    return trace;
  }
  const positive=run(1),negative=run(-1);
  for(const phaseOffset of [Math.PI/2,Math.PI,3*Math.PI/2]){
    const shifted=run(1,phaseOffset,phaseOffset===Math.PI?9:2);
    for(let i=0;i<positive.commands.length;i++)report.maximumPhaseCommandDifference=Math.max(report.maximumPhaseCommandDifference,difference(positive.commands[i],shifted.commands[i]));
    for(let i=0;i<positive.frames.length;i++)report.maximumPhaseGeometryDifference=Math.max(report.maximumPhaseGeometryDifference,difference(positive.frames[i].positions,shifted.frames[i].positions));
  }
  assert.ok(report.maximumPhaseCommandDifference<1e-10,'task-relative bilateral motor timing is independent of the unrelated carrier phase and absolute age');
  assert.ok(report.maximumPhaseGeometryDifference<2e-7,'actual membrane and ray task trajectories retain phase-independent timing after upload and skinning');
  const onset=series=>{const peak=Math.max(...series.map(row=>row.value));return series.find(row=>row.value>peak*.15)?.t;};
  for(const family of ['pectoral','pelvic'])for(const kind of ['membrane','rays']){
    const plus=family+':'+kind+':1',minus=family+':'+kind+':-1';
    const forward=onset(positive.groups[plus])-onset(positive.groups[minus]),reverse=onset(negative.groups[plus])-onset(negative.groups[minus]);
    assert.ok(forward*reverse<0,'mirrored steering reverses side-specific actual tissue/ray onset order without imposing one universal leading side');
  }
  // The new fore/aft motor must affect the real mesh, in addition to rowing.
  const peak=positive.peakFish;assert.ok(peak,'the gesture contains an explicit fore/aft presentation');
  const absent={...peak,...Object.fromEntries(foreAftFields.map(name=>[name,0]))};
  const activeSweep=capture(records,authoredMotions(peak,motion),deform),noSweep=capture(records,authoredMotions(absent,motion),deform);
  for(let n=0;n<records.length;n++){
    let maximum=0;for(let i=0;i<activeSweep[n].length;i++)maximum=Math.max(maximum,Math.abs(activeSweep[n][i]-noSweep[n][i]));
    report.maximumForeAftDisplacement=Math.max(report.maximumForeAftDisplacement,maximum);
    if(records[n].family==='pectoral')assert.ok(maximum>.0001,'the fore/aft task command rotates actual pectoral tissue and embedded rays');
  }
  // Finite strokes change neither signed control couples nor held anatomy.
  for(const pitchFin of [-.15,-.005,0,.005,.15])for(const rollFin of [-.15,0,.15]){
    const fish={...peak,pitchFin,rollFin};
    assert.ok(Math.abs(pitchMoment(fish)-.99*clamp(pitchFin/.15,-1,1))<1e-14,'finite task motion preserves the shared signed pitch moment');
    assert.ok(Math.abs(rollMoment(fish)-.99*clamp(rollFin/.15,-1,1))<1e-14,'finite task motion preserves the shared signed roll moment');
    for(const pectoral of [true,false])for(const side of [-1,1]){
      const held={...fish,poseHistory:false},untracked={...held,...Object.fromEntries(fields.map(name=>[name,undefined]))};
      assert.deepEqual(actuation(held,side,pectoral),actuation(untracked,side,pectoral),'held anatomy ignores stale live task commands');
    }
    const current=authoredMotions(fish,motion),reference=Object.fromEntries(['pectoral','pelvic'].map(family=>[family,
      Object.fromEntries([-1,1].map(side=>[side,{...current[family][side],incidence:actuation({...fish,poseHistory:false},side,family==='pectoral').incidence}]))]));
    const rendered=capture(records,current,deform),expected=capture(records,reference,deform);
    for(let n=0;n<records.length;n++)assert.ok(difference(Array.from(rendered[n]),Array.from(expected[n]))<2e-7,
      'active task tissue and rays retain the established signed pitch/roll incidence');
  }
  const center=capture(records,authoredMotions({...peak,pitchFin:0,rollFin:0},motion),deform);
  for(const name of ['pitchFin','rollFin']){
    const plus=capture(records,authoredMotions({...peak,pitchFin:0,rollFin:0,[name]:.1},motion),deform);
    const minus=capture(records,authoredMotions({...peak,pitchFin:0,rollFin:0,[name]:-.1},motion),deform);
    for(let n=0;n<records.length;n++)for(const group of records[n].groups){
      let opposed=0,energy=0;
      for(const i of group.indices)for(let axis=0;axis<3;axis++){
        const k=i*3+axis,a=plus[n][k]-center[n][k],b=minus[n][k]-center[n][k];opposed+=a*b;energy+=a*a;
      }
      assert.ok(opposed<0&&Math.sqrt(energy/group.indices.length)>.0001,
        'signed pitch/roll still creates opposite actual tissue/ray presentations during a finite stroke');
    }
  }
  // Frozen observations cannot queue a different future side. Verify the whole
  // resumed trace, not just the repeated-age output object.
  const a=task(),b=task();
  for(let i=-48;i<=48;i++){const input=cue(i*dt);a.sample(input);b.sample(input);}
  const before={...a.sample(cue(.4))},identity=a.sample(cue(.4));
  for(let i=0;i<200;i++)assert.strictEqual(a.sample({...cue(.4),turn:i%2?-.7:.8,yawError:-.4,mode:'retreat'}),identity,'a paused task observation reuses its state and output identity');
  assert.deepEqual(motorValues(identity),motorValues(before),'inspection at a frozen age cannot alter motor angles');
  for(let i=49;i<=4*120;i++)assert.deepEqual(motorValues(a.sample(cue(i*dt))),motorValues(b.sample(cue(i*dt))),'paused inspection cannot leave a latent future task or side');
  for(const reset of ['explicit','rewind','gap']){
    const used=task();for(let i=0;i<80;i++)used.sample({...cue(i*dt),turn:i>35?-.4:.4,yawError:i>35?-.2:.2});
    if(reset==='explicit')used.reset();
    const base=reset==='explicit'?2+79*dt:reset==='gap'?10:0,fresh=task();
    for(let i=0;i<3*120;i++){
      const input=cue(i*dt,-1,base);
      assert.deepEqual(motorValues(used.sample(input)),motorValues(fresh.sample(input)),'reset, rewind and unknown gaps discard all prior task and handoff state');
    }
  }
  // Real navigation must emit and consume this same motor repertoire. Compare
  // independent carrier phases and reset with real physical simulation ticks.
  function real(phaseOffset=0){
    const swim=swimmer(()=>.5);Object.assign(swim.fish,{x:.55,y:.84,z:tankCenterZ,yaw:0,finPhase:phaseOffset});
    swim.navigate({x:-.85,y:.84,z:tankCenterZ+.18});const output={commands:[],frames:[]},fins=controls(controller);let maximumExcursion=0;
    for(let frame=0;frame<6*120;frame++){
      swim.step(dt);const fish={...swim.fish};
      assert.ok(motorValues(fish).every(Number.isFinite),'real swimming emits the eight task angles rather than falling through to the carrier');
      if(magnitude(motorValues(fish))>1e-5)report.realActiveFrames++;
      for(const pectoral of [true,false])for(const side of [-1,1]){
        const name=(pectoral?'pectoral':'pelvic')+'Paddle'+(side>0?'Plus':'Minus');
        assert.ok(Math.abs(actuation(fish,side,pectoral).paddle-fish[name])<1e-14,'real emitted task strokes reach the basal command consumed by the renderer');
      }
      output.commands.push(motorValues(fish));const motions=observeControls(fins,fish);
      if(frame%12===0){
        const actual=capture(records,motions,deform),neutral=capture(records,authoredMotions({...fish,...zeroFields},motion),deform);
        maximumExcursion=Math.max(maximumExcursion,...Object.values(signals(records,actual,neutral)));
        output.frames.push(selectedPositions(records,actual));
      }
    }
    assert.ok(maximumExcursion>.00005,'real navigation strokes actually deform the GLB rather than only updating task metadata');
    swim.reset();assert.ok(magnitude(motorValues(swim.fish))===0,'real reset clears active and queued fin tasks');return output;
  }
  const first=real(),shifted=real(Math.PI);
  for(let i=0;i<first.commands.length;i++)assert.ok(difference(first.commands[i],shifted.commands[i])<1e-10,'actual navigation chooses task timing independently of the decorative carrier clock');
  for(let i=0;i<first.frames.length;i++)assert.ok(difference(first.frames[i],shifted.frames[i])<2e-7,'real navigation retains phase-independent membrane and ray trajectories after upload');
  assert.ok(report.realActiveFrames>0,'the real swimmer actually performs a finite steering task');
  const settling=swimmer(()=>.5),settledFins=controls(controller);
  Object.assign(settling.fish,{x:.55,y:.84,z:tankCenterZ,yaw:0});settling.navigate({x:-.85,y:.84,z:tankCenterZ});
  let arrivalDecision,quietSince,quietRun=0,maximumQuietRun=0;
  for(let frame=0;frame<25*120;frame++){
    settling.step(dt);const fish={...settling.fish},behavior=settling.behavior,motions=observeControls(settledFins,fish);
    if(arrivalDecision===undefined&&behavior.intent==='hover')arrivalDecision=behavior.decisions;
    if(arrivalDecision!==undefined&&behavior.decisions!==arrivalDecision)break;
    // A stopped root or the hover label alone is insufficient: an animal can
    // still be actively turning. Require observed angular and translational rest.
    const quiet=arrivalDecision!==undefined&&Math.abs(fish.yawRate)<.03&&Math.abs(fish.pitchRate)<.03&&fish.speed<.01;
    if(!quiet){quietSince=undefined;quietRun=0;continue;}
    quietSince??=fish.age;if(fish.age-quietSince<.8||frame%12)continue;
    const actual=capture(records,motions,deform),neutral=capture(records,authoredMotions({...fish,...zeroFields},motion),deform);
    if(Math.max(...Object.values(signals(records,actual,neutral)))<2e-7){
      report.quietRealFrames++;quietRun++;maximumQuietRun=Math.max(maximumQuietRun,quietRun);
    }else quietRun=0; // Sparse finite hover-balance corrections remain allowed.
  }
  assert.ok(maximumQuietRun>=6,'real navigation reaches a continuous observed quiet interval before making its next behavioral decision');
  report.continuity=taskContinuity({records,task,controller,deform,capture,physical});
  return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href)console.log(`Actual finite fin tasks passed: ${JSON.stringify(await assertFinTasks())}`);
