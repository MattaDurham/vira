/* Experimental offline water-comparison reproducer. It writes recorded states;
 * the ordinary aquarium renderer and behavior remain separate. */
import assert from 'node:assert/strict';
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {performance} from 'node:perf_hooks';
import {resolve,relative} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {Texture,Vector3,Quaternion,Matrix4} from '../../../static/vendor/three.webgpu.js';
import {GLTFLoader} from '../../../static/vendor/GLTFLoader.js';
import {createAxialFrame} from '../../../static/aquarium-axial-frame.js';
import {createRapierChain} from '../../../static/aquarium-articulated.js';
import {createTurnController} from '../../../static/aquarium-turn.js';
import {buildBodySurfaceSamples,appendRigidMembraneSurface,createDissipativeWaterImpulse,rapierWaterStates} from './water.mjs';
import {createMuscleWaterImpulse} from './muscle-water.mjs';
import {createObservedTurnCue} from './observed-cue.mjs';
import {createPhysicalScaleAdapter} from './physical-scale.mjs';

const root=fileURLToPath(new URL('../../../',import.meta.url));
const caseNames=['hinge','bounded','source13cm'];
const rates=[240,480,960];
const hash=bytes=>createHash('sha256').update(bytes).digest('hex');
const vector=value=>[value.x,value.y,value.z];
const normalizedQuaternion=value=>new Quaternion(value.x,value.y,value.z,value.w).normalize();
const logQuaternion=value=>{
  const q=value.clone();if(q.w<0)q.set(-q.x,-q.y,-q.z,-q.w);
  const length=Math.hypot(q.x,q.y,q.z),factor=length<1e-14?2:2*Math.atan2(length,q.w)/length;
  return [q.x*factor,q.y*factor,q.z*factor];
};
const modulePaths=[
  'scripts/aquarium/water-study/generate.mjs','scripts/aquarium/water-study/muscle-water.mjs',
  'scripts/aquarium/water-study/water-body.mjs','scripts/aquarium/water-study/water.mjs',
  'scripts/aquarium/water-study/angular.mjs','scripts/aquarium/water-study/observed-cue.mjs',
  'scripts/aquarium/water-study/physical-scale.mjs','static/aquarium-articulated.js',
  'static/aquarium-axial-frame.js','static/aquarium-spine.js','static/aquarium-turn.js',
  'static/vendor/three.webgpu.js','static/vendor/three.core.js','static/vendor/GLTFLoader.js',
  'static/vendor/BufferGeometryUtils.js','static/vendor/rapier.mjs',
];

export function parseOptions(args){
  const options={caseName:'all',rate:960,out:resolve(root,'.aquarium-build/water-study-reproduced')};
  for(const argument of args){
    if(argument==='--help'){options.help=true;continue;}
    const match=/^--(case|rate|out)=(.+)$/.exec(argument);
    if(!match)throw new Error(`Unknown or empty argument: ${argument}`);
    if(match[1]==='case')options.caseName=match[2];
    if(match[1]==='rate')options.rate=Number(match[2]);
    if(match[1]==='out')options.out=resolve(match[2]);
  }
  if(!['all',...caseNames].includes(options.caseName))throw new Error('Case must be hinge, bounded, source13cm or all');
  if(!rates.includes(options.rate))throw new Error('Rate must be 240,480 or960 Hz');
  return options;
}

export function validateInputs(inputs){
  if(inputs?.schemaVersion!==1||!inputs.payload||!inputs.provenance)throw new Error('Water-study input schema1 required');
  const extractSHA256=hash(JSON.stringify(inputs.payload));
  if(extractSHA256!==inputs.provenance.extractSHA256)throw new Error('Compact input extract checksum does not match');
  const {regions,capacity,observedCue}=inputs.payload,frames=observedCue?.trials?.fast?.frames;
  if(regions?.length!==33||regions.some(r=>r.x?.length!==2||!r.x.every(Number.isFinite)||r.x[0]>=r.x[1]))throw new Error('33 finite water X intervals required');
  if(capacity?.fraction050?.length!==28||capacity.fraction050.some(v=>!Number.isFinite(v)||v<=0))throw new Error('28 positive lateral capacities required');
  if(frames?.length!==69||frames.some((f,i)=>!Number.isFinite(f.time)||(i&&f.time<=frames[i-1].time)||f.b?.length!==3||!f.b.every(Number.isFinite)||![f.headReference,f.tailReference,...(f.authorCOMOverL??[])].every(Number.isFinite)||f.authorCOMOverL?.length!==2))throw new Error('69 exact observed-cue frames required');
  return inputs;
}

async function loadContext(){
  const inputBytes=await readFile(new URL('./inputs.json',import.meta.url));
  const inputs=validateInputs(JSON.parse(inputBytes.toString('utf8')));
  const assetBytes=await readFile(resolve(root,'static/aquarium/koi.glb'));
  const massBytes=await readFile(resolve(root,'static/aquarium/body-mass.json'));
  if(hash(assetBytes)!==inputs.provenance.assetSHA256||hash(massBytes)!==inputs.provenance.massSHA256)throw new Error('Pinned fish asset or mass profile changed; regenerate reviewed inputs explicitly');
  const loader=new GLTFLoader().register(()=>({name:'WaterStudyGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(assetBytes.buffer.slice(assetBytes.byteOffset,assetBytes.byteOffset+assetBytes.byteLength),'');
  const massProfile=JSON.parse(massBytes.toString('utf8')),sampler=createAxialFrame(asset);
  const sectionProfile={...massProfile,regions:inputs.payload.regions};
  const surface=appendRigidMembraneSurface(asset,buildBodySurfaceSamples(asset,sectionProfile),sectionProfile);
  const moduleSHA256=Object.fromEntries(await Promise.all(modulePaths.map(async path=>[path,hash(await readFile(resolve(root,path)))])));
  return {inputs,inputBytes,assetBytes,massBytes,asset,massProfile,sampler,surface,moduleSHA256};
}

async function hingeRun(context,rate){
  const {asset,sampler:frame,massProfile:profile}=context;
  // The original witness builds its water regions from actual neutral extrema,
  // independently of the compact section profile used by the shared cases.
  asset.updateMatrixWorld(true);const skin=asset.getObjectByName('Koi_skin');skin.skeleton.update();
  const inverse=new Matrix4().copy(asset.matrixWorld).invert(),point=new Vector3();let xmin=Infinity,xmax=-Infinity;
  for(let i=0;i<skin.geometry.attributes.position.count;i++){
    skin.getVertexPosition(i,point).applyMatrix4(skin.matrixWorld).applyMatrix4(inverse);xmin=Math.min(xmin,point.x);xmax=Math.max(xmax,point.x);
  }
  const waterProfile={...profile,regions:Array.from({length:33},(_,i)=>({x:[i===0?xmin:frame.centers[i].x,i===32?xmax:frame.centers[i+1].x]}))};
  const surface=appendRigidMembraneSurface(asset,buildBodySurfaceSamples(asset,waterProfile),waterProfile);
  const mass=profile.bodies29.map(body=>({mass:body.mass,inertia:body.inertia}));
  const chain=await createRapierChain(frame,profile,{dt:1/rate,naturalFrequency:360,dampingRatio:16,motorImplementation:'sdk',capAngle:.15,iterations:16,internalIterations:2});
  try{
    const operator=createDissipativeWaterImpulse(surface,mass),states=rapierWaterStates(chain),turn=createTurnController();
    turn.request(Math.PI,0,{urgent:true});chain.configure('active');let worst={angle:0};const frames=[];
    for(let tick=0;tick<3*rate;tick++){
      const time=tick/rate,gesture=turn.sample(time),b=[...gesture.b];
      chain.setTargets({phase:0,stroke:0,muscle1:0,muscle2:0,muscle3:0,turnAnterior:b[0],turnTrunk:b[1],turnPosterior:b[2]});
      chain.step(1,()=>{
        rapierWaterStates(chain,states);const result=operator.step(states,1/rate,time);
        for(let i=0;i<29;i++){chain.bodies[i].applyImpulse(result.linearImpulses[i],true);chain.bodies[i].applyTorqueImpulse(result.angularImpulses[i],true);}
      });
      let largest={angle:0};
      for(const [index,link] of chain.links.entries()){
        const A=normalizedQuaternion(chain.bodies[link.parent].rotation()),B=normalizedQuaternion(chain.bodies[link.child].rotation());
        const relativeRotation=A.clone().invert().multiply(B),rotationVector=logQuaternion(relativeRotation),angle=Math.hypot(...rotationVector);
        const childOmega=chain.bodies[link.child].angvel(),parentOmega=chain.bodies[link.parent].angvel();
        if(angle>largest.angle)largest={index,parentBody:link.parent,childBody:link.child,childBone:chain.records[link.child].panel,angle,rotationVector,
          targetRotationVector:logQuaternion(link.target),errorRotationVector:logQuaternion(relativeRotation.clone().multiply(link.target.clone().invert())),
          relativeOmegaParent:new Vector3(childOmega.x-parentOmega.x,childOmega.y-parentOmega.y,childOmega.z-parentOmega.z).applyQuaternion(A.clone().invert()).toArray(),
          stiffness:link.stiffness,damping:link.damping,cap:link.cap,effective:link.effective};
      }
      if(largest.angle>worst.angle)worst={time:chain.age,b:[...b],...largest};
      // Preserve the historical post-step sampling offset; do not manufacture
      // an initial/final sample absent from this original hinge witness.
      if(tick%Math.max(1,rate/120)===0){
        const bones=chain.boneTransforms(),measured=chain.measure(),head=chain.bodies[0],tail=chain.bodies[28];
        const bodyRotations=chain.bodies.map(body=>{const q=body.rotation();return[q.x,q.y,q.z,q.w];});
        frames.push({time:chain.age,b:[...b],bonePositions:bones.positions.map(p=>p.toArray()),boneRotations:bones.rotations.map(q=>q.toArray()),
          bodyPositions:chain.bodies.map(body=>vector(body.translation())),bodyRotations,headQuaternion:[...bodyRotations[0]],
          headOmega:vector(head.angvel()),tailOmega:vector(tail.angvel()),com:measured.com.toArray(),kineticEnergy:measured.kineticEnergy,
          springDiagnostic:measured.springEstimate,anchorError:measured.anchorError,largestJoint:largest});
      }
    }
    return {run:{wet:true,rate,initialSpeed:0,request:{angle:Math.PI,urgent:true,start:0},worst,frames},
      definition:{chain:'public aquarium-articulated.js',frame:'public aquarium-axial-frame.js',water:'sequentialBE641body+67actualcaudal; rho1000/Cn1/Ct.02; no rootservo orposeoverride',
        settings:{naturalFrequency:360,dampingRatio:16,motorImplementation:'sdk',capAngle:.15,iterations:16,internalIterations:2,jointLimits:null},
        units:'metres, seconds, world absolute quaternions; bone rest orientations included; no extra root transform',frameRate:120,sourcePreparation:.36,
        restCenters:frame.centers.map(p=>p.toArray()),restRotations:frame.rest.map(q=>q.toArray())}};
  }finally{chain.free();}
}

async function sharedRun(context,caseName,rate){
  const {inputs,asset,massProfile:nativeMassProfile,sampler:nativeSampler,surface:nativeSurface}=context;
  const physical13cm=caseName==='source13cm',movingSource=physical13cm,sourceFast=physical13cm,range016=true,capacityHalf=true;
  const nativeMass=nativeMassProfile.bodies29,adapter=physical13cm?createPhysicalScaleAdapter({asset,physicalLengthM:.13}):null;
  const sampler=adapter?adapter.scaleSampler(nativeSampler):nativeSampler,massProfile=adapter?adapter.scaleMassProfile(nativeMassProfile):nativeMassProfile;
  const mass=massProfile.bodies29,surface=adapter?adapter.scaleSurface(nativeSurface):nativeSurface;
  let nativeBaselineLinks;
  if(adapter){
    const baseline=await createRapierChain(nativeSampler,nativeMassProfile,{dt:1/240,naturalFrequency:360,dampingRatio:16,motorImplementation:'sdk',capAngle:.15,iterations:16,internalIterations:2});
    try{baseline.configure('off');nativeBaselineLinks=baseline.links.map(({stiffness,damping,cap})=>({stiffness,damping,cap}));}finally{baseline.free();}
  }
  const ease=x=>{x=Math.max(0,Math.min(1,x));return x*x*x*(10+x*(-15+6*x));};
  const angle=q=>2*Math.atan2(Math.hypot(q.x,q.y,q.z),Math.abs(q.w)),deg=180/Math.PI,phases=[.20,.35,.52,.60,1.10,1.60,3];
  const observed=sourceFast?createObservedTurnCue(inputs.payload.observedCue,{trial:'fast'}):null;
  const capacity=inputs.payload.capacity.fraction050;
  const sourceInput=file=>inputs.provenance.sourceFiles.find(source=>source.file===file);
const scaleMetadata=adapter?{physicalLengthM:adapter.physicalLengthM,nativeLengthM:adapter.nativeMeasurement.length,S:adapter.S,S2:adapter.S2,S3:adapter.S3,S5:adapter.S5,nativeBodyMassKg:nativeMass.reduce((sum,x)=>sum+x.mass,0),physicalBodyMassKg:mass.reduce((sum,x)=>sum+x.mass,0),surfaceAreaSquareMeters:surface.totalArea,activeLaw:'native giant K,D,cap multiplied by S^3; constructor inertia S^5 law explicitly replaced while SDK remains OFF; then Y-only .50 capacity ratio',density:'unchanged',sourceClock:'unchanged physical seconds',render:'frame bonePositions normalized by 1/S into native asset coordinates; bonePositionsMeters and bodyPositions/com remain SI',scope:'13 cm uniformly scaled koi proxy, source bluegill shape and initial SI velocity; authored active capacity and resistive water, no species or dynamic-similarity calibration'}:null;
const initialStateMetadata={velocityMetersPerSecond:movingSource?[-.272926,0,-.0509592]:[0,0,0],angularVelocityRadiansPerSecond:[0,0,0],basis:'own initial preframe head; initial modelheadidentity',scope:movingSource?(physical13cm?'source own-head SI initialcondition on 13cm scaled koi proxy; no dynamic-similarity claim':'source fixed-m/s causal initialcondition on giant1.495m animal; no dynamic-similarity claim'):'rest'};
const cueMetadata=sourceFast?{kind:'clarkfast',trial:observed.metadata.ID,sourceClock:observed.metadata,fitFile:'scripts/aquarium/water-study/inputs.json#payload.observedCue',fitSHA256:sourceInput('clark-turn-shape-fit.json').sha256,headAndCOMPrescribed:false,requestedHeadingBudget:null}:{kind:'reciprocal',regionalPeak:[2.4,2.4,2.4],loadSeconds:.35,holdUntilSeconds:.60,releaseSeconds:.50,endSeconds:1.10,headAndCOMPrescribed:false,requestedHeadingBudget:null},capacityMetadata=capacityHalf?{fraction:.50,axis:'Y',profileFile:'scripts/aquarium/water-study/inputs.json#payload.capacity',profileSHA256:sourceInput('axial-capacity-profile.json').sha256,scaling:'parent-Y K,D,cap scaled by capNew/capOld; X/Z baseline',lateralTorqueCapsNm:adapter?adapter.scaleTorqueCaps(capacity):capacity}:null;
const physicalCapacity=adapter?adapter.scaleTorqueCaps(capacity):capacity;
 const dt=1/rate;
 const chain=await createRapierChain(sampler,{bodies29:mass},{dt,naturalFrequency:360,dampingRatio:16,motorImplementation:'sdk',jointLimits:range016?{x:.16,y:.16,z:.16}:undefined,iterations:16,internalIterations:2,capAngle:.15});
 try{
 chain.configure('off');if(adapter)adapter.applyActiveLaw(chain,nativeBaselineLinks);if(movingSource)chain.setWholeVelocity(new Vector3(-.272926,0,-.0509592),new Vector3());const operator=createMuscleWaterImpulse(surface,mass,chain.links,{lateralTorqueCapsNm:physicalCapacity}),states=rapierWaterStates(chain),initial=chain.measure(),frames=[],start=performance.now();let workMotor=0,workFluid=0,workIntrinsic=0,workWater=0,lossBE=0,worldStepEnergy=0,positiveWorldStepEnergy=0,negativeWorldStepEnergy=0,maxWorldStepInjection=0,maxRangeCoordinate=0,maxRangeOvershoot=0,maxKickEnergyError=0,maxWorkIdentity=0,maxJV=0,maxGap=0,maxReactionP=0,maxReactionH=0,maxIterations=0,kickMs=0,peakRelative=0,peakTime=0,maxLoaded=0,maxJoint=0,maxAnchor=0,lastResult,previousTime=0,previousHead=new Quaternion(),previousTail=new Quaternion();const phaseData={},worst={angle:0,childBone:0,time:0};
 function save(time,A){const m=chain.measure(),physicalTransforms=chain.boneTransforms(),tr=adapter?adapter.toRenderTransforms(physicalTransforms):physicalTransforms,bodyRotations=chain.bodies.map(b=>{const q=b.rotation();return[q.x,q.y,q.z,q.w];}),bodyPositions=chain.bodies.map(b=>{const p=b.worldCom();return[p.x,p.y,p.z];});let largestJoint={angle:0,childBone:0};for(const l of chain.links){const a=new Quaternion(...bodyRotations[l.parent]),b=new Quaternion(...bodyRotations[l.child]),r=angle(a.invert().multiply(b));if(r>largestJoint.angle)largestJoint={angle:r,childBone:chain.records[l.child].panel};}if(largestJoint.angle>worst.angle)Object.assign(worst,largestJoint,{time});const w=chain.bodies[0].angvel();frames.push({time,b:Array.isArray(A)?[...A]:[A,A,A],bonePositions:tr.positions.map(p=>p.toArray()),...(adapter?{bonePositionsMeters:physicalTransforms.positions.map(p=>p.toArray()),renderBodyPositions:bodyPositions.map(p=>p.map(x=>x/adapter.S)),renderCOM:m.com.toArray().map(x=>x/adapter.S)}:{}),boneRotations:tr.rotations.map(q=>q.toArray()),bodyRotations,bodyPositions,headQuaternion:[...bodyRotations[0]],headOmega:[w.x,w.y,w.z],com:m.com.toArray(),kineticEnergy:m.kineticEnergy,anchorError:m.anchorError,largestJoint,kickdt:dt,dragLinearImpulses:lastResult?.dragLinearImpulses.map(v=>v.toArray())??mass.map(()=>[0,0,0]),motorAngularImpulses:lastResult?.motorAngularImpulses.map(v=>v.toArray())??mass.map(()=>[0,0,0]),work:{motor:workMotor,fluid:workFluid,intrinsic:workIntrinsic,water:workWater,backwardEuler:lossBE,rapierWorldStep:worldStepEnergy}});}
 save(0,0);
 for(let tick=0;tick<3*rate;tick++){
  const t=tick*dt,A=2.4*ease(t/.35)*(1-ease((t-.60)/.50)),B=sourceFast?[...observed.sample(t).b]:[A,A,A];chain.setTargets({phase:0,drive:0,stroke:0,muscle1:0,muscle2:0,muscle3:0,turnAnterior:B[0],turnTrunk:B[1],turnPosterior:B[2]});let afterKick=0;
  chain.step(1,()=>{assert.equal(chain.mode,'off','old SDK motors remain off throughout shared kick');const before=chain.measure().kineticEnergy;rapierWaterStates(chain,states);const begun=performance.now(),r=operator.step(states,dt,t);kickMs+=performance.now()-begun;lastResult=r;for(let i=0;i<29;i++){chain.bodies[i].applyImpulse(r.linearImpulses[i],true);chain.bodies[i].applyTorqueImpulse(r.angularImpulses[i],true);}afterKick=chain.measure().kineticEnergy;maxKickEnergyError=Math.max(maxKickEnergyError,Math.abs(afterKick-before-r.directEnergyChange));workMotor+=r.motorWork;workFluid+=r.fluidRelativeWork;workIntrinsic+=r.intrinsicWork;workWater+=r.waterWork;lossBE+=r.numericalDissipation;maxWorkIdentity=Math.max(maxWorkIdentity,r.energyIdentityError);maxJV=Math.max(maxJV,r.anchorVelocityResidual);maxGap=Math.max(maxGap,r.anchorPositionGap);maxReactionP=Math.max(maxReactionP,r.constraintLinearImpulseResidual);maxReactionH=Math.max(maxReactionH,r.constraintAngularImpulse.length());maxIterations=Math.max(maxIterations,r.iterations);});
  const m=chain.measure();const worldChange=m.kineticEnergy-afterKick;worldStepEnergy+=worldChange;positiveWorldStepEnergy+=Math.max(0,worldChange);negativeWorldStepEnergy+=Math.min(0,worldChange);maxWorldStepInjection=Math.max(maxWorldStepInjection,worldChange);if(range016){const Q=chain.bodies.map(body=>{const a=body.rotation();return new Quaternion(a.x,a.y,a.z,a.w).normalize();});for(const link of chain.links){const r=Q[link.parent].clone().invert().multiply(Q[link.child]);for(const c of [r.x,r.y,r.z]){const a=Math.abs(Math.atan2(Math.sin(2*Math.atan2(c,r.w)),Math.cos(2*Math.atan2(c,r.w))));maxRangeCoordinate=Math.max(maxRangeCoordinate,a);maxRangeOvershoot=Math.max(maxRangeOvershoot,a-.16);}}}maxJoint=Math.max(maxJoint,m.maxJointAngle);maxAnchor=Math.max(maxAnchor,m.anchorError);const pa=chain.bodies[0].rotation(),pb=chain.bodies.at(-1).rotation(),H=new Quaternion(pa.x,pa.y,pa.z,pa.w).normalize(),T=new Quaternion(pb.x,pb.y,pb.z,pb.w).normalize(),relative=angle(H.clone().invert().multiply(T)),now=chain.age;if(relative>peakRelative){peakRelative=relative;peakTime=now;}if(now>=.35-1e-10&&now<=.60+1e-10)maxLoaded=Math.max(maxLoaded,relative);
  for(const time of phases)if(phaseData[time]===undefined&&now>=time-1e-10){const alpha=Math.min(1,Math.max(0,(time-previousTime)/dt)),h=previousHead.clone().slerp(H,alpha),q=previousTail.clone().slerp(T,alpha),forward=new Vector3(1,0,0).applyQuaternion(h);phaseData[time]={relativeDeg:angle(h.clone().invert().multiply(q))*deg,headYawDeg:Math.atan2(-forward.z,forward.x)*deg};}previousHead.copy(H);previousTail.copy(T);previousTime=now;if((tick+1)%Math.round(rate/120)===0)save(now,B);
 } const final=chain.measure(),report={variant:'shared-muscle-water',physicalScale:scaleMetadata,initialState:initialStateMetadata,cue:cueMetadata,capacity:capacityMetadata,jointLimits:range016?{x:.16,y:.16,z:.16}:null,program:sourceFast?'observed bluegill fast regional shape only':'uniform finite2.4C',sourceCue:observed?.metadata,rate,sdk:'OFF',controller:capacityHalf?'active parent-Y .50 section capacity; Y K,D,cap scaled together; X/Z baseline':'active parent-frame per-axis Huber velocity potential; same360/16 K,D,cap',phaseData,peakRelativeDeg:peakRelative*deg,peakTime,maxLoadedDeg:maxLoaded*deg,maxJointDeg:maxJoint*deg,maxAnchor,maxGap,maxJV,maxRangeCoordinate,maxRangeOvershoot,positiveWorldStepEnergy,negativeWorldStepEnergy,maxWorldStepInjection,maxReactionP,maxReactionH,maxIterations,maxKickEnergyError,maxWorkIdentity,displacement:final.com.clone().sub(initial.com).toArray(),finalKineticEnergy:final.kineticEnergy,work:{motor:workMotor,fluid:workFluid,intrinsic:workIntrinsic,water:workWater,backwardEuler:lossBE,rapierWorldStep:worldStepEnergy},totalEnergyLedgerError:Math.abs(final.kineticEnergy-initial.kineticEnergy-(workMotor+workFluid-workIntrinsic+workWater+lossBE+worldStepEnergy)),kickMsPerStep:kickMs/(3*rate),totalMs:performance.now()-start};assert.ok(maxWorkIdentity<1e-7&&maxJV<1e-8&&maxKickEnergyError<1e-6,'actual shared kick maintains constraints and signed work accounting');
 const run={wet:true,rate,physicalScale:scaleMetadata,initialState:initialStateMetadata,cue:cueMetadata,capacity:capacityMetadata,jointLimits:range016?{x:.16,y:.16,z:.16}:null,range:range016?.16:undefined,variant:report.variant,controller:report.controller,worst,frames,report};

  return {run,definition:{chain:'public aquarium-articulated.js',frame:'public aquarium-axial-frame.js',
    water:'shared active Huber muscle +BE water+frozen anchor constraints; SDK off',frameRate:120,
    settings:{naturalFrequency:360,dampingRatio:16,motorImplementation:'sdk',sdk:'OFF',capAngle:.15,iterations:16,internalIterations:2,jointLimits:{x:.16,y:.16,z:.16}}}};
  }finally{chain.free();}
}

export async function generate(options={}){
  const {caseName='all',rate=960,out=resolve(root,'.aquarium-build/water-study-reproduced')}=options;
  if(!['all',...caseNames].includes(caseName)||!rates.includes(rate))throw new Error('Unsupported case or physical rate');
  const destination=resolve(out);await mkdir(destination,{recursive:true});
  const context=await loadContext(),results=[];
  for(const selected of caseName==='all'?caseNames:[caseName]){
    const result=selected==='hinge'?await hingeRun(context,rate):await sharedRun(context,selected,rate);
    const timelineText=JSON.stringify({definition:result.definition,runs:[result.run]})+'\n';
    const timelineName=`${selected}-${rate}-timeline.json`,provenanceName=`${selected}-${rate}-provenance.json`;
    const provenance={schemaVersion:1,case:selected,rate,durationSeconds:3,frameRate:120,timelineFile:timelineName,timelineSHA256:hash(timelineText),
      assetSHA256:hash(context.assetBytes),massSHA256:hash(context.massBytes),inputsSHA256:hash(context.inputBytes),
      inputExtractSHA256:context.inputs.provenance.extractSHA256,sourceInputs:context.inputs.provenance.sourceFiles,
      originalCase:context.inputs.provenance.originalCases[selected],moduleSHA256:context.moduleSHA256,
      units:{positions:'m in native render coordinates',physical13cmSI:'m',rotation:'quaternion xyzw',time:'s'},
      settings:result.definition.settings,numeric:{rate,durationSeconds:3,physicalScale:result.run.physicalScale??null,initialState:result.run.initialState??{velocityMetersPerSecond:[0,0,0]},
        cue:result.run.cue??result.run.request,capacity:result.run.capacity??null,jointLimits:result.run.jointLimits??null},
      scope:'Experimental replay of authored mechanics and resistive water. No measured koi physiology, species retarget calibration, solved fluid field or dynamic similarity is established.'};
    await writeFile(resolve(destination,timelineName),timelineText,'utf8');
    await writeFile(resolve(destination,provenanceName),JSON.stringify(provenance,null,2)+'\n','utf8');
    const summary={case:selected,rate,frames:result.run.frames.length,timeline:relative(root,resolve(destination,timelineName)),provenance:relative(root,resolve(destination,provenanceName)),timelineSHA256:provenance.timelineSHA256};
    results.push(summary);console.log(JSON.stringify(summary));
  }
  return results;
}

if(process.argv[1]&&pathToFileURL(resolve(process.argv[1])).href===import.meta.url){
  const options=parseOptions(process.argv.slice(2));
  if(options.help)console.log('Usage: node scripts/aquarium/water-study/generate.mjs [--case=hinge|bounded|source13cm|all] [--rate=240|480|960] [--out=DIR]\nDefault: all cases at960 Hz; output in ignored .aquarium-build/water-study-reproduced.');
  else await generate(options);
}
