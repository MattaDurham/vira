/* Actual-asset articulated mechanics experiment. No kinematic body teleports occur
 * during step. Refined closed-envelope neutral masses are still a proxy. */
import RAPIER from './vendor/rapier.mjs';
import {Vector3,Quaternion,Matrix4} from './vendor/three.webgpu.js';
let ready;export const initRapier=()=>ready??=RAPIER.init();
const v=x=>new Vector3(x.x??x[0],x.y??x[1],x.z??x[2]);
const q=x=>new Quaternion(x.x,x.y,x.z,x.w).normalize();
const identity={x:0,y:0,z:0,w:1},axes=[RAPIER.JointAxis.AngX,RAPIER.JointAxis.AngY,RAPIER.JointAxis.AngZ];
const multiply=(I,x)=>new Vector3(I[0]*x.x+I[1]*x.y+I[2]*x.z,I[3]*x.x+I[4]*x.y+I[5]*x.z,I[6]*x.x+I[7]*x.y+I[8]*x.z);
function tensor(diagonal,rotation){
 const I=new Float64Array(9);
 for(let axis=0;axis<3;axis++){const a=new Vector3(axis===0?1:0,axis===1?1:0,axis===2?1:0).applyQuaternion(rotation),s=[a.x,a.y,a.z];for(let i=0;i<3;i++)for(let j=0;j<3;j++)I[3*i+j]+=diagonal[axis]*s[i]*s[j];}
 return I;
}
export function principalInertia(input){
 const A=Float64Array.from(input),V=new Float64Array([1,0,0,0,1,0,0,0,1]);
 for(let iteration=0;iteration<40;iteration++){
  let p=0,r=1;for(const [a,b] of [[0,2],[1,2]])if(Math.abs(A[3*a+b])>Math.abs(A[3*p+r])){p=a;r=b;}
  const off=A[3*p+r];if(Math.abs(off)<1e-15)break;
  const tau=(A[3*r+r]-A[3*p+p])/(2*off),t=(tau>=0?1:-1)/(Math.abs(tau)+Math.sqrt(1+tau*tau)),c=1/Math.sqrt(1+t*t),s=t*c;
  const pp=A[3*p+p],rr=A[3*r+r];A[3*p+p]=pp-t*off;A[3*r+r]=rr+t*off;A[3*p+r]=A[3*r+p]=0;
  for(let k=0;k<3;k++)if(k!==p&&k!==r){const a=A[3*k+p],b=A[3*k+r];A[3*k+p]=A[3*p+k]=c*a-s*b;A[3*k+r]=A[3*r+k]=s*a+c*b;}
  for(let k=0;k<3;k++){const a=V[3*k+p],b=V[3*k+r];V[3*k+p]=c*a-s*b;V[3*k+r]=s*a+c*b;}
 }
 const X=new Vector3(V[0],V[3],V[6]),Y=new Vector3(V[1],V[4],V[7]),Z=new Vector3(V[2],V[5],V[8]);if(X.clone().cross(Y).dot(Z)<0)Z.negate();
 const rotation=new Quaternion().setFromRotationMatrix(new Matrix4().makeBasis(X,Y,Z)).normalize(),diagonal=[A[0],A[4],A[8]];
 const reconstructed=tensor(diagonal,rotation),error=Math.max(...input.map((x,i)=>Math.abs(x-reconstructed[i])));
 if(diagonal.some(x=>!(x>0))||error>1e-9)throw new Error(`Invalid principal inertia decomposition ${diagonal}, error ${error}`);
 return {diagonal,rotation,error};
}
function logQuaternion(value){const a=value.clone().normalize();if(a.w<0)a.set(-a.x,-a.y,-a.z,-a.w);const n=Math.hypot(a.x,a.y,a.z),k=n<1e-12?2:2*Math.atan2(n,a.w)/n;return new Vector3(a.x*k,a.y*k,a.z*k);}
const zeroFish={phase:0,drive:0,stroke:0,muscle1:0,muscle2:0,muscle3:0,turnAnterior:0,turnTrunk:0,turnPosterior:0};
const lagKeys=['yaw','pitch','bank'].flatMap(p=>[1,2,3].map(i=>p+'Lag'+i));
export async function createRapierChain(sampler,profile,{dt=1/240,iterations=16,internalIterations=2,naturalFrequency=15,dampingRatio=1,capAngle=.15,initialFish=zeroFish,motorImplementation='torque',externalForces,jointLimits}={}){
 if(!['torque','sdk'].includes(motorImplementation))throw new Error('Unknown joint motor implementation');
 if(![dt,naturalFrequency,dampingRatio,capAngle].every(Number.isFinite)||dt<=0||naturalFrequency<=0||dampingRatio<0||capAngle<=0)throw new Error('Invalid articulated body parameters');
 const ranges=jointLimits===undefined?null:[jointLimits?.x,jointLimits?.y,jointLimits?.z];
 if(ranges?.some(value=>!Number.isFinite(value)||!(Math.fround(value)>0)||Math.fround(value)>=Math.PI))throw new Error('Joint limits require positive finite x/y/z half-ranges below pi radians');
 await initRapier();
 const world=new RAPIER.World({x:0,y:0,z:0});world.timestep=dt;world.integrationParameters.numSolverIterations=iterations;world.integrationParameters.numInternalPgsIterations=internalIterations;
 if(ranges&&['jointSetLimits','jointLimitsEnabled','jointLimitsMin','jointLimitsMax'].some(name=>typeof world.impulseJoints.raw[name]!=='function')){world.free();throw new Error('Rapier angular joint limits are unavailable');}
 const source=profile.bodies29;if(source?.length!==29)throw new Error('Expected29 refined mass bodies (cranial0..4, then5..32)');
 const bodies=[],records=[],links=[],desired=sampler.createFrame();sampler.capture({...zeroFish,...initialFish},desired);
 const deformation=panel=>desired.rotations[panel].clone().multiply(sampler.rest[panel].clone().invert()).normalize();
 let decompositionError=0;
 for(let index=0;index<29;index++){
  const panel=index===0?0:index+4,region=source[index],principal=principalInertia(region.inertia);decompositionError=Math.max(decompositionError,principal.error);
  const R=deformation(panel),C=v(region.com).sub(sampler.centers[panel]).applyQuaternion(R).add(desired.positions[panel]);
  const desc=RAPIER.RigidBodyDesc.dynamic().setTranslation(C.x,C.y,C.z).setRotation(R).setCanSleep(false).setLinearDamping(0).setAngularDamping(0)
   .setAdditionalMassProperties(region.mass,{x:0,y:0,z:0},v(principal.diagonal),principal.rotation);
  const body=world.createRigidBody(desc);body.recomputeMassPropertiesFromColliders();bodies.push(body);records.push({panel,region,principal,restCenter:v(region.com)});
 }
 for(let child=1;child<29;child++){
  const parent=child-1,anchor=sampler.centers[records[child].panel],a=anchor.clone().sub(records[parent].restCenter),b=anchor.clone().sub(records[child].restCenter);
  const generic=world.createImpulseJoint(RAPIER.JointData.spherical(a,b),bodies[parent],bodies[child],true);
  // 0.21 returns GenericImpulseJoint for a raw spherical descriptor. The
  // public typed wrapper exposes its documented angular motor methods.
  const joint=typeof generic.configureMotor==='function'?generic:new RAPIER.SphericalImpulseJoint(world.impulseJoints.raw,world.bodies,generic.handle);
  joint.setLocalFrame1(a,identity);joint.setLocalFrame2(b,identity);joint.setContactsEnabled(false);
  if(ranges){
   // Optional authored mechanical ranges, not measured koi calibration. In
   // Rapier0.21 each symmetric angular limit bounds wrap(2*atan2(q_axis,q_w))
   // in the two local joint frames. Three limits form a box, not a cone;
   // finite-step solver error can temporarily exceed them under strong load.
   // The exposed raw set supplies the setter missing from the spherical wrapper.
   // https://github.com/dimforge/rapier/blob/b716d375efc0201003f0cd9ef7168eee0b62c177/src/dynamics/solver/joint_constraint/joint_constraint_helper.rs#L422-L458
   try{
    const raw=world.impulseJoints.raw;
    axes.forEach((axis,i)=>{
     raw.jointSetLimits(joint.handle,axis,-ranges[i],ranges[i]);
     if(!raw.jointLimitsEnabled(joint.handle,axis)||Math.abs(raw.jointLimitsMin(joint.handle,axis)+ranges[i])>1e-6||Math.abs(raw.jointLimitsMax(joint.handle,axis)-ranges[i])>1e-6)throw new Error('Rapier did not retain the requested angular joint limits');
    });
   }catch(error){world.free();throw error;}
  }
  const inertiaAt=(record,offset)=>[0,1,2].map(axis=>record.region.inertia[axis*3+axis]+record.region.mass*(offset.lengthSq()-offset.getComponent(axis)**2));
  const P=inertiaAt(records[parent],a),C=inertiaAt(records[child],b),effective=P.map((x,i)=>1/(1/x+1/C[i]));
  const reference=effective[1],stiffness=reference*naturalFrequency**2,damping=2*dampingRatio*Math.sqrt(stiffness*Math.min(...effective)),cap=stiffness*capAngle;
  axes.forEach(axis=>{joint.configureMotorModel(axis,RAPIER.MotorModel.ForceBased);joint.configureMotor(axis,0,0,0,0);joint.setMotorMaxForce(axis,0);});
  links.push({joint,parent,child,anchor,a,b,effective,stiffness,damping,cap,target:new Quaternion(),targetAngles:[0,0,0]});
 }
 let mode='off',age=0,maximumAppliedTorque=0;
 function setTargets(fish=zeroFish){
  const commands={...zeroFish,...fish,poseHistory:true,yawRate:0,pitchRate:0,bankRate:0,...Object.fromEntries(lagKeys.map(k=>[k,0]))};sampler.capture(commands,desired);
  for(const link of links){
   const A=deformation(records[link.parent].panel),B=deformation(records[link.child].panel);link.target.copy(A.invert()).multiply(B).normalize();if(link.target.w<0)link.target.set(-link.target.x,-link.target.y,-link.target.z,-link.target.w);
   // SDK motor coordinates differ from the limit coordinates above. Keep
   // 2*asin(q_axis), including compound rotations, as used by the pinned solver:
   // https://github.com/dimforge/rapier/blob/b716d375efc0201003f0cd9ef7168eee0b62c177/src/dynamics/solver/joint_constraint/joint_constraint_helper.rs#L542-L550
   link.targetAngles=[link.target.x,link.target.y,link.target.z].map(x=>2*Math.asin(Math.max(-1,Math.min(1,x))));
  }
 }
 function configure(nextMode='off'){
  mode=nextMode;if(!['off','damping','passive','restoring','active'].includes(mode))throw new Error('Unknown articulated motor mode');
  for(const link of links)for(let i=0;i<3;i++){
   const enabled=motorImplementation==='sdk'&&mode!=='off',K=mode==='damping'?0:link.stiffness,target=mode==='active'?link.targetAngles[i]:0;
   link.joint.configureMotor(axes[i],target,0,enabled?K:0,enabled?link.damping:0);link.joint.setMotorMaxForce(axes[i],enabled?link.cap:0);
  }
 }
 function jointState(link){
  const A=q(bodies[link.parent].rotation()),B=q(bodies[link.child].rotation()),relative=A.clone().invert().multiply(B),target=mode==='active'?link.target:new Quaternion();
  const relativeAngle=logQuaternion(relative).length(),error=logQuaternion(relative.multiply(target.clone().invert())),omega=v(bodies[link.child].angvel()).sub(v(bodies[link.parent].angvel()));
  return {A,error,omega,angle:error.length(),relativeAngle};
 }
 function potential(){
  if(mode==='off'||mode==='damping')return 0;
  return links.reduce((sum,link)=>{const x=jointState(link).angle,threshold=link.cap/link.stiffness;return sum+(x<=threshold?.5*link.stiffness*x*x:link.cap*x-.5*link.cap*threshold);},0);
 }
 function applyInternalTorques(){
  for(const body of bodies){body.resetForces(true);body.resetTorques(true);}
  if(mode==='off'||motorImplementation!=='torque')return;
  for(const link of links){
   const state=jointState(link),spring=state.error.clone().multiplyScalar(mode==='damping'?0:-link.stiffness).applyQuaternion(state.A),drag=state.omega.clone().multiplyScalar(-link.damping);
   if(spring.length()>link.cap)spring.setLength(link.cap);if(drag.length()>link.cap)drag.setLength(link.cap);
   const torque=spring.add(drag);maximumAppliedTorque=Math.max(maximumAppliedTorque,torque.length());bodies[link.child].addTorque(torque,true);bodies[link.parent].addTorque(torque.clone().negate(),true);
  }
 }
 function step(count=1,external=externalForces){for(let i=0;i<count;i++){if(motorImplementation==='sdk')configure(mode);applyInternalTorques();external?.({world,bodies,records,dt,age});world.step();age+=dt;}}
 function measure(){
  let mass=0,energy=0;const com=new Vector3(),P=new Vector3(),H=new Vector3();
  const states=bodies.map(body=>{const mass=body.mass(),center=v(body.worldCom()),rotation=q(body.rotation()),omega=v(body.angvel()),velocity=v(body.linvel()),diagonal=v(body.principalInertia()).toArray(),principal=rotation.multiply(q(body.principalInertiaLocalFrame())),I=tensor(diagonal,principal);return {mass,center,omega,velocity,I};});
  for(const s of states){mass+=s.mass;com.addScaledVector(s.center,s.mass);P.addScaledVector(s.velocity,s.mass);energy+=.5*s.mass*s.velocity.lengthSq()+.5*s.omega.dot(multiply(s.I,s.omega));}com.multiplyScalar(1/mass);
  for(const s of states)H.add(s.center.clone().sub(com).cross(s.velocity.clone().multiplyScalar(s.mass))).add(multiply(s.I,s.omega));
  let anchorError=0,maxJointAngle=0,maxMotorErrorAngle=0;
  for(const link of links){const a=v(bodies[link.parent].translation()).add(link.a.clone().applyQuaternion(q(bodies[link.parent].rotation()))),b=v(bodies[link.child].translation()).add(link.b.clone().applyQuaternion(q(bodies[link.child].rotation())));anchorError=Math.max(anchorError,a.distanceTo(b));const joint=jointState(link);maxJointAngle=Math.max(maxJointAngle,joint.relativeAngle);maxMotorErrorAngle=Math.max(maxMotorErrorAngle,joint.angle);}
  // SDK independent-axis motors are active servos. Their compound force
  // field does not have this geodesic scalar as an exact potential.
  const springEstimate=potential(),exactPotential=motorImplementation==='torque'||mode==='off'||mode==='damping'?springEstimate:null;
  return {age,mass,com,P,H,kineticEnergy:energy,potentialEnergy:exactPotential,
   energy:exactPotential===null?null:energy+exactPotential,energyDiagnostic:energy+springEstimate,
   springEstimate,potentialModel:exactPotential===null?'geodesic diagnostic; active SDK motors':'capped geodesic potential',
   anchorError,maxJointAngle,maxMotorErrorAngle,maximumAppliedTorque:motorImplementation==='torque'?maximumAppliedTorque:null};
 }
 function setWholeVelocity(velocity=new Vector3(),omega=new Vector3()){
  const center=measure().com;for(const body of bodies){const r=v(body.worldCom()).sub(center);body.setLinvel(v(velocity).add(v(omega).cross(r)),true);body.setAngvel(omega,true);}
 }
 setTargets(zeroFish);configure('off');
 function boneTransforms(out={positions:sampler.centers.map(()=>new Vector3()),rotations:sampler.rest.map(()=>new Quaternion())}){
  for(let i=0;i<34;i++){const index=i<=4?0:Math.min(28,i-4),R=q(bodies[index].rotation());out.positions[i].copy(sampler.centers[i]).sub(records[index].restCenter).applyQuaternion(R).add(v(bodies[index].translation()));out.rotations[i].copy(R).multiply(sampler.rest[i]);}return out;
 }
 return {world,bodies,links,records,sampler,profile,dt,setTargets,boneTransforms,configure,step,measure,setWholeVelocity,decompositionError,
  jointLimits:ranges?Object.freeze({x:ranges[0],y:ranges[1],z:ranges[2]}):null,
  get mode(){return mode;},get age(){return age;},free:()=>world.free(),RAPIER};
}
