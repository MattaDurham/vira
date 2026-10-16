/* Experimental distributed resistive water approximation. Material surface normals
 * give anisotropy; no root propulsion/heading servo, added mass or wake model. */
import {Vector3,Quaternion,Matrix4} from '../../../static/vendor/three.webgpu.js';
const copyV=(out,v)=>out.set(v.x??v[0],v.y??v[1],v.z??v[2]);
const copyQ=(out,q)=>out.set(q.x??q[0],q.y??q[1],q.z??q[2],q.w??q[3]).normalize();
export function buildBodySurfaceSamples(asset,profile,{azimuthBins=16,headAxialBins=8,bodyAxialBins=1,normalXBins=3,fullTriangles=false}={}){
 const skin=asset.getObjectByName('Koi_skin');if(!skin?.isSkinnedMesh)throw new Error('Actual neutral Koi_skin required');
 asset.updateMatrixWorld(true);skin.skeleton.update();const inverse=new Matrix4().copy(asset.matrixWorld).invert(),p=new Vector3(),vertices=[];
 for(let i=0;i<skin.geometry.attributes.position.count;i++)vertices.push(skin.getVertexPosition(i,p).applyMatrix4(skin.matrixWorld).applyMatrix4(inverse).clone());
 const index=skin.geometry.index,count=index?index.count:vertices.length,patches=new Map(),a=new Vector3(),b=new Vector3(),c=new Vector3(),normal=new Vector3(),center=new Vector3();let totalArea=0,triangles=0;
 const ranges=profile.bodies29.map(body=>[profile.regions[body.regions[0]].x[0],profile.regions[body.regions.at(-1)].x[1]]);
 for(let k=0;k<count;k+=3){
  a.copy(vertices[index?index.getX(k):k]);b.copy(vertices[index?index.getX(k+1):k+1]);c.copy(vertices[index?index.getX(k+2):k+2]);normal.copy(b).sub(a).cross(p.copy(c).sub(a));const area=normal.length()/2;if(area<1e-15)continue;normal.normalize();center.copy(a).add(b).add(c).multiplyScalar(1/3);
  const region=profile.regions.findIndex(r=>center.x>=r.x[0]-1e-8&&center.x<=r.x[1]+1e-8);if(region<0)throw new Error('Surface triangle outside physical body regions');
  const bodyIndex=region<=4?0:region-4,nx=bodyIndex===0?headAxialBins:bodyAxialBins,range=ranges[bodyIndex],axial=Math.min(nx-1,Math.max(0,Math.floor((center.x-range[0])/(range[1]-range[0])*nx))),azimuth=Math.min(azimuthBins-1,Math.floor((Math.atan2(normal.z,normal.y)+Math.PI)/(2*Math.PI)*azimuthBins)),nxbin=Math.min(normalXBins-1,Math.max(0,Math.floor((normal.x+1)/2*normalXBins)));
  const key=fullTriangles?`${k}`:`${bodyIndex}:${axial}:${azimuth}:${nxbin}`;let patch=patches.get(key);if(!patch){patch={bodyIndex,area:0,localPoint:new Vector3(),localNormal:new Vector3()};patches.set(key,patch);}patch.area+=area;patch.localPoint.addScaledVector(center,area);patch.localNormal.addScaledVector(normal,area);totalArea+=area;triangles++;
 }
 const samples=[...patches.values()];for(const s of samples){s.localPoint.multiplyScalar(1/s.area).sub(new Vector3(...profile.bodies29[s.bodyIndex].com));if(s.localNormal.lengthSq()<1e-20)throw new Error('Surface patch cancelled opposed normals');s.localNormal.normalize();}
 return {samples,totalArea,triangles,bodyCount:29,definition:'Actual neutral body-skin triangle areas/centroids/normals; centroid body-region assignment; rigid material patches, no gill, fin or wake area',resolution:{azimuthBins,headAxialBins,bodyAxialBins,normalXBins,fullTriangles}};
}
export function createDistributedWaterDrag(surface,{density=1000,normalCoefficient=1,tangentCoefficient=.02,isotropicCoefficient,waterVelocity}={}){
 if(!(density>0)||!(normalCoefficient>=0)||!(tangentCoefficient>=0)||(isotropicCoefficient!==undefined&&!(isotropicCoefficient>=0)))throw new Error('Water density/drag coefficients must be finite and nonnegative');
 if(![density,normalCoefficient,tangentCoefficient,isotropicCoefficient??0].every(Number.isFinite))throw new Error('Finite water parameters required');
 const samples=surface.samples??surface,bodyCount=surface.bodyCount??Math.max(...samples.map(s=>s.bodyIndex))+1;
 const createOutput=()=>({forces:Array.from({length:bodyCount},()=>new Vector3()),torques:Array.from({length:bodyCount},()=>new Vector3()),relativePower:0,bodyPower:0,waterPower:0,powerIdentityError:0,maxPointRelativePower:-Infinity});
 const scratch=createOutput(),r=new Vector3(),n=new Vector3(),u=new Vector3(),vt=new Vector3(),force=new Vector3(),pointVelocity=new Vector3(),flow=new Vector3(),center=new Vector3(),velocity=new Vector3(),omega=new Vector3(),orientation=new Quaternion(),point=new Vector3(),torque=new Vector3();
 function evaluate(states,age=0,out=scratch){
  out.forces.forEach(f=>f.set(0,0,0));out.torques.forEach(t=>t.set(0,0,0));out.relativePower=out.bodyPower=out.waterPower=out.powerIdentityError=0;out.maxPointRelativePower=-Infinity;
  for(const sample of samples){
   const s=states[sample.bodyIndex];copyV(center,s.center);copyV(velocity,s.velocity);copyV(omega,s.omega);copyQ(orientation,s.orientation);r.copy(sample.localPoint).applyQuaternion(orientation);n.copy(sample.localNormal).applyQuaternion(orientation);point.copy(center).add(r);pointVelocity.copy(omega).cross(r).add(velocity);flow.set(0,0,0);if(waterVelocity){const value=waterVelocity(point,age,flow);if(value&&value!==flow)copyV(flow,value);}u.copy(pointVelocity).sub(flow);
   const scale=-.5*density*sample.area;
   if(isotropicCoefficient!==undefined)force.copy(u).multiplyScalar(scale*isotropicCoefficient*u.length());
   else {const un=u.dot(n);vt.copy(u).addScaledVector(n,-un);force.copy(n).multiplyScalar(normalCoefficient*Math.abs(un)*un).addScaledVector(vt,tangentCoefficient*vt.length()).multiplyScalar(scale);}
   torque.copy(r).cross(force);out.forces[sample.bodyIndex].add(force);out.torques[sample.bodyIndex].add(torque);
   const relative=force.dot(u);out.relativePower+=relative;out.maxPointRelativePower=Math.max(out.maxPointRelativePower,relative);out.bodyPower+=force.dot(pointVelocity);out.waterPower+=force.dot(flow);
  }
  let wrenchPower=0;for(let i=0;i<bodyCount;i++){copyV(velocity,states[i].velocity);copyV(omega,states[i].omega);wrenchPower+=out.forces[i].dot(velocity)+out.torques[i].dot(omega);}
  out.powerIdentityError=Math.max(Math.abs(wrenchPower-out.bodyPower),Math.abs(out.bodyPower-out.waterPower-out.relativePower));return out;
 }
 return {evaluate,createOutput,surface,definition:'Instantaneous dissipative normal/tangential quadratic resistance; positive coefficients authored, no added mass/lift/unsteady/wake/wall model'};
}
export function rapierWaterStates(chain,out=chain.bodies.map(()=>({center:new Vector3(),orientation:new Quaternion(),velocity:new Vector3(),omega:new Vector3()}))){
 for(let i=0;i<chain.bodies.length;i++){const b=chain.bodies[i],s=out[i];copyV(s.center,b.worldCom());copyQ(s.orientation,b.rotation());copyV(s.velocity,b.linvel());copyV(s.omega,b.angvel());}return out;
}

// Backward-Euler point drag, split over material patches. Each operation uses
// the body's true point effective mass and cannot add relative kinetic energy.
// This is a first-order operator split; refine dt/patches rather than claiming
// continuous-law passivity alone proves a discrete jointed simulation stable.
import {multiplyTensor,solveTensor} from './angular.mjs';
export function createDissipativeWaterImpulse(surface,massProperties,{density=1000,normalCoefficient=1,tangentCoefficient=.02,waterVelocity,tolerance=1e-12,maxIterations=16}={}){
 if(![density,normalCoefficient,tangentCoefficient].every(Number.isFinite)||density<=0||normalCoefficient<0||tangentCoefficient<0)throw new Error('Finite positive density and nonnegative resistance required');
 const samples=surface.samples??surface,count=massProperties.length,bodyData=massProperties.map(p=>{if(!(p.mass>0))throw new Error('Positive material body mass required');const inverse=new Float64Array(9);for(let a=0;a<3;a++){const v=solveTensor(p.inertia,new Vector3(a===0?1:0,a===1?1:0,a===2?1:0));for(let r=0;r<3;r++)inverse[r*3+a]=v.getComponent(r);}return {mass:p.mass,inertia:Float64Array.from(p.inertia),inverse};});
 const cache=samples.map(sample=>{const b=bodyData[sample.bodyIndex],r=sample.localPoint,cross=[0,1,2].map(i=>r.clone().cross(new Vector3(i===0?1:0,i===1?1:0,i===2?1:0))),K=new Float64Array(9),inverse=new Float64Array(9);for(let i=0;i<3;i++)for(let j=0;j<3;j++)K[i*3+j]=(i===j?1/b.mass:0)+cross[i].dot(multiplyTensor(b.inverse,cross[j]));for(let a=0;a<3;a++){const v=solveTensor(K,new Vector3(a===0?1:0,a===1?1:0,a===2?1:0));for(let r=0;r<3;r++)inverse[r*3+a]=v.getComponent(r);}return {...sample,K,inverse,scale:.5*density*sample.area};});
 const createOutput=()=>({linearImpulses:Array.from({length:count},()=>new Vector3()),angularImpulses:Array.from({length:count},()=>new Vector3()),velocities:Array.from({length:count},()=>new Vector3()),omegas:Array.from({length:count},()=>new Vector3()),relativeEnergyChange:0,bodyEnergyChange:0,directEnergyChange:0,waterWork:0,maxPointRelativeEnergyChange:-Infinity,maxResidual:0,maxIterations:0});
 const output=createOutput(),vel=bodyData.map(()=>new Vector3()),omega=bodyData.map(()=>new Vector3()),Q=bodyData.map(()=>new Quaternion()),Qinv=bodyData.map(()=>new Quaternion()),centers=bodyData.map(()=>new Vector3()),localJ=bodyData.map(()=>new Vector3()),localTorque=bodyData.map(()=>new Vector3());
 const u=new Vector3(),v=new Vector3(),tangent=new Vector3(),gradient=new Vector3(),delta=new Vector3(),residual=new Vector3(),candidateResidual=new Vector3(),correction=new Vector3(),candidate=new Vector3(),j=new Vector3(),spin=new Vector3(),flow=new Vector3(),point=new Vector3(),temp=new Vector3(),H=new Float64Array(9),M=new Float64Array(9);
 const energy=(i,v,w)=>.5*bodyData[i].mass*v.lengthSq()+.5*w.dot(multiplyTensor(bodyData[i].inertia,w,temp));
 function step(states,dt,age=0,out=output){
  if(!(dt>0&&Number.isFinite(dt)))throw new Error('Positive physical water timestep required');
  let initialEnergy=0;out.relativeEnergyChange=out.bodyEnergyChange=out.directEnergyChange=out.waterWork=out.maxResidual=out.maxIterations=0;out.maxPointRelativeEnergyChange=-Infinity;
  for(let i=0;i<count;i++){copyQ(Q[i],states[i].orientation);Qinv[i].copy(Q[i]).invert();copyV(centers[i],states[i].center);copyV(vel[i],states[i].velocity).applyQuaternion(Qinv[i]);copyV(omega[i],states[i].omega).applyQuaternion(Qinv[i]);localJ[i].set(0,0,0);localTorque[i].set(0,0,0);initialEnergy+=energy(i,vel[i],omega[i]);}
  for(const patch of cache){
   const i=patch.bodyIndex,n=patch.localNormal,r=patch.localPoint,b=bodyData[i];flow.set(0,0,0);
   if(waterVelocity){point.copy(r).applyQuaternion(Q[i]).add(centers[i]);const f=waterVelocity(point,age,flow);if(f&&f!==flow)copyV(flow,f);flow.applyQuaternion(Qinv[i]);}
   u.copy(omega[i]).cross(r).add(vel[i]).sub(flow);v.copy(u);
   const compute=(value,hessian=false)=>{const un=value.dot(n);tangent.copy(value).addScaledVector(n,-un);const tn=tangent.length();gradient.copy(n).multiplyScalar(normalCoefficient*Math.abs(un)*un).addScaledVector(tangent,tangentCoefficient*tn).multiplyScalar(patch.scale);if(hessian)for(let a=0;a<3;a++)for(let c=0;c<3;c++){const na=n.getComponent(a),nc=n.getComponent(c);H[a*3+c]=patch.scale*(2*normalCoefficient*Math.abs(un)*na*nc+tangentCoefficient*(tn*((a===c?1:0)-na*nc)+(tn>1e-14?tangent.getComponent(a)*tangent.getComponent(c)/tn:0)));}return patch.scale/3*(normalCoefficient*Math.abs(un)**3+tangentCoefficient*tn**3);};
   let iterations=0,norm=Infinity;const target=tolerance*(1+u.length());
   for(;iterations<maxIterations;iterations++){
    compute(v,true);multiplyTensor(patch.inverse,delta.copy(v).sub(u),residual).addScaledVector(gradient,dt);norm=residual.length();if(norm<=target)break;
    for(let k=0;k<9;k++)M[k]=patch.inverse[k]+dt*H[k];solveTensor(M,residual,correction);let accepted=false;
    for(let fraction=1;fraction>=1/256;fraction*=.5){candidate.copy(v).addScaledVector(correction,-fraction);compute(candidate);multiplyTensor(patch.inverse,delta.copy(candidate).sub(u),candidateResidual).addScaledVector(gradient,dt);if(candidateResidual.length()<norm){v.copy(candidate);accepted=true;break;}}
    if(!accepted)break;
   }
   compute(v);multiplyTensor(patch.inverse,delta.copy(v).sub(u),residual).addScaledVector(gradient,dt);norm=residual.length();if(norm>target*20)throw new Error(`Implicit surface drag failed: ${norm}`);
   j.copy(gradient).multiplyScalar(-dt);multiplyTensor(patch.K,j,delta);const relativeChange=j.dot(u)+.5*j.dot(delta),waterWork=j.dot(flow);out.relativeEnergyChange+=relativeChange;out.waterWork+=waterWork;out.bodyEnergyChange+=relativeChange+waterWork;out.maxPointRelativeEnergyChange=Math.max(out.maxPointRelativeEnergyChange,relativeChange);out.maxResidual=Math.max(out.maxResidual,norm);out.maxIterations=Math.max(out.maxIterations,iterations);
   vel[i].addScaledVector(j,1/b.mass);spin.copy(r).cross(j);omega[i].add(multiplyTensor(b.inverse,spin,temp));localJ[i].add(j);localTorque[i].add(spin);
  }
  let finalEnergy=0;for(let i=0;i<count;i++){out.linearImpulses[i].copy(localJ[i]).applyQuaternion(Q[i]);out.angularImpulses[i].copy(localTorque[i]).applyQuaternion(Q[i]);out.velocities[i].copy(vel[i]).applyQuaternion(Q[i]);out.omegas[i].copy(omega[i]).applyQuaternion(Q[i]);finalEnergy+=energy(i,vel[i],omega[i]);}out.directEnergyChange=finalEnergy-initialEnergy;return out;
 }
 return {step,createOutput,surface,bodyData,definition:'Sequential backward-Euler material-point resistive impulses with exact rigid-body point mobility; first-order split; no added mass/lift or root servo'};
}

// This asset's caudal membrane is wholly spine_33, rigidly belonging to final
// body28 in the 29-link chain. Reject a multi-body attachment: that requires a
// shared material-point Jacobian and transpose force distribution, not snapping.
export function appendRigidMembraneSurface(asset,surface,profile,{meshName='Caudal_membrane',bodyIndex=28,binsX=8,binsY=12}={}){
 const mesh=asset.getObjectByName(meshName);if(!mesh?.isSkinnedMesh)throw new Error('Actual attached membrane required');
 const weights=mesh.geometry.attributes.skinWeight,ids=mesh.geometry.attributes.skinIndex;
 for(let i=0;i<weights.count;i++)for(let j=0;j<4;j++)if(weights.getComponent(i,j)>0){const name=mesh.skeleton.bones[ids.getComponent(i,j)].name,match=/^spine_(\d+)$/.exec(name);if(!match)throw new Error('Membrane references non-axial joint');const axial=Number(match[1]),mapped=axial<=4?0:Math.min(28,axial-4);if(mapped!==bodyIndex)throw new Error('Multi-body fin needs a shared material-point Jacobian');}
 asset.updateMatrixWorld(true);mesh.skeleton.update();const inverse=new Matrix4().copy(asset.matrixWorld).invert(),point=new Vector3(),vertices=[];let xmin=Infinity,xmax=-Infinity,ymin=Infinity,ymax=-Infinity;
 for(let i=0;i<mesh.geometry.attributes.position.count;i++){const p=mesh.getVertexPosition(i,point).applyMatrix4(mesh.matrixWorld).applyMatrix4(inverse).clone();vertices.push(p);xmin=Math.min(xmin,p.x);xmax=Math.max(xmax,p.x);ymin=Math.min(ymin,p.y);ymax=Math.max(ymax,p.y);}
 const indices=mesh.geometry.index,count=indices?indices.count:vertices.length,patches=new Map(),a=new Vector3(),b=new Vector3(),c=new Vector3(),n=new Vector3(),center=new Vector3(),temp=new Vector3();let totalArea=0,triangles=0;
 for(let k=0;k<count;k+=3){a.copy(vertices[indices?indices.getX(k):k]);b.copy(vertices[indices?indices.getX(k+1):k+1]);c.copy(vertices[indices?indices.getX(k+2):k+2]);n.copy(b).sub(a).cross(temp.copy(c).sub(a));const area=n.length()/2;if(area<1e-15)continue;n.normalize();center.copy(a).add(b).add(c).multiplyScalar(1/3);const ix=Math.max(0,Math.min(binsX-1,Math.floor((center.x-xmin)/(xmax-xmin)*binsX))),iy=Math.max(0,Math.min(binsY-1,Math.floor((center.y-ymin)/(ymax-ymin)*binsY))),key=`${ix}:${iy}`;let patch=patches.get(key);if(!patch){patch={bodyIndex,area:0,localPoint:new Vector3(),localNormal:new Vector3(),source:meshName};patches.set(key,patch);}patch.area+=area;patch.localPoint.addScaledVector(center,area);patch.localNormal.addScaledVector(n,area);totalArea+=area;triangles++;}
 const extra=[...patches.values()];for(const patch of extra){patch.localPoint.multiplyScalar(1/patch.area).sub(new Vector3(...profile.bodies29[bodyIndex].com));if(patch.localNormal.lengthSq()<1e-20)throw new Error('Opposed membrane faces in one material patch');patch.localNormal.normalize();}
 return {...surface,samples:[...surface.samples,...extra],totalArea:surface.totalArea+totalArea,triangles:surface.triangles+triangles,membrane:{meshName,bodyIndex,samples:extra.length,area:totalArea,triangles,mass:'omitted',deformation:'rigid neutral membrane; no kinematic fin cupping'}};
}
