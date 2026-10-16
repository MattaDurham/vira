import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Matrix4} from '../static/vendor/three.webgpu.js';
import {createFinFrame,createFinMotion,createFinController,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';
import {createSwimmer,finActuation,finPhaseRate,finPitchMoment,finRollMoment} from '../static/aquarium-motion.js';

const tau=Math.PI*2,clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const clone=motion=>({...motion,samples:Float64Array.from(motion.samples)});
const pose={speed:.14,forwardSpeed:.14,brake:0,stroke:.6,muscle3:.6,pitch:0,pitchRate:0,pitchFin:0,rollFin:0,finSteer:0};

async function geometry(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'TransitionGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  // The same actual rest skeleton and root transform are held throughout every
  // control crossing; external body rotation cannot conceal a fin-only cusp.
  asset.position.set(0,.8,-.205);asset.rotation.set(0,.3,-.12,'YXZ');asset.rotateX(.04);asset.updateMatrixWorld(true);
  const records=[];
  asset.traverse(mesh=>{
    if(mesh.isSkinnedMesh&&/^(Pectoral|Pelvic)_/.test(mesh.name))records.push({mesh,name:mesh.name,
      pectoral:mesh.name.startsWith('Pectoral'),base:Float32Array.from(mesh.geometry.attributes.position.array),
      uv:Float32Array.from(mesh.geometry.attributes.uv.array)});
  });
  assert.equal(records.length,6,'the transition oracle measures four actual membranes and both combined ray meshes');
  for(const pectoral of [true,false]){
    const family=records.filter(record=>record.pectoral===pectoral),frames={};
    for(const record of family.filter(record=>record.name.includes('membrane'))){
      const frame=createFinFrame(record.base,record.uv);frames[frame.side]=frame;
    }
    for(const record of family){
      record.binding=bindFinGeometry(record.base,frames);record.mesh.skeleton.update();
      const a=record.mesh.geometry.attributes,ids=new Set();
      for(let i=0;i<a.skinWeight.count;i++){
        assert.equal(a.skinWeight.getX(i),1,'actual paired fins have one rigid skeletal attachment');ids.add(a.skinIndex.getX(i));
      }
      assert.equal(ids.size,1,'the independently deformed paired fin follows one actual joint');
      const bone=new Matrix4().fromArray(record.mesh.skeleton.boneMatrices,[...ids][0]*16);
      record.matrix=new Matrix4().multiplyMatrices(record.mesh.matrixWorld,record.mesh.bindMatrixInverse).multiply(bone).multiply(record.mesh.bindMatrix);
    }
  }
  return records;
}

function capture(records,motions,deform,uploaded=false){
  return records.map(record=>{
    const out=new Float64Array(record.base.length);
    deform(record.binding,motions[record.pectoral?'pectoral':'pelvic'],out);
    const e=record.matrix.elements;
    for(let i=0;i<out.length;i+=3){
      const x=uploaded?Math.fround(out[i]):out[i],y=uploaded?Math.fround(out[i+1]):out[i+1],z=uploaded?Math.fround(out[i+2]):out[i+2];
      out[i]=e[0]*x+e[4]*y+e[8]*z+e[12];out[i+1]=e[1]*x+e[5]*y+e[9]*z+e[13];out[i+2]=e[2]*x+e[6]*y+e[10]*z+e[14];
    }
    assert.ok(out.every(Number.isFinite),'actual tissue and ray transition coordinates remain finite');return out;
  });
}

function derivatives(records,samples,h){
  const groups={};
  for(let n=0;n<records.length;n++)for(const side of [-1,1]){
    const record=records[n];let velocity=0,acceleration=0,centralAcceleration=0,count=0;
    for(let i=0;i<record.base.length/3;i++){
      if(Math.sign(record.base[i*3+2])!==side)continue;count++;
      const deltaV=[],deltaA=[];
      for(let axis=0;axis<3;axis++){
        const k=i*3+axis,[a,b,c,d,e]=samples.map(sample=>sample[n][k]);
        deltaV.push((d-2*c+b)/h);
        deltaA.push((e-2*d+2*b-a)/(h*h));
      }
      const jump=Math.hypot(...deltaV);velocity=Math.max(velocity,jump);
      acceleration=Math.max(acceleration,Math.hypot(...deltaA));centralAcceleration=Math.max(centralAcceleration,jump/h);
    }
    if(count)groups[record.name+':'+side]={velocity,acceleration,centralAcceleration};
  }
  return groups;
}

function observe(records,fixture,hz,phase,mode,{motion,controller,deform}){
  const h=1/hz,center=fixture.center??0,fish=t=>({...fixture.fish(t),age:2+t,finPhase:phase});let motions;
  if(mode==='stateless')motions=[-2,-1,0,1,2].map(t=>Object.fromEntries([true,false].map(pectoral=>[
    pectoral?'pectoral':'pelvic',Object.fromEntries([-1,1].map(side=>[side,motion(fish(center+t*h),side,pectoral)]))])));
  else{
    const controls=Object.fromEntries([true,false].map(pectoral=>[pectoral?'pectoral':'pelvic',
      Object.fromEntries([-1,1].map(side=>[side,controller(side,pectoral)]))]));
    motions=[];
    // Stay within the documented 1,000 Hz history capacity. Include the actual
    // crossing observation so delayed commands are not inferred across it.
    const wanted=[-2,-1,0,1,2].map(t=>center+t*h),times=[...wanted];
    for(let tick=-Math.ceil(.3/h);tick<=Math.ceil((center+2*h)/h);tick++){
      const time=tick*h;
      if(time>wanted.at(-1)+1e-10||wanted.some(value=>Math.abs(value-time)<1e-10))continue;
      times.push(time);
    }
    times.sort((a,b)=>a-b);
    for(const time of times){
      const value=Object.fromEntries([true,false].map(pectoral=>[pectoral?'pectoral':'pelvic',
        Object.fromEntries([-1,1].map(side=>[side,clone(controls[pectoral?'pectoral':'pelvic'][side].sample(fish(time)))]))]));
      if(wanted.includes(time))motions.push(value);
    }
  }
  const precise=motions.map(value=>capture(records,value,deform)),uploaded=motions.map(value=>capture(records,value,deform,true));
  let positionError=0;
  for(let t=0;t<precise.length;t++)for(let n=0;n<records.length;n++)for(let i=0;i<precise[t][n].length;i+=3){
    positionError=Math.max(positionError,Math.hypot(...[0,1,2].map(axis=>precise[t][n][i+axis]-uploaded[t][n][i+axis])));
  }
  return {precise:derivatives(records,precise,h),uploaded:derivatives(records,uploaded,h),positionError,hz};
}

// Frozen pre-change control equations are an independent compatibility oracle
// for already-loaded maneuvers. The changed near-zero blend is never duplicated.
function loadedActuation(fish,side,pectoral){
  const speed=Math.abs(fish.forwardSpeed??fish.speed??0),brake=clamp(fish.brake??0,0,1),age=fish.age??0;
  const slow=clamp(1-speed/.18,0,1),turn=clamp((fish.yawRate??0)*.9+(fish.finSteer??0)*.35,-1,1);
  const trim=clamp(Number.isFinite(fish.pitchFin)?fish.pitchFin/.25:(fish.pitchRate??0)*2+(fish.pitch??0)*.15,-.6,.6);
  const stopping=brake*clamp((speed-.025)/.075,0,1),control=clamp(.65*stopping+.75*Math.abs(turn)+.65*Math.abs(trim),0,1);
  const hover=clamp(1-speed/.065,0,1)*(1-clamp((fish.stroke??0)/.3,0,1))
    *Math.max(0,Math.sin(age*.79+1.1*Math.sin(age*.173)+(side<0?1.65:0)+(pectoral?0:1.15)))**10;
  const phase=(fish.finPhase??age*tau*.65)+(side<0?.75:0)+(pectoral?0:1.35),cycle=((phase/tau)%1+1)%1;
  const beat=cycle<.38?Math.sin(Math.PI*cycle/.38)**2:-.28*Math.sin(Math.PI*(cycle-.38)/.62)**2,gain=pectoral?1:.35;
  return {spread:(.035+.07*slow+.18*stopping+.12*Math.max(0,turn*side)+.05*Math.abs(trim))*gain,
    paddle:(.085*control+.14*hover)*beat*gain,sweep:(-.010*speed+.003*(control+.4*hover)*beat)*gain,
    incidence:trim*.25*(pectoral?-1:.35)+(fish.rollFin??0)*side*(pectoral?1:.35),foreAft:0};
}

function loadedMotion(fish,side,pectoral){
  const age=fish.age??0,speed=Math.abs(fish.forwardSpeed??fish.speed??0),phase=fish.finPhase??age*tau*.65;
  const phaseRate=tau*(fish.finPhase===undefined?.65:.55+.35*(fish.brake??0)+.18*Math.abs(fish.yawRate??0));
  const base=loadedActuation(fish,side,pectoral),samples=new Float64Array(66);
  for(let i=0;i<33;i++){
    const delay=i/32*.24,paddle=loadedActuation({...fish,age:age-delay,finPhase:phase-phaseRate*delay},side,pectoral).paddle;
    const loaded=Math.max(0,paddle)/(.06+Math.abs(paddle)),recovery=Math.max(0,-paddle)/(.025+Math.abs(paddle));
    samples[i*2]=(.010*loaded-.003*recovery)*(pectoral?1:.7);samples[i*2+1]=(.50*loaded-.08*recovery)*(pectoral?1:.7);
  }
  const flap=-(base.spread+base.paddle),trail=clamp(speed*.6,0,.22);
  const activation=clamp(fish.poseHistory===false?fish.stroke??0:fish.muscle3??fish.stroke??0,0,1);
  const passive=.0015*clamp(speed/.25,0,1)*(.15+.85*activation)*(pectoral?1:.7);
  return {samples,flap,trail,incidence:base.incidence,passive,flapCos:Math.cos(flap),flapSin:Math.sin(flap),trailCos:Math.cos(trail),trailSin:Math.sin(trail)};
}

export async function assertFinTransitions({motion=createFinMotion,controller=createFinController,deform=deformFinGeometry,
  actuation=finActuation,phaseRate=finPhaseRate,swimmer=createSwimmer,pitchMoment=finPitchMoment,rollMoment=finRollMoment}={}){
  const records=await geometry(),fixtures=[];
  for(const boundary of [-.04,0,.04])fixtures.push({name:'turn '+boundary,fish:t=>({...pose,yawRate:(boundary+t)/.9})});
  for(const boundary of [-.04,0,.04])fixtures.push({name:'loaded trim '+boundary,fish:t=>({...pose,yawRate:.4/.9,brake:.5,pitchFin:.25*(boundary+t)})});
  fixtures.push({name:'loaded yaw clock',fish:t=>({...pose,yawRate:t,finSteer:.3/.35,brake:.5})});
  fixtures.push({name:'loaded zero turn',fish:t=>({...pose,yawRate:t/.9,brake:.5})});
  for(const center of [.12,.195])fixtures.push({name:'delayed turn '+center,center,modes:['history'],fish:t=>({...pose,yawRate:t/.9})});
  const report={maximumFineVelocityJump:0,maximumTargetFineVelocityJump:0,maximumFineAccelerationJump:0,maximumVelocityRatio:0,maximumAccelerationRatio:0,
    maximumUploadedPositionError:0,maximumLoadedPositionChange:0,maximumClockError:0,cases:0};
  for(const fixture of fixtures)for(const phase of [1.2,4.2])for(const mode of fixture.modes??['stateless','history']){
    const coarse=observe(records,fixture,480,phase,mode,{motion,controller,deform});
    const fine=observe(records,fixture,960,phase,mode,{motion,controller,deform});
    for(const name of Object.keys(fine.precise)){
      const a=coarse.precise[name],b=fine.precise[name],uploaded=fine.uploaded[name];
      report.maximumFineVelocityJump=Math.max(report.maximumFineVelocityJump,b.velocity);
      report.maximumFineAccelerationJump=Math.max(report.maximumFineAccelerationJump,b.acceleration);
      report.maximumVelocityRatio=Math.max(report.maximumVelocityRatio,b.velocity/Math.max(a.velocity,1e-8));
      report.maximumAccelerationRatio=Math.max(report.maximumAccelerationRatio,b.acceleration/Math.max(a.acceleration,1e-6));
      assert.ok(b.velocity<a.velocity*.7+1e-8,`actual tissue/ray velocity converges through ${fixture.name}, ${mode}, ${name}`);
      assert.ok(b.acceleration<a.acceleration*.7+1e-6,`actual tissue/ray acceleration joins through ${fixture.name}, ${mode}, ${name}`);
      if(!fixture.center){
        report.maximumTargetFineVelocityJump=Math.max(report.maximumTargetFineVelocityJump,b.velocity);
        assert.ok(b.velocity<.001,'the finely sampled compact target join has no remaining membrane or ray velocity step');
      }
      // The continuous field is tested on actual GLB material points, then its
      // Float32 upload is tested separately. Quantization is not acceleration.
      const epsilon=fine.positionError;
      assert.ok(uploaded.velocity<=b.velocity+4*epsilon*960+1e-9,'uploaded Float32 tissue/ray velocity differs only by its position representation bound');
      assert.ok(uploaded.acceleration<=b.acceleration+6*epsilon*960**2+1e-7,'uploaded Float32 tissue/ray acceleration respects its position representation bound');
    }
    report.maximumUploadedPositionError=Math.max(report.maximumUploadedPositionError,fine.positionError);report.cases++;
  }
  // Existing loaded turns/trim and their actual tissue/rays must remain exactly
  // as authored, rather than passing by reducing every fin's movement globally.
  const loaded=[];
  for(const yawRate of [-.9,-.3,0,.3,.9])for(const pitchFin of [-.1,0,.1])loaded.push({...pose,yawRate,pitchFin,brake:.45,age:2});
  for(const fish of loaded)for(let phase=0;phase<16;phase++){
    const at={...fish,finPhase:phase*tau/16};
    const make=fn=>Object.fromEntries([true,false].map(pectoral=>[pectoral?'pectoral':'pelvic',Object.fromEntries([-1,1].map(side=>[side,fn(at,side,pectoral)]))]));
    const current=capture(records,make(motion),deform,true),original=capture(records,make(loadedMotion),deform,true);
    for(let n=0;n<records.length;n++)for(let i=0;i<current[n].length;i++){
      const difference=Math.abs(current[n][i]-original[n][i]);report.maximumLoadedPositionChange=Math.max(report.maximumLoadedPositionChange,difference);
      assert.ok(difference<1e-12,'outside the compact blend the actual loaded GLB tissue and rays retain their old positions');
    }
    for(const pectoral of [true,false])for(const side of [-1,1])assert.deepEqual(actuation(at,side,pectoral),loadedActuation(at,side,pectoral),
      'loaded signed controls retain their established deployment, paddle, sweep and incidence');
  }
  const turnShape=sign=>capture(records,Object.fromEntries([true,false].map(pectoral=>[pectoral?'pectoral':'pelvic',
    Object.fromEntries([-1,1].map(side=>[side,motion({...pose,age:2,finPhase:1.2,yawRate:sign*.3/.9},side,pectoral)]))])),deform);
  const left=turnShape(-1),right=turnShape(1);
  for(let n=0;n<records.length;n++)for(const side of [-1,1]){
    let count=0,excursion=0;
    for(let i=0;i<records[n].base.length/3;i++)if(Math.sign(records[n].base[i*3+2])===side){
      count++;excursion=Math.max(excursion,Math.hypot(...[0,1,2].map(axis=>left[n][i*3+axis]-right[n][i*3+axis])));
    }
    if(count)assert.ok(excursion>.0005,'each actual membrane and embedded ray family retains independent loaded turning movement');
  }
  for(const pitchFin of [-.15,-.01,-.005,0,.005,.01,.15])for(const rollFin of [-.15,0,.15]){
    const fish={...pose,pitchFin,rollFin,yawRate:.005,brake:.6};
    assert.ok(Math.abs(pitchMoment(fish)-.99*clamp(pitchFin/.15,-1,1))<1e-14,'near-zero demand smoothing preserves the shared signed pitch moment');
    assert.ok(Math.abs(rollMoment(fish)-.99*clamp(rollFin/.15,-1,1))<1e-14,'near-zero demand smoothing preserves the shared signed roll moment');
  }
  // Join the live swimmer's elapsed clock to the same effective clock consumed
  // by fin deformation. Near-zero yaw distinguishes this from the old abs law.
  for(const yawRate of [-.05,-.02,0,.02,.05]){
    const swim=swimmer(()=>.5);Object.assign(swim.fish,{x:0,y:.8,z:-.205,yaw:0,yawRate,brake:.4});
    swim.navigate({x:-.8,y:.8,z:-.205});
    for(let i=0;i<12;i++){
      const previous=swim.fish.finPhase;swim.step(1/960);
      const error=Math.abs((swim.fish.finPhase-previous)*960-phaseRate(swim.fish));
      report.maximumClockError=Math.max(report.maximumClockError,error);
      assert.ok(error<1e-9,'live swimming advances the same effective fin clock used by the rendered response');
    }
  }
  return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  console.log(`Actual paired-fin transitions passed: ${JSON.stringify(await assertFinTransitions())}`);
}
