/* Experimental simultaneous 6D frozen-body drag kick. No patch ordering, root servo,
 * velocity/momentum normalization, added water mass or hidden kinetic clamp. */
import {Vector3,Quaternion} from '../../../static/vendor/three.webgpu.js';
const copyV=(out,v)=>out.set(v.x??v[0],v.y??v[1],v.z??v[2]);
const copyQ=(out,q)=>out.set(q.x??q[0],q.y??q[1],q.z??q[2],q.w??q[3]).normalize();
function multiply(A,x,out){for(let i=0;i<6;i++){let sum=0;for(let j=0;j<6;j++)sum+=A[i*6+j]*x[j];out[i]=sum;}return out;}
function factorSPD(A,L){L.fill(0);for(let i=0;i<6;i++)for(let j=0;j<=i;j++){let sum=A[i*6+j];for(let k=0;k<j;k++)sum-=L[i*6+k]*L[j*6+k];if(i===j){if(!(sum>0&&Number.isFinite(sum)))throw new Error('Water 6D Hessian must remain SPD');L[i*6+j]=Math.sqrt(sum);}else L[i*6+j]=sum/L[j*6+j];}return L;}
function solveFactored(L,b,out,scratch){for(let i=0;i<6;i++){let sum=b[i];for(let j=0;j<i;j++)sum-=L[i*6+j]*scratch[j];scratch[i]=sum/L[i*6+i];}for(let i=5;i>=0;i--){let sum=scratch[i];for(let j=i+1;j<6;j++)sum-=L[j*6+i]*out[j];out[i]=sum/L[i*6+i];}return out;}
const dot=(a,b)=>{let x=0;for(let i=0;i<6;i++)x+=a[i]*b[i];return x;};
export function createBodyWaterImpulse(surface,massProperties,{density=1000,normalCoefficient=1,tangentCoefficient=.02,waterVelocity,method='midpoint',tolerance=1e-12,maxIterations=20}={}){
 if(![density,normalCoefficient,tangentCoefficient].every(Number.isFinite)||density<=0||normalCoefficient<0||tangentCoefficient<0||!['midpoint','backwardEuler'].includes(method))throw new Error('Invalid dissipative-body water parameters');
 const theta=method==='midpoint'?.5:1,count=massProperties.length,bodies=massProperties.map(p=>{const M=new Float64Array(36),L=new Float64Array(36);if(!(p.mass>0))throw new Error('Positive body mass required');for(let i=0;i<3;i++)M[i*6+i]=p.mass;for(let i=0;i<3;i++)for(let j=0;j<3;j++)M[(i+3)*6+j+3]=p.inertia[i*3+j];factorSPD(M,L);return {mass:p.mass,M,L,patches:[]};});
 for(const sample of surface.samples??surface){const r=sample.localPoint,n=sample.localNormal;if(!(sample.area>0)||Math.abs(n.lengthSq()-1)>1e-6)throw new Error('Positive area/unit normal required');const J=new Float64Array([1,0,0,0,r.z,-r.y,0,1,0,-r.z,0,r.x,0,0,1,r.y,-r.x,0]),N=new Float64Array([n.x,n.y,n.z,r.y*n.z-r.z*n.y,r.z*n.x-r.x*n.z,r.x*n.y-r.y*n.x]),P=new Float64Array(36);for(let i=0;i<6;i++)for(let j=0;j<6;j++)P[i*6+j]=J[i]*J[j]+J[6+i]*J[6+j]+J[12+i]*J[12+j]-N[i]*N[j];bodies[sample.bodyIndex].patches.push({...sample,source:sample,localPoint:sample.localPoint.clone(),localNormal:sample.localNormal.clone(),intrinsicVelocity:sample.intrinsicVelocity?.clone()??new Vector3(),J,N,P,scale:.5*density*sample.area,flow:new Float64Array(3)});}
 const createOutput=()=>({linearImpulses:Array.from({length:count},()=>new Vector3()),angularImpulses:Array.from({length:count},()=>new Vector3()),velocities:Array.from({length:count},()=>new Vector3()),omegas:Array.from({length:count},()=>new Vector3()),relativeEnergyChange:0,bodyEnergyChange:0,directEnergyChange:0,waterWork:0,fluidRelativeWork:0,intrinsicWork:0,numericalDissipation:0,maxBodyRelativeEnergyChange:-Infinity,maxPointRelativePower:-Infinity,maxResidual:0,maxIterations:0});
 const outScratch=createOutput(),Q=new Quaternion(),Qinv=new Quaternion(),center=new Vector3(),worldPoint=new Vector3(),flow=new Vector3(),velocity=new Vector3(),omega=new Vector3();
 const old=new Float64Array(6),x=new Float64Array(6),next=new Float64Array(6),delta=new Float64Array(6),g=new Float64Array(6),H=new Float64Array(36),T=new Float64Array(6),residual=new Float64Array(6),scaledResidual=new Float64Array(6),matrix=new Float64Array(36),L=new Float64Array(36),solveScratch=new Float64Array(6),correction=new Float64Array(6),candidate=new Float64Array(6),candidateResidual=new Float64Array(6),candidateG=new Float64Array(6),impulse=new Float64Array(6),velocityIncrement=new Float64Array(6);
 let relativePower=0,waterPower=0,intrinsicPower=0,maxPointPower=-Infinity;
 function evaluate(body,value,gradient,hessian){
  gradient.fill(0);if(hessian)hessian.fill(0);relativePower=waterPower=intrinsicPower=0;maxPointPower=-Infinity;let potential=0;
  for(const patch of body.patches){
   const r=patch.localPoint,n=patch.localNormal,u0=value[0]+value[4]*r.z-value[5]*r.y-patch.flow[0]+patch.intrinsicVelocity.x,u1=value[1]+value[5]*r.x-value[3]*r.z-patch.flow[1]+patch.intrinsicVelocity.y,u2=value[2]+value[3]*r.y-value[4]*r.x-patch.flow[2]+patch.intrinsicVelocity.z,un=u0*n.x+u1*n.y+u2*n.z,t0=u0-un*n.x,t1=u1-un*n.y,t2=u2-un*n.z,tn=Math.hypot(t0,t1,t2),normalScale=normalCoefficient*Math.abs(un)*un,tangentScale=tangentCoefficient*tn,s=patch.scale;
   const a=s*(normalScale*n.x+tangentScale*t0),b=s*(normalScale*n.y+tangentScale*t1),c=s*(normalScale*n.z+tangentScale*t2);
   gradient[0]+=a;gradient[1]+=b;gradient[2]+=c;gradient[3]+=r.y*c-r.z*b;gradient[4]+=r.z*a-r.x*c;gradient[5]+=r.x*b-r.y*a;
   const power=-(a*u0+b*u1+c*u2);relativePower+=power;waterPower-=a*patch.flow[0]+b*patch.flow[1]+c*patch.flow[2];intrinsicPower-=a*patch.intrinsicVelocity.x+b*patch.intrinsicVelocity.y+c*patch.intrinsicVelocity.z;maxPointPower=Math.max(maxPointPower,power);potential+=s/3*(normalCoefficient*Math.abs(un)**3+tangentCoefficient*tn**3);
   if(hessian){
    T[0]=t0;T[1]=t1;T[2]=t2;T[3]=r.y*t2-r.z*t1;T[4]=r.z*t0-r.x*t2;T[5]=r.x*t1-r.y*t0;
    const a=2*s*normalCoefficient*Math.abs(un),b=s*tangentCoefficient*tn,c=tn>1e-14?s*tangentCoefficient/tn:0,N=patch.N,P=patch.P;
    for(let i=0;i<6;i++)for(let j=0;j<6;j++)hessian[i*6+j]+=a*N[i]*N[j]+b*P[i*6+j]+c*T[i]*T[j];
   }
  }
  return potential;
 }
 const inverseNorm=(body,value)=>{solveFactored(body.L,value,scaledResidual,solveScratch);return Math.sqrt(Math.max(0,dot(value,scaledResidual)));};
 function step(states,dt,age=0,out=outScratch){
  if(!(dt>0&&Number.isFinite(dt)))throw new Error('Positive body-drag elapsed time required');out.relativeEnergyChange=out.bodyEnergyChange=out.directEnergyChange=out.waterWork=out.fluidRelativeWork=out.intrinsicWork=out.numericalDissipation=out.maxResidual=out.maxIterations=0;out.maxBodyRelativeEnergyChange=out.maxPointRelativePower=-Infinity;
  for(let index=0;index<count;index++){
   const body=bodies[index],state=states[index];copyQ(Q,state.orientation);Qinv.copy(Q).invert();copyV(center,state.center);copyV(velocity,state.velocity).applyQuaternion(Qinv);copyV(omega,state.omega).applyQuaternion(Qinv);old.set([velocity.x,velocity.y,velocity.z,omega.x,omega.y,omega.z]);x.set(old);
   for(const patch of body.patches){
    if(patch.dynamic){const source=patch.source;if(source.bodyIndex!==index||!(source.area>0)||Math.abs(source.localNormal.lengthSq()-1)>1e-6)throw new Error('Invalid dynamic material patch');patch.localPoint.copy(source.localPoint);patch.localNormal.copy(source.localNormal);patch.intrinsicVelocity.copy(source.intrinsicVelocity??{x:0,y:0,z:0});patch.scale=.5*density*source.area;const r=patch.localPoint,n=patch.localNormal,J=patch.J,N=patch.N,P=patch.P;J.set([1,0,0,0,r.z,-r.y,0,1,0,-r.z,0,r.x,0,0,1,r.y,-r.x,0]);N.set([n.x,n.y,n.z,r.y*n.z-r.z*n.y,r.z*n.x-r.x*n.z,r.x*n.y-r.y*n.x]);for(let i=0;i<6;i++)for(let j=0;j<6;j++)P[i*6+j]=J[i]*J[j]+J[6+i]*J[6+j]+J[12+i]*J[12+j]-N[i]*N[j];}
    patch.flow.fill(0);if(waterVelocity){worldPoint.copy(patch.localPoint).applyQuaternion(Q).add(center);flow.set(0,0,0);const returned=waterVelocity(worldPoint,age,flow);if(returned&&returned!==flow)copyV(flow,returned);flow.applyQuaternion(Qinv);patch.flow.set([flow.x,flow.y,flow.z]);}}
   let norm=0,iterations=0;const target=tolerance*(1+Math.sqrt(Math.max(0,dot(old,multiply(body.M,old,next)))));
   for(;iterations<maxIterations;iterations++){
    evaluate(body,x,g,H);for(let i=0;i<6;i++)delta[i]=x[i]-old[i];multiply(body.M,delta,residual);for(let i=0;i<6;i++)residual[i]+=theta*dt*g[i];norm=inverseNorm(body,residual);if(norm<=target)break;
    for(let i=0;i<36;i++)matrix[i]=body.M[i]+theta*dt*H[i];factorSPD(matrix,L);solveFactored(L,residual,correction,solveScratch);let accepted=false;
    for(let fraction=1;fraction>=1/256;fraction*=.5){for(let i=0;i<6;i++)candidate[i]=x[i]-fraction*correction[i];evaluate(body,candidate,candidateG);for(let i=0;i<6;i++)delta[i]=candidate[i]-old[i];multiply(body.M,delta,candidateResidual);for(let i=0;i<6;i++)candidateResidual[i]+=theta*dt*candidateG[i];if(inverseNorm(body,candidateResidual)<norm){x.set(candidate);accepted=true;break;}}
    if(!accepted)break;
   }
   evaluate(body,x,g);for(let i=0;i<6;i++)delta[i]=x[i]-old[i];multiply(body.M,delta,residual);for(let i=0;i<6;i++)residual[i]+=theta*dt*g[i];norm=inverseNorm(body,residual);if(norm>target*20)throw new Error(`Simultaneous body drag failed ${norm}`);
   for(let i=0;i<6;i++)impulse[i]=-dt*g[i];solveFactored(body.L,impulse,velocityIncrement,solveScratch);for(let i=0;i<6;i++)next[i]=old[i]+velocityIncrement[i];const direct=dot(old,impulse)+.5*dot(velocityIncrement,impulse),flowWork=dt*waterPower,relative=direct-flowWork;
   const stageWork=dt*(relativePower+waterPower-intrinsicPower);out.directEnergyChange+=direct;out.bodyEnergyChange+=stageWork;out.relativeEnergyChange+=relative;out.waterWork+=flowWork;out.fluidRelativeWork+=dt*relativePower;out.intrinsicWork+=dt*intrinsicPower;out.numericalDissipation+=direct-stageWork;out.maxBodyRelativeEnergyChange=Math.max(out.maxBodyRelativeEnergyChange,relative);out.maxPointRelativePower=Math.max(out.maxPointRelativePower,maxPointPower);out.maxResidual=Math.max(out.maxResidual,norm);out.maxIterations=Math.max(out.maxIterations,iterations);
   out.linearImpulses[index].set(impulse[0],impulse[1],impulse[2]).applyQuaternion(Q);out.angularImpulses[index].set(impulse[3],impulse[4],impulse[5]).applyQuaternion(Q);out.velocities[index].set(next[0],next[1],next[2]).applyQuaternion(Q);out.omegas[index].set(next[3],next[4],next[5]).applyQuaternion(Q);
  }
  return out;
 }
 function prepare(states,age=0){
  for(let index=0;index<count;index++){const body=bodies[index],state=states[index];copyQ(Q,state.orientation);Qinv.copy(Q).invert();copyV(center,state.center);for(const patch of body.patches){if(patch.dynamic){const source=patch.source;if(source.bodyIndex!==index||!(source.area>0)||Math.abs(source.localNormal.lengthSq()-1)>1e-6)throw new Error('Invalid dynamic material patch');patch.localPoint.copy(source.localPoint);patch.localNormal.copy(source.localNormal);patch.intrinsicVelocity.copy(source.intrinsicVelocity??{x:0,y:0,z:0});patch.scale=.5*density*source.area;const r=patch.localPoint,n=patch.localNormal,J=patch.J,N=patch.N,P=patch.P;J.set([1,0,0,0,r.z,-r.y,0,1,0,-r.z,0,r.x,0,0,1,r.y,-r.x,0]);N.set([n.x,n.y,n.z,r.y*n.z-r.z*n.y,r.z*n.x-r.x*n.z,r.x*n.y-r.y*n.x]);for(let i=0;i<6;i++)for(let j=0;j<6;j++)P[i*6+j]=J[i]*J[j]+J[6+i]*J[6+j]+J[12+i]*J[12+j]-N[i]*N[j];}patch.flow.fill(0);if(waterVelocity){worldPoint.copy(patch.localPoint).applyQuaternion(Q).add(center);flow.set(0,0,0);const returned=waterVelocity(worldPoint,age,flow);if(returned&&returned!==flow)copyV(flow,returned);flow.applyQuaternion(Qinv);patch.flow.set([flow.x,flow.y,flow.z]);}}}
 }
 const rates={potential:0,relativePower:0,waterPower:0,intrinsicPower:0,maxPointPower:0};
 function evaluateBody(index,value,gradient,hessian){rates.potential=evaluate(bodies[index],value,gradient,hessian);rates.relativePower=relativePower;rates.waterPower=waterPower;rates.intrinsicPower=intrinsicPower;rates.maxPointPower=maxPointPower;return rates;}
 const derivatives=(index,value)=>{const potential=evaluate(bodies[index],value,g,H);return {potential,gradient:Float64Array.from(g),hessian:Float64Array.from(H)};};
 return {step,createOutput,prepare,evaluateBody,derivatives,bodies,surface,method,definition:`Simultaneous per-body 6D ${method} frozen-frame resistive impulse; no point-order splitting; no added mass/lift/root servo`};
}

export {multiply as multiply6,factorSPD as factor6,solveFactored as solve6};
