/* Experimental numerical experiment: one world angular momentum and quaternion head
 * frame, with prescribed body-only shape inertia/momentum. No fluid/added mass. */
import {Vector3,Quaternion,Euler} from '../../../static/vendor/three.webgpu.js';
export const multiplyTensor=(I,v,out=new Vector3())=>out.set(I[0]*v.x+I[1]*v.y+I[2]*v.z,I[3]*v.x+I[4]*v.y+I[5]*v.z,I[6]*v.x+I[7]*v.y+I[8]*v.z);
export function solveTensor(A,b,out=new Vector3()){
  const a=A[0],c=A[1],d=A[2],e=A[3],f=A[4],g=A[5],h=A[6],i=A[7],j=A[8];
  const C00=f*j-g*i,C01=d*i-c*j,C02=c*g-d*f,C10=g*h-e*j,C11=a*j-d*h,C12=d*e-a*g,C20=e*i-f*h,C21=c*h-a*i,C22=a*f-c*e;
  const determinant=a*C00+c*C10+d*C20;
  if(!Number.isFinite(determinant)||Math.abs(determinant)<1e-18)throw new Error('Singular angular tensor/Jacobian');
  return out.set((C00*b.x+C01*b.y+C02*b.z)/determinant,(C10*b.x+C11*b.y+C12*b.z)/determinant,(C20*b.x+C21*b.y+C22*b.z)/determinant);
}
export const createAngularState=()=>({headOrientation:new Quaternion(),Hworld:new Vector3(),midpointOmegaBody:new Vector3(),midpointOmegaWorld:new Vector3(),diagnostics:{}});
export const headQuaternion=(fish,out=new Quaternion())=>out.setFromEuler(new Euler(fish.bank??0,fish.yaw??0,-(fish.pitch??0),'YZX'));
export function headAngles(q){const e=new Euler().setFromQuaternion(q,'YZX');return {yaw:e.y,pitch:-e.z,bank:e.x};}
export function bodyOmegaFromEulerRates({pitch=0,bank=0},{yawRate=0,pitchRate=0,bankRate=0},out=new Vector3()){
  return out.set(bankRate-Math.sin(pitch)*yawRate,Math.cos(bank)*Math.cos(pitch)*yawRate-Math.sin(bank)*pitchRate,-Math.sin(bank)*Math.cos(pitch)*yawRate-Math.cos(bank)*pitchRate);
}
export function eulerRatesFromBodyOmega({pitch=0,bank=0},omega){
  if(Math.abs(Math.cos(pitch))<1e-8)throw new Error('Euler diagnostic singularity; use the quaternion state');
  const yawRate=(Math.cos(bank)*omega.y-Math.sin(bank)*omega.z)/Math.cos(pitch),pitchRate=-Math.sin(bank)*omega.y-Math.cos(bank)*omega.z;
  return {yawRate,pitchRate,bankRate:omega.x+Math.sin(pitch)*yawRate};
}
export function headLocalIncrement(first,second,out=new Vector3()){
  const q=first.clone().invert().multiply(second).normalize();if(q.w<0)q.set(-q.x,-q.y,-q.z,-q.w);
  const n=Math.hypot(q.x,q.y,q.z),k=n<1e-12?2:2*Math.atan2(n,q.w)/n;return out.set(k*q.x,k*q.y,k*q.z);
}
// Existing scalar efforts are authored body-axis angular-acceleration proxies.
// Their neutral inertia calibration is fixed; this is not a fluid-force model.
export const bodyTorqueFromAcceleration=(referenceInertia,alphaBody,out)=>multiplyTensor(referenceInertia,alphaBody,out);
export function bodyDampingFromRates(referenceInertia,rates){return new Float64Array([referenceInertia[0]*rates.x,0,0,0,referenceInertia[4]*rates.y,0,0,0,referenceInertia[8]*rates.z]);}
export function frozenRotationalEnergy(state,inertia){const B=state.Hworld.clone().applyQuaternion(state.headOrientation.clone().invert());return .5*B.dot(solveTensor(inertia,B));}
export function createAngularMomentumIntegrator({tolerance=2e-12,maxIterations=14}={}){
  const q0=new Quaternion(),q1=new Quaternion(),qmid=new Quaternion(),dq=new Quaternion(),half=new Quaternion(),inverse=new Quaternion();
  const H0=new Vector3(),H1=new Vector3(),B0=new Vector3(),B1=new Vector3(),Bmid=new Vector3(),omega=new Vector3(),residual=new Vector3(),trialResidual=new Vector3(),plus=new Vector3(),minus=new Vector3(),trial=new Vector3(),correction=new Vector3();
  const torque=new Vector3(),activeWorld=new Vector3(),activeBody=new Vector3(),dragBody=new Vector3(),dampingPowerBody=new Vector3(),h=new Vector3(),I=new Float64Array(9),J=new Float64Array(9),zero=new Vector3();
  let currentShape,lastTrial,activePower=0,dampingPower=0;
  function step(state,shape,dt,options={},out=createAngularState()){
    if(!(Number.isFinite(dt)&&dt>0))throw new Error('Angular step requires positive physical elapsed time');
    q0.copy(state.headOrientation).normalize();H0.copy(state.Hworld);B0.copy(H0).applyQuaternion(inverse.copy(q0).invert());
    function evaluate(w,result){
      dq.set(w.x*dt/2,w.y*dt/2,w.z*dt/2,1).normalize();q1.copy(q0).multiply(dq).normalize();half.identity().slerp(dq,.5);qmid.copy(q0).multiply(half).normalize();
      lastTrial=options.evaluateTrial?.({headOrientation:q1,midpointOmegaBody:w,dt});currentShape=lastTrial?.shape??shape;
      if(!currentShape?.inertia||!currentShape.h)throw new Error('A full shape interval is required');
      I.set(currentShape.inertia);h.copy(currentShape.h);
      const torqueWorld=lastTrial?.torqueWorld??options.torqueWorld??zero,torqueBody=lastTrial?.torqueBody??options.torqueBody??zero,D=lastTrial?.dampingBody??options.dampingBody;
      activeBody.copy(torqueBody);activeWorld.copy(torqueWorld).add(dampingPowerBody.copy(activeBody).applyQuaternion(qmid));
      if(D)multiplyTensor(D,w,dragBody).negate();else dragBody.set(0,0,0);
      dampingPower=dragBody.dot(w);activePower=activeBody.dot(w)+torqueWorld.dot(dampingPowerBody.copy(w).applyQuaternion(qmid));
      torque.copy(activeWorld).add(dampingPowerBody.copy(dragBody).applyQuaternion(qmid));H1.copy(H0).addScaledVector(torque,dt);
      B1.copy(H1).applyQuaternion(inverse.copy(q1).invert());Bmid.copy(B0).add(B1).multiplyScalar(.5);
      multiplyTensor(I,w,result).add(h).sub(Bmid);
      if(![result.x,result.y,result.z,H1.x,H1.y,H1.z].every(Number.isFinite))throw new Error('Nonfinite angular trial');
      return result.length();
    }
    // Predictor uses the supplied interval. Pure trial callbacks can then rebuild
    // shape from the one frozen behavior packet at each Newton evaluation.
    solveTensor(shape.inertia,trial.copy(B0).sub(shape.h),omega);
    let norm=evaluate(omega,residual),iterations=0;const target=tolerance*(1+B0.length()+h.length());
    for(;norm>target&&iterations<maxIterations;iterations++){
      for(let axis=0;axis<3;axis++){
        const epsilon=1e-5*(1+Math.abs(omega.getComponent(axis)));trial.copy(omega).setComponent(axis,omega.getComponent(axis)+epsilon);evaluate(trial,plus);
        trial.copy(omega).setComponent(axis,omega.getComponent(axis)-epsilon);evaluate(trial,minus);
        for(let row=0;row<3;row++)J[row*3+axis]=(plus.getComponent(row)-minus.getComponent(row))/(2*epsilon);
      }
      solveTensor(J,residual,correction);let accepted=false;
      for(let scale=1;scale>=1/256;scale*=.5){trial.copy(omega).addScaledVector(correction,-scale);const candidate=evaluate(trial,trialResidual);if(candidate<norm){omega.copy(trial);residual.copy(trialResidual);norm=candidate;accepted=true;break;}}
      if(!accepted)break;
    }
    norm=evaluate(omega,residual);
    if(norm>target)throw new Error(`Angular solve did not converge: residual ${norm}, tolerance ${target}`);
    out.headOrientation.copy(q1);out.Hworld.copy(H1);out.midpointOmegaBody.copy(omega);out.midpointOmegaWorld.copy(omega).applyQuaternion(qmid);
    out.diagnostics={iterations,residualNorm:norm,angularImpulseResidual:out.Hworld.clone().sub(H0).addScaledVector(torque,-dt).length(),activePower,dampingPower,torqueWorld:torque.clone(),midpointOrientation:qmid.clone()};
    return out;
  }
  return {step};
}
