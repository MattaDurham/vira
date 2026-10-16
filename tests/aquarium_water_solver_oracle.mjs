/* Independent dense KKT oracle for the public experimental water solver. Does not import a water/body solver,
 * projection routine, block factorization, motor helper or derivative helper. */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {Vector3,Quaternion} from '../static/vendor/three.webgpu.js';

const dot=(a,b)=>a.reduce((sum,value,i)=>sum+value*b[i],0);
const matrix=(r,c=r)=>Array.from({length:r},()=>Array(c).fill(0));
const multiply=(A,x)=>A.map(row=>dot(row,x));
const norm=a=>Math.hypot(...a);
const V=a=>new Vector3(...a);
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
function denseSolve(input,rhs){
  const n=rhs.length,A=input.map((row,i)=>[...row,rhs[i]]);
  for(let column=0;column<n;column++){
    let pivot=column;for(let row=column+1;row<n;row++)if(Math.abs(A[row][column])>Math.abs(A[pivot][column]))pivot=row;
    assert.ok(Math.abs(A[pivot][column])>1e-14,'independent dense KKT matrix must be nonsingular');
    [A[pivot],A[column]]=[A[column],A[pivot]];
    const diagonal=A[column][column];for(let j=column;j<=n;j++)A[column][j]/=diagonal;
    for(let row=0;row<n;row++)if(row!==column){const amount=A[row][column];for(let j=column;j<=n;j++)A[row][j]-=amount*A[column][j];}
  }
  return A.map(row=>row[n]);
}
function quaternionLog(q){
  const s=q.clone().normalize();if(s.w<0)s.set(-s.x,-s.y,-s.z,-s.w);
  const size=Math.hypot(s.x,s.y,s.z),scale=size<1e-12?2:2*Math.atan2(size,s.w)/size;
  return [scale*s.x,scale*s.y,scale*s.z];
}
function axisQuaternion(axis,angle){return new Quaternion().setFromAxisAngle(V(axis).normalize(),angle);}
function pointJacobian(r){
  // Columns are independent unit translation/rotation experiments, not a
  // copied skew-array convention from the implementation under test.
  return [0,1,2].map(axis=>Array.from({length:6},(_,column)=>{
    const basis=new Vector3(column%3===0?1:0,column%3===1?1:0,column%3===2?1:0);
    return (column<3?basis:basis.cross(r)).getComponent(axis);
  }));
}
export function muscleWaterFixture(){
  const states=Array.from({length:4},(_,i)=>({
    center:new Vector3(),orientation:axisQuaternion([.3+i*.1,.6-i*.07,.2+i*.13],.3*(i+1)),
    velocity:V([.2+i*.03,-.1+i*.02,.14-i*.04]),omega:V([.3-i*.07,-.2+i*.05,.1+i*.04])
  }));
  const massProperties=states.map((_,i)=>({mass:2+i,inertia:[.3+i*.1,.02,-.01,.02,.4+i*.1,.03,-.01,.03,.5+i*.1]}));
  const targets=[axisQuaternion([.2,.7,-.3],.15),axisQuaternion([.8,-.4,.2],-.4),axisQuaternion([-.3,.2,.8],.2)];
  const links=Array.from({length:3},(_,i)=>({parent:i,child:i+1,a:V([.12,.04,-.03]),b:V([-.1,.03,.01]),
    stiffness:[3.2,5.1,2.4][i],damping:[.8,.35,1.1][i],cap:[1.6,.09,.3][i],target:targets[i]}));
  for(const link of links)states[link.child].center.copy(link.a).applyQuaternion(states[link.parent].orientation)
    .add(states[link.parent].center).sub(link.b.clone().applyQuaternion(states[link.child].orientation));
  const samples=[];
  for(let bodyIndex=0;bodyIndex<4;bodyIndex++)for(let j=0;j<2;j++)samples.push({bodyIndex,
    localPoint:V([.05+j*.03,.09-j*.05,-.03+j*.02]),localNormal:V([.2+j*.4,.7-j*.2,.5]).normalize(),
    area:.02-j*.007,intrinsicVelocity:V([.02*(bodyIndex%2?1:-1),-.1+j*.03,.015]),dynamic:true});
  const waterVelocity=(point,age,out=new Vector3())=>out.set(.04+.025*point.y,.03+.01*point.z,-.02+.015*point.x+.01*age);
  return {states,massProperties,links,surface:{samples},options:{density:111,normalCoefficient:.7,tangentCoefficient:.03,waterVelocity},age:.47};
}

export function solveDenseMuscleWater(fixture,dt,{motors=true,water=true}={}){
  const {states,massProperties,links,surface,options,age}=fixture,N=states.length,n=6*N,m=links.length*3;
  const M=matrix(n),J=matrix(m,n),old=[],motorsData=[];
  for(let i=0;i<N;i++){
    const body=massProperties[i];for(let axis=0;axis<3;axis++)M[i*6+axis][i*6+axis]=body.mass;
    for(let a=0;a<3;a++)for(let b=0;b<3;b++)M[i*6+3+a][i*6+3+b]=body.inertia[a*3+b];
    const inverse=states[i].orientation.clone().invert();
    old.push(...states[i].velocity.clone().applyQuaternion(inverse).toArray(),...states[i].omega.clone().applyQuaternion(inverse).toArray());
  }
  for(let index=0;index<links.length;index++){
    const link=links[index],B=matrix(3,n),relative=states[link.parent].orientation.clone().invert().multiply(states[link.child].orientation).normalize();
    for(const [body,r,sign] of [[link.parent,link.a,1],[link.child,link.b,-1]]){
      const columns=pointJacobian(r);
      for(let c=0;c<6;c++){
        const v=V(columns.map(row=>row[c])).applyQuaternion(states[body].orientation).multiplyScalar(sign);
        for(let axis=0;axis<3;axis++)J[index*3+axis][body*6+c]=v.getComponent(axis);
      }
    }
    for(let c=0;c<3;c++){
      B[c][link.parent*6+3+c]=-1;
      const rotated=new Vector3(c===0?1:0,c===1?1:0,c===2?1:0).applyQuaternion(relative);
      for(let axis=0;axis<3;axis++)B[axis][link.child*6+3+c]=rotated.getComponent(axis);
    }
    motorsData.push({link,B,error:quaternionLog(relative.clone().multiply(link.target.clone().invert()))});
  }
  const patches=surface.samples.map(patch=>{
    const state=states[patch.bodyIndex],worldPoint=patch.localPoint.clone().applyQuaternion(state.orientation).add(state.center);
    const flow=options.waterVelocity(worldPoint,age,new Vector3()).applyQuaternion(state.orientation.clone().invert());
    return {patch,flow,J:pointJacobian(patch.localPoint)};
  });
  const kkt=(H,g,constraint)=>{
    const system=matrix(n+m),rhs=[...g,...constraint];
    for(let a=0;a<n;a++)for(let b=0;b<n;b++)system[a][b]=H[a][b];
    for(let a=0;a<m;a++)for(let b=0;b<n;b++)system[n+a][b]=system[b][n+a]=J[a][b];
    return denseSolve(system,rhs);
  };
  function evaluate(x,derivatives=true){
    const delta=x.map((v,i)=>v-old[i]),gradient=multiply(M,delta),H=M.map(row=>[...row]);
    let objective=.5*dot(delta,gradient),relativeWork=0,waterWork=0,intrinsicWork=0,muscleWork=0;
    const waterGradient=Array(n).fill(0),motorGradient=Array(n).fill(0),axes=[];
    if(water)for(const {patch,flow,J:localJ} of patches){
      const body=patch.bodyIndex,velocity=multiply(localJ,x.slice(body*6,body*6+6)),u=velocity.map((v,i)=>v-flow.getComponent(i)+patch.intrinsicVelocity.getComponent(i));
      const normal=patch.localNormal.toArray(),un=dot(normal,u),t=u.map((v,i)=>v-un*normal[i]),tn=norm(t),scale=.5*options.density*patch.area;
      const a=normal.map((v,i)=>scale*(options.normalCoefficient*Math.abs(un)*un*v+options.tangentCoefficient*tn*t[i]));
      objective+=dt*scale/3*(options.normalCoefficient*Math.abs(un)**3+options.tangentCoefficient*tn**3);
      relativeWork-=dt*dot(a,u);waterWork-=dt*dot(a,flow.toArray());intrinsicWork-=dt*dot(a,patch.intrinsicVelocity.toArray());
      for(let i=0;i<6;i++){
        const g=localJ.reduce((sum,row,axis)=>sum+row[i]*a[axis],0);gradient[body*6+i]+=dt*g;waterGradient[body*6+i]+=g;
        if(derivatives)for(let j=0;j<6;j++)for(let k=0;k<3;k++)for(let l=0;l<3;l++){
          const D=scale*(2*options.normalCoefficient*Math.abs(un)*normal[k]*normal[l]
            +options.tangentCoefficient*(tn*((k===l?1:0)-normal[k]*normal[l])+(tn>1e-15?t[k]*t[l]/tn:0)));
          H[body*6+i][body*6+j]+=dt*localJ[k][i]*D*localJ[l][j];
        }
      }
    }
    if(motors)for(const {link,B,error} of motorsData){
      const w=multiply(B,x);
      for(let axis=0;axis<3;axis++){
        // The documented optional capacity changes only parent-frame Y. It
        // rescales stiffness, damping and torque together, retaining the same
        // preferred relative rate rather than merely changing saturation.
        const ratio=axis===1&&options.lateralTorqueCapsNm?options.lateralTorqueCapsNm[link.parent]/link.cap:1;
        const stiffness=link.stiffness*ratio,damping=link.damping*ratio,cap=link.cap*ratio,c=damping+dt*stiffness;
        const preferredRate=-stiffness*error[axis]/c,shifted=w[axis]-preferredRate,raw=c*shifted,g=clamp(raw,-cap,cap),saturated=Math.abs(raw)>=cap;
        objective+=dt*(saturated?cap*Math.abs(shifted)-.5*cap*cap/c:.5*c*shifted*shifted);
        muscleWork-=dt*g*w[axis];axes.push({link:link.parent,axis,cap,stiffness,damping,preferredRate,raw,torque:-g,saturated,relativeSpeed:w[axis],error:error[axis]});
        for(let i=0;i<n;i++){
          gradient[i]+=dt*B[axis][i]*g;motorGradient[i]+=B[axis][i]*g;
          if(derivatives&&!saturated)for(let j=0;j<n;j++)H[i][j]+=dt*c*B[axis][i]*B[axis][j];
        }
      }
    }
    return {objective,gradient,H,relativeWork,waterWork,intrinsicWork,muscleWork,waterGradient,motorGradient,axes};
  }
  let x=kkt(M,multiply(M,old),Array(m).fill(0)).slice(0,n),iterations=0;
  const projectionEnergyChange=.5*(dot(x,multiply(M,x))-dot(old,multiply(M,old)));
  for(;iterations<100;iterations++){
    const at=evaluate(x),direction=kkt(at.H,at.gradient.map(v=>-v),multiply(J,x).map(v=>-v)).slice(0,n),slope=dot(at.gradient,direction);
    if(norm(direction)<2e-13)break;
    let accepted=false;
    for(let alpha=1;alpha>=1/65536;alpha*=.5){
      const candidate=x.map((v,i)=>v+alpha*direction[i]);
      if(evaluate(candidate,false).objective<=at.objective+1e-4*alpha*slope+1e-15){x=candidate;accepted=true;break;}
    }
    assert.ok(accepted,'independent dense convex solve must descend');
  }
  assert.ok(iterations<100,'independent dense solve must converge');
  const at=evaluate(x),JJT=J.map(row=>J.map(other=>dot(row,other))),lambda=denseSolve(JJT,multiply(J,at.gradient));
  const stationarity=at.gradient.map((v,i)=>v-J.reduce((sum,row,j)=>sum+row[i]*lambda[j],0));
  const delta=x.map((v,i)=>v-old[i]),numericalDissipation=-.5*dot(delta,multiply(M,delta));
  const directEnergyChange=.5*(dot(x,multiply(M,x))-dot(old,multiply(M,old)));
  const energyIdentityError=Math.abs(directEnergyChange-at.relativeWork-at.waterWork+at.intrinsicWork-at.muscleWork-numericalDissipation);
  assert.ok(norm(stationarity)<1e-10&&norm(multiply(J,x))<1e-11&&energyIdentityError<1e-10,'independent dense stationarity, constraints and work balance');
  return {x,old,M,J,at,iterations,stationarityResidual:norm(stationarity),anchorVelocityResidual:norm(multiply(J,x)),
    directEnergyChange,numericalDissipation,projectionEnergyChange,energyIdentityError};
}

/** All reference math above is independent of the implementation's API. Its
 * documented motorMode='damping' means zero stiffness; no unsupported off mode
 * is silently assumed. */
export async function assertMuscleWaterImpulse(factory){
  const report={cases:[],maximumVelocityDifference:0,maximumImpulseDifference:0,maximumWorkDifference:0,
    maximumMotorTorqueDifference:0,maximumMotorImpulseDifference:0,maximumReactionLinearResidual:0,maximumReactionAngularResidual:0,worldCovarianceError:0};
  let baselineLocal;
  for(const [label,dt,water,damping,rotated,capacity] of [['mixed',.075,true,false,false],['small-step',1/240,true,false,false],['motor-only',.075,false,false,false],['damping-only',.075,true,true,false],['world-covariance',.075,true,false,true],['lateral-capacity',.075,true,false,false,true]]){
    const f=muscleWaterFixture();
    if(capacity)f.options.lateralTorqueCapsNm=f.links.map((link,i)=>link.cap*[.35,.8,1.4][i]);
    if(rotated){
      const rotation=axisQuaternion([.2,.7,-.4],.83),translation=V([.4,-.2,.7]),originalFlow=f.options.waterVelocity;
      for(const state of f.states){state.center.applyQuaternion(rotation).add(translation);state.orientation.premultiply(rotation);state.velocity.applyQuaternion(rotation);state.omega.applyQuaternion(rotation);}
      f.options.waterVelocity=(p,age,out=new Vector3())=>originalFlow(p.clone().sub(translation).applyQuaternion(rotation.clone().invert()),age,out).applyQuaternion(rotation);
    }
    // Keep positive authored stiffness on the actual link. Only the independent
    // reference suppresses it; otherwise ignoring motorMode would pass by luck.
    const referenceFixture=damping?{...f,links:f.links.map(link=>({...link,stiffness:0}))}:f;
    const reference=solveDenseMuscleWater(referenceFixture,dt,{water});
    const options={...f.options,...(!water?{normalCoefficient:0,tangentCoefficient:0}:{}),...(damping?{motorMode:'damping'}:{})};
    const operator=factory(f.surface,f.massProperties,f.links,options);
    const result=operator.step(f.states,dt,f.age,operator.createOutput?.()),actual=[];
    for(let i=0;i<f.states.length;i++){
      const inverse=f.states[i].orientation.clone().invert();actual.push(...result.velocities[i].clone().applyQuaternion(inverse).toArray(),...result.omegas[i].clone().applyQuaternion(inverse).toArray());
    }
    const velocityDifference=Math.max(...actual.map((v,i)=>Math.abs(v-reference.x[i])));report.maximumVelocityDifference=Math.max(report.maximumVelocityDifference,velocityDifference);
    assert.ok(velocityDifference<2e-8,`${label}: complete constrained velocities must agree with independent dense KKT solve (error ${velocityDifference})`);
    if(label==='mixed')baselineLocal=[...actual];
    if(rotated){report.worldCovarianceError=Math.max(...actual.map((v,i)=>Math.abs(v-baselineLocal[i])));assert.ok(report.worldCovarianceError<2e-8,'the complete articulated kick is covariant under world rotation/translation, including spatially varying moving water');}
    let impulseDifference=0;
    const delta=reference.x.map((v,i)=>v-reference.old[i]),expectedImpulse=multiply(reference.M,delta);
    for(let i=0;i<f.states.length;i++){
      const inverse=f.states[i].orientation.clone().invert(),linear=result.linearImpulses[i].clone().applyQuaternion(inverse),angular=result.angularImpulses[i].clone().applyQuaternion(inverse);
      impulseDifference=Math.max(impulseDifference,...[...linear.toArray(),...angular.toArray()].map((v,j)=>Math.abs(v-expectedImpulse[i*6+j])));
    }
    report.maximumImpulseDifference=Math.max(report.maximumImpulseDifference,impulseDifference);assert.ok(impulseDifference<3e-8,`${label}: actual impulses include water, muscle and constraint reaction exactly once`);
    const works={directEnergyChange:reference.directEnergyChange,fluidRelativeWork:reference.at.relativeWork,waterWork:reference.at.waterWork,
      intrinsicWork:reference.at.intrinsicWork,motorWork:reference.at.muscleWork,numericalDissipation:reference.numericalDissipation,projectionEnergyChange:reference.projectionEnergyChange};
    let workDifference=0;for(const [key,value] of Object.entries(works)){assert.ok(Number.isFinite(result[key]),`${label}: mandatory ${key} must be reported`);workDifference=Math.max(workDifference,Math.abs(result[key]-value));}
    report.maximumWorkDifference=Math.max(report.maximumWorkDifference,workDifference);assert.ok(workDifference<3e-8,`${label}: independently computed work terms agree`);
    assert.ok(Number.isFinite(result.energyIdentityError)&&result.energyIdentityError<3e-8,`${label}: reported complete energy identity must close`);
    const motorImpulse=f.states.map(()=>new Vector3());let torqueDifference=0,rateDifference=0;
    for(const axis of reference.at.axes){
      const link=f.links[axis.link],worldImpulse=new Vector3().setComponent(axis.axis,-dt*axis.torque).applyQuaternion(f.states[link.parent].orientation);
      motorImpulse[link.parent].add(worldImpulse);motorImpulse[link.child].sub(worldImpulse);
      assert.ok(result.motorTorquesParent?.[axis.link]?.isVector3&&result.motorRelativeRates?.[axis.link]?.isVector3,'actual motor torques and endpoint rates must be reported');
      torqueDifference=Math.max(torqueDifference,Math.abs(result.motorTorquesParent[axis.link].getComponent(axis.axis)-axis.torque));
      rateDifference=Math.max(rateDifference,Math.abs(result.motorRelativeRates[axis.link].getComponent(axis.axis)-axis.relativeSpeed));
    }
    assert.ok(torqueDifference<3e-8&&rateDifference<3e-8,`${label}: actual per-axis saturated torque and relative rate agree independently`);
    let motorImpulseDifference=0;const linearReaction=new Vector3(),angularReaction=new Vector3();
    for(let i=0;i<f.states.length;i++){
      assert.ok(result.motorAngularImpulses?.[i]?.isVector3,'individual world motor impulses must be reported');
      motorImpulseDifference=Math.max(motorImpulseDifference,result.motorAngularImpulses[i].distanceTo(motorImpulse[i]));
      const waterP=V(reference.at.waterGradient.slice(i*6,i*6+3)).multiplyScalar(-dt).applyQuaternion(f.states[i].orientation);
      const waterL=V(reference.at.waterGradient.slice(i*6+3,i*6+6)).multiplyScalar(-dt).applyQuaternion(f.states[i].orientation);
      const reactionP=result.linearImpulses[i].clone().sub(waterP),reactionL=result.angularImpulses[i].clone().sub(waterL);
      linearReaction.add(reactionP);angularReaction.add(f.states[i].center.clone().cross(reactionP)).add(reactionL);
    }
    report.maximumMotorTorqueDifference=Math.max(report.maximumMotorTorqueDifference,torqueDifference);
    report.maximumMotorImpulseDifference=Math.max(report.maximumMotorImpulseDifference,motorImpulseDifference);
    report.maximumReactionLinearResidual=Math.max(report.maximumReactionLinearResidual,linearReaction.length());
    report.maximumReactionAngularResidual=Math.max(report.maximumReactionAngularResidual,angularReaction.length());
    assert.ok(motorImpulseDifference<3e-8&&linearReaction.length()<3e-8&&angularReaction.length()<3e-8,`${label}: internal motor/anchor impulses conserve total world force and moment, including child-frame adjoint`);
    if(water)assert.ok(Math.abs(reference.at.intrinsicWork)>1e-7&&Math.abs(reference.at.waterWork)>1e-7,'moving tissue and moving water must actually perform nonzero endpoint work');
    report.cases.push({label,dt,velocityDifference,impulseDifference,workDifference,denseIterations:reference.iterations,
      stationarityResidual:reference.stationarityResidual,anchorVelocityResidual:reference.anchorVelocityResidual,independentEnergyIdentityError:reference.energyIdentityError,
      expectedMuscleWork:reference.at.muscleWork,saturatedAxes:reference.at.axes.filter(a=>a.saturated).length,unsaturatedAxes:reference.at.axes.filter(a=>!a.saturated).length,
      solvedBranchCoverage:{positiveSaturation:reference.at.axes.some(a=>a.raw>1.2*a.cap),negativeSaturation:reference.at.axes.some(a=>a.raw< -1.2*a.cap),nontrivialInterior:reference.at.axes.some(a=>Math.abs(a.raw)>.1*a.cap&&Math.abs(a.raw)<.7*a.cap)},
      ...(capacity?{lateralTorqueCapsNm:f.options.lateralTorqueCapsNm,expectedAxisParameters:reference.at.axes.map(({link,axis,cap,stiffness,damping,preferredRate})=>({link,axis,cap,stiffness,damping,preferredRate}))}:{}),
      works,actualMuscleWork:result.motorWork,reportedEnergyIdentityError:result.energyIdentityError,torqueDifference,rateDifference,motorImpulseDifference,
      reactionLinearResidual:linearReaction.length(),reactionAngularResidual:angularReaction.length()});
  }
  const mixed=report.cases[0];assert.ok(mixed.saturatedAxes>0&&mixed.unsaturatedAxes>0,'fixture must exercise simultaneous saturated and unsaturated motors');
  assert.ok(Object.values(mixed.solvedBranchCoverage).every(Boolean),'solved fixture must have well separated positive/negative saturation and nontrivial interior branches');
  return report;
}


// Frozen synthetic kicks only: no GLB, Rapier world, recording or private files.
const solverURL=new URL('../scripts/aquarium/water-study/muscle-water.mjs',import.meta.url);
const {createMuscleWaterImpulse}=await import(solverURL.href);
const report=await assertMuscleWaterImpulse(createMuscleWaterImpulse);
assert.equal(report.cases.length,6,'all six independent frozen-kick fixtures must run');
const solverSource=await readFile(solverURL,'utf8');
const mutations=[
  {name:'wrong-child-adjoint',before:'.applyQuaternion(relative);',after:'.applyQuaternion(relative.clone().invert());'},
  {name:'motor-work-sign',before:'out.motorWork+=dt*torque.dot(rates)',after:'out.motorWork-=dt*torque.dot(rates)'},
  {name:'intrinsic-work-sign',before:'out.intrinsicWork+=dt*e.intrinsicPower',after:'out.intrinsicWork-=dt*e.intrinsicPower'},
  {name:'wrong-child-motor-impulse',before:'out.motorAngularImpulses[a.child].add(v)',after:'out.motorAngularImpulses[a.child].sub(v)'},
  {name:'ignored-damping-mode',before:"motorMode==='damping'?0:a.link.stiffness",after:'a.link.stiffness'},
  {name:'cap-only-lateral-response',replacements:[
    {before:'a.alphaAxes[j]=a.alpha*ratio',after:'a.alphaAxes[j]=a.alpha'},
    {before:'-stiffness*ratio*a.error.getComponent(j)/a.alphaAxes[j]',after:'-stiffness*a.error.getComponent(j)/a.alphaAxes[j]'}
  ]},
  {name:'uncapped-motor-gradient',before:'g=Math.max(-cap,Math.min(cap,raw))',after:'g=raw'}
];
const rejected=[];
for(const mutation of mutations){
  let modified=solverSource;
  for(const change of mutation.replacements??[mutation]){
    assert.equal(modified.split(change.before).length-1,1,`mutation ${mutation.name} must alter its intended physical operation exactly once`);
    modified=modified.replace(change.before,change.after);
  }
  // Resolve file imports from the production source URL before constructing
  // an in-memory mutant. URL conversion works on Windows and macOS alike;
  // the real public solver and helper files are never changed.
  modified=modified.replace(/from '([^']+)'/g,(match,path)=>path.startsWith('.')?`from '${new URL(path,solverURL).href}'`:match);
  const mutant=await import('data:text/javascript;base64,'+Buffer.from(modified,'utf8').toString('base64'));
  let failure;
  try{await assertMuscleWaterImpulse(mutant.createMuscleWaterImpulse);}catch(error){failure=String(error.message);}
  assert.ok(failure,`independent outcome oracle must reject ${mutation.name}`);
  rejected.push({name:mutation.name,reason:failure});
}
assert.equal(rejected.length,7,'all seven physical-operation mutations must fail');
console.log(JSON.stringify({cases:report.cases.map(c=>c.label),rejectedMutations:rejected.map(m=>m.name),
  maximumVelocityDifference:report.maximumVelocityDifference,maximumImpulseDifference:report.maximumImpulseDifference,
  maximumWorkDifference:report.maximumWorkDifference,maximumMotorTorqueDifference:report.maximumMotorTorqueDifference,
  worldCovarianceError:report.worldCovarianceError}));
