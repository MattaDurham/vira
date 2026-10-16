import assert from 'node:assert/strict';
import {Quaternion,Vector3} from '../static/vendor/three.webgpu.js';
import {createRapierChain} from '../static/aquarium-articulated.js';

const quaternion=value=>new Quaternion(value.x,value.y,value.z,value.w).normalize();
const short=value=>{if(value.w<0)value.set(-value.x,-value.y,-value.z,-value.w);return value;};
const twistCoordinates=value=>[value.x,value.y,value.z].map(x=>2*Math.atan2(x,value.w));

export async function testArticulatedLimits(sampler,profile){
 const settings={dt:1/960,naturalFrequency:360,dampingRatio:16,motorImplementation:'sdk'};
 const free=await createRapierChain(sampler,profile,settings),requested={x:.12,y:.16,z:.20};
 const limited=await createRapierChain(sampler,profile,{...settings,jointLimits:requested});
 let RAPIER;
 try{
  RAPIER=limited.RAPIER;
  const axes=[RAPIER.JointAxis.AngX,RAPIER.JointAxis.AngY,RAPIER.JointAxis.AngZ];
  assert.equal(free.jointLimits,null);
  assert.deepEqual(limited.jointLimits,requested);
  requested.x=.8;
  assert.equal(limited.jointLimits.x,.12,'caller changes cannot alter captured mechanical ranges');
  for(const link of limited.links)for(const [i,axis]of axes.entries()){
   const raw=limited.world.impulseJoints.raw,expected=[.12,.16,.20][i];
   assert.ok(raw.jointLimitsEnabled(link.joint.handle,axis),'every actual body joint receives each angular range');
   assert.ok(Math.abs(raw.jointLimitsMin(link.joint.handle,axis)+expected)<1e-7);
   assert.ok(Math.abs(raw.jointLimitsMax(link.joint.handle,axis)-expected)<1e-7);
  }
  for(const link of free.links)for(const axis of axes)assert.equal(free.world.impulseJoints.raw.jointLimitsEnabled(link.joint.handle,axis),false,'omitting limits preserves the unbounded joint API');
  // Body frames are deformation frames, so their neutral world orientations
  // coincide even though the source bone rest arch has varying orientations.
  for(const link of limited.links){
   const A=quaternion(limited.bodies[link.parent].rotation()).multiply(quaternion(link.joint.frameX1()));
   const B=quaternion(limited.bodies[link.child].rotation()).multiply(quaternion(link.joint.frameX2()));
   assert.ok(A.angleTo(B)<1e-6,'mechanical range origin must equal the actual neutral pose');
  }
  const fish={phase:1.2,drive:.3,stroke:.8,muscle1:.7,muscle2:.6,muscle3:.5,turnAnterior:2.4,turnTrunk:-2.4,turnPosterior:2.4,torsionDrive:.7,verticalGain:8};
  limited.setTargets(fish);let compound=0,coordinateDifference=0;
  for(const link of limited.links){
   const target=short(link.target.clone()),vector=link.targetAngles.map(x=>Math.sin(x/2));
   const rebuilt=new Quaternion(...vector,Math.sqrt(Math.max(0,1-vector.reduce((s,x)=>s+x*x,0))));
   assert.ok(rebuilt.angleTo(target)<1e-7,'SDK motor coordinates must reconstruct the actual compound quaternion');
   const twist=twistCoordinates(target);
   coordinateDifference=Math.max(coordinateDifference,...twist.map((x,i)=>Math.abs(x-link.targetAngles[i])));
   if([target.x,target.y,target.z].filter(x=>Math.abs(x)>1e-4).length===3)compound++;
  }
  assert.ok(compound>5&&coordinateDifference>1e-5,'compound target fixture must distinguish motor coordinates from limit coordinates');
  // Exercise the complete public constructor-to-physics path on the actual
  // mass/anchor chain. A small authored test impulse, with all motors off,
  // would fold the unloaded tail joint without its optional solver range.
  const impulsePeaks=[];
  for(const chain of [free,limited]){
   chain.configure('off');chain.bodies[27].applyTorqueImpulse({x:0,y:.00003,z:0},true);let maximum=0;
   for(let tick=0;tick<960;tick++){
    chain.step();
    for(const link of chain.links){
     const relative=short(quaternion(chain.bodies[link.parent].rotation()).invert().multiply(quaternion(chain.bodies[link.child].rotation())).normalize());
     maximum=Math.max(maximum,Math.abs(twistCoordinates(relative)[1]));
    }
   }
   impulsePeaks.push(maximum);
  }
  assert.ok(impulsePeaks[0]>.7&&impulsePeaks[1]<.1601,'optional ranges constrain the actual articulated tail under an internal impulse');
 }finally{free.free();limited.free();}
 for(const bad of [null,{}, {x:0,y:.1,z:.1},{x:-.1,y:.1,z:.1},{x:NaN,y:.1,z:.1},{x:Infinity,y:.1,z:.1},{x:Math.PI,y:.1,z:.1},{x:Number.MIN_VALUE,y:.1,z:.1}]){
  await assert.rejects(createRapierChain(sampler,profile,{jointLimits:bad}),/Joint limits require/);
 }

 // Force an impossible rotation using the real SDK solver. Fixed A /dynamic B
 // isolates angular limits from fish actuator calibration. Arbitrarily rotated
 // body and local frames ensure we measure joint coordinates, not world Euler
 // angles. An unconstrained control must rotate, so a mistaken rotation lock
 // cannot pass. Both signs, every axis, compound motion and step refinement run.
 const rows=[],zero={x:0,y:0,z:0},identity={x:0,y:0,z:0,w:1};
 const axes=[RAPIER.JointAxis.AngX,RAPIER.JointAxis.AngY,RAPIER.JointAxis.AngZ];
 const restRotation=new Quaternion().setFromAxisAngle(new Vector3(1,2,-1).normalize(),.45);
 const localRotation=new Quaternion().setFromAxisAngle(new Vector3(-2,1,1).normalize(),.3);
 for(const rate of [240,480])for(const sign of [-1,1])for(const chosen of [0,1,2,'compound'])for(const enabled of [false,true]){
  const world=new RAPIER.World(zero);world.timestep=1/rate;world.integrationParameters.numSolverIterations=16;world.integrationParameters.numInternalPgsIterations=2;
  try{
   const A=world.createRigidBody(RAPIER.RigidBodyDesc.fixed().setRotation(restRotation));
   const B=world.createRigidBody(RAPIER.RigidBodyDesc.dynamic().setRotation(restRotation).setCanSleep(false).setAdditionalMassProperties(1,zero,{x:.01,y:.01,z:.01},identity));B.recomputeMassPropertiesFromColliders();
   const generic=world.createImpulseJoint(RAPIER.JointData.spherical(zero,zero),A,B,true);
   const joint=typeof generic.configureMotor==='function'?generic:new RAPIER.SphericalImpulseJoint(world.impulseJoints.raw,world.bodies,generic.handle);
   joint.setLocalFrame1(zero,localRotation);joint.setLocalFrame2(zero,localRotation);
   for(const [i,axis]of axes.entries()){
    if(enabled)world.impulseJoints.raw.jointSetLimits(joint.handle,axis,-.12,.12);
    joint.configureMotorModel(axis,RAPIER.MotorModel.ForceBased);
    joint.configureMotor(axis,chosen==='compound'||i===chosen?sign*.8:0,0,.25,.02);joint.setMotorMaxForce(axis,1);
   }
   let maximum=0,translation=0;
   for(let tick=0;tick<rate;tick++){
    world.step();
    const a=quaternion(A.rotation()).multiply(localRotation),b=quaternion(B.rotation()).multiply(localRotation);
    const relative=short(a.invert().multiply(b).normalize());maximum=Math.max(maximum,...twistCoordinates(relative).map(Math.abs));
    const p=B.translation();translation=Math.max(translation,Math.hypot(p.x,p.y,p.z));
   }
   assert.ok(enabled?maximum<.1202:maximum>.7,'actual SDK impulses must enforce optional ranges while unconstrained controls rotate');
   assert.ok(translation<1e-6,'spherical anchors retain locked translation throughout rotation');
   rows.push({rate,sign,chosen,enabled,maximum});
  }finally{world.free();}
 }
 const bounded=rows.filter(row=>row.enabled);
 assert.ok(Math.max(...bounded.map(row=>row.maximum))-Math.min(...bounded.map(row=>row.maximum))<.0003,'the isolated angular limit remains close under step refinement');
 console.log(JSON.stringify({angularRanges:{cases:rows.length,maximumTwist:Math.max(...bounded.map(row=>row.maximum))},checks:'Optional ranges, actual neutral frames, compound motor-coordinate distinction, and mirrored SDK constraints passed.'}));
}
