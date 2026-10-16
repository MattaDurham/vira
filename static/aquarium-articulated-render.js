/* Articulated mechanics study: render the actual articulated rigid-body solution.
 * No prescribed spinal field, FK repair, COM placement or reaction transform is
 * applied here. Use a dedicated createKoiInstance; its local bone translations
 * become the solver's positions until restore()/dispose() is called. */
import {Matrix4,Quaternion,Vector3} from './vendor/three.webgpu.js';
import {headAngles,eulerRatesFromBodyOmega} from './aquarium-attitude.js?v=89';

const finiteVector=v=>v&&[v.x,v.y,v.z].every(Number.isFinite);
const finiteQuaternion=q=>q&&[q.x,q.y,q.z,q.w].every(Number.isFinite)&&q.lengthSq()>1e-20;
const quaternionError=(a,b)=>{
  const d=a.clone().invert().multiply(b).normalize();
  return 2*Math.atan2(Math.hypot(d.x,d.y,d.z),Math.abs(d.w));
};

/** boneTransforms positions/quaternions are in the lane parent's unscaled,
 * neutral-model coordinate system. All positions use the same instance scale.
 * The returned sample/diagnostics are reused; snapshot them if retaining them.
 * This helper owns no geometry, skeleton, physics world or animation clock. */
export function createRapierRenderBridge(instance,{sampler}={}){
  const {asset,joints,facialRig,supports}=instance??{};
  if(!asset?.isObject3D||joints?.length!==34||sampler?.centers?.length!==34||sampler?.rest?.length!==34)
    throw new Error('Rapier render bridge requires an actual koi instance and its pristine 34-bone sampler');
  if(!['x','y','z'].every(k=>Number.isFinite(asset.scale[k])&&asset.scale[k]>0)||
    Math.abs(asset.scale.x-asset.scale.y)>1e-12||Math.abs(asset.scale.x-asset.scale.z)>1e-12)
    throw new Error('Rapier render bridge requires positive uniform animal scale');
  const scale=asset.scale.x,centers=sampler.centers.map(p=>p.clone()),restInverse=sampler.rest.map(q=>q.clone().invert());
  for(let i=1;i<34;i++)if(joints[i].parent!==joints[i-1])throw new Error('Rapier bridge requires the actual consecutive axial hierarchy');
  const originalRoot={position:asset.position.clone(),quaternion:asset.quaternion.clone(),scale:asset.scale.clone()};
  const originalBones=[];asset.traverse(b=>{if(b.isBone)originalBones.push({bone:b,position:b.position.clone(),quaternion:b.quaternion.clone(),scale:b.scale.clone()});});
  const originalFish={...instance.fish};
  const savedPositions=centers.map(()=>new Vector3()),savedRotations=centers.map(()=>new Quaternion());
  const parentMatrix=new Matrix4(),parentInverse=new Matrix4(),parentRotation=new Quaternion(),parentScale=new Vector3(),parentTranslation=new Vector3();
  const rootRotation=new Quaternion(),previousRoot=new Quaternion(),relative=new Quaternion(),temporaryQ=new Quaternion(),actualQ=new Quaternion();
  const desiredP=new Vector3(),desiredQ=new Quaternion(),actualP=new Vector3(),otherP=new Vector3(),expectedP=new Vector3(),omega=new Vector3(),rootVelocity=new Vector3(),previousPosition=new Vector3();
  const sample={...originalFish},diagnostics={scale,parentScale:1,maximumPositionError:0,maximumRotationError:0,
    maximumSegmentLengthError:0,maximumAnchorVectorError:0,physicsMaximumSegmentLengthError:0,physicsMaximumAnchorVectorError:0,
    rigidCranialLengthError:0,rigidTerminalLengthError:0,age:0};
  let previousAge,lastAge,hasPose=false,disposed=false;
  function refreshParent(){
    if(asset.parent){asset.parent.updateWorldMatrix(true,false);parentMatrix.copy(asset.parent.matrixWorld);}
    else parentMatrix.identity();
    parentMatrix.decompose(parentTranslation,parentRotation,parentScale);
    const unit=Math.max(1,Math.abs(parentScale.x));
    if(!(parentMatrix.determinant()>0)||!['x','y','z'].every(k=>Number.isFinite(parentScale[k])&&parentScale[k]>0)||
      Math.max(Math.abs(parentScale.x-parentScale.y),Math.abs(parentScale.x-parentScale.z))>1e-8*unit)
      throw new Error('Rapier render lane requires positive uniform scale without shear');
    const recomposed=new Matrix4().compose(parentTranslation,parentRotation,parentScale);
    if(Math.max(...parentMatrix.elements.map((x,i)=>Math.abs(x-recomposed.elements[i])))>1e-8*unit)
      throw new Error('Rapier render lane cannot contain shear');
    parentInverse.copy(parentMatrix).invert();diagnostics.parentScale=parentScale.x;
  }
  function measure(){
    if(!hasPose)return {...diagnostics,posed:false};
    asset.updateMatrixWorld(true);
    for(const key of ['maximumPositionError','maximumRotationError','maximumSegmentLengthError','maximumAnchorVectorError',
      'physicsMaximumSegmentLengthError','physicsMaximumAnchorVectorError','rigidCranialLengthError','rigidTerminalLengthError'])diagnostics[key]=0;
    for(let i=0;i<34;i++){
      desiredP.copy(savedPositions[i]).multiplyScalar(scale).applyMatrix4(parentMatrix);
      desiredQ.copy(parentRotation).multiply(savedRotations[i]);
      joints[i].getWorldPosition(actualP);joints[i].getWorldQuaternion(actualQ);
      diagnostics.maximumPositionError=Math.max(diagnostics.maximumPositionError,actualP.distanceTo(desiredP));
      diagnostics.maximumRotationError=Math.max(diagnostics.maximumRotationError,quaternionError(actualQ,desiredQ));
      if(i===0)continue;
      const restLength=centers[i].distanceTo(centers[i-1]),displayScale=scale*parentScale.x;
      joints[i-1].getWorldPosition(otherP);
      const lengthError=Math.abs(actualP.distanceTo(otherP)-restLength*displayScale);
      diagnostics.maximumSegmentLengthError=Math.max(diagnostics.maximumSegmentLengthError,lengthError);
      if(i<=4)diagnostics.rigidCranialLengthError=Math.max(diagnostics.rigidCranialLengthError,lengthError);
      if(i===33)diagnostics.rigidTerminalLengthError=lengthError;
      diagnostics.physicsMaximumSegmentLengthError=Math.max(diagnostics.physicsMaximumSegmentLengthError,
        Math.abs(savedPositions[i].distanceTo(savedPositions[i-1])-restLength));
      temporaryQ.copy(savedRotations[i-1]).multiply(restInverse[i-1]);
      expectedP.copy(centers[i]).sub(centers[i-1]).applyQuaternion(temporaryQ).add(savedPositions[i-1]);
      diagnostics.physicsMaximumAnchorVectorError=Math.max(diagnostics.physicsMaximumAnchorVectorError,expectedP.distanceTo(savedPositions[i]));
      expectedP.multiplyScalar(scale).applyMatrix4(parentMatrix);
      diagnostics.maximumAnchorVectorError=Math.max(diagnostics.maximumAnchorVectorError,actualP.distanceTo(expectedP));
    }
    diagnostics.age=lastAge;return diagnostics;
  }
  function pose(transforms,{age=0,visualFish={},headAngularVelocity,headLinearVelocity}={}){
    if(disposed)throw new Error('Rapier render bridge has been disposed');
    if(!Number.isFinite(age)||transforms?.positions?.length!==34||transforms?.rotations?.length!==34)
      throw new Error('Rapier bridge requires finite time and all 34 physical bone transforms');
    for(let i=0;i<34;i++){
      if(!finiteVector(transforms.positions[i])||!finiteQuaternion(transforms.rotations[i]))throw new Error('Nonfinite physical bone transform');
      savedPositions[i].copy(transforms.positions[i]);savedRotations[i].copy(transforms.rotations[i]).normalize();
    }
    refreshParent();
    rootRotation.copy(savedRotations[0]).multiply(restInverse[0]).normalize();
    asset.quaternion.copy(rootRotation);asset.position.copy(savedPositions[0]).multiplyScalar(scale)
      .sub(expectedP.copy(centers[0]).multiplyScalar(scale).applyQuaternion(rootRotation));
    asset.updateMatrixWorld(true);
    // Use actual parents, including the armature above spine_00. The exported
    // nearly-unit bone scales remain untouched; no length or pose repair hides
    // a finite solver anchor gap.
    for(let i=0;i<34;i++){
      const bone=joints[i];
      desiredP.copy(savedPositions[i]).multiplyScalar(scale).applyMatrix4(parentMatrix);
      bone.position.copy(desiredP);bone.parent.worldToLocal(bone.position);
      desiredQ.copy(parentRotation).multiply(savedRotations[i]);
      bone.parent.getWorldQuaternion(temporaryQ).invert();bone.quaternion.copy(temporaryQ).multiply(desiredQ).normalize();
      bone.updateMatrixWorld(true);
    }
    asset.updateMatrixWorld(true);
    const angles=headAngles(rootRotation),dt=previousAge===undefined?0:age-previousAge;
    omega.set(0,0,0);rootVelocity.set(0,0,0);
    if(dt>0){
      relative.copy(previousRoot).invert().multiply(rootRotation).normalize();
      if(relative.w<0)relative.set(-relative.x,-relative.y,-relative.z,-relative.w);
      const n=Math.hypot(relative.x,relative.y,relative.z),k=n<1e-12?2/dt:2*Math.atan2(n,relative.w)/(n*dt);
      omega.set(relative.x*k,relative.y*k,relative.z*k);
      rootVelocity.copy(asset.position).sub(previousPosition).multiplyScalar(1/dt);
    }
    if(headAngularVelocity){
      if(!finiteVector(headAngularVelocity))throw new Error('Nonfinite cranial angular velocity');
      omega.copy(headAngularVelocity).applyQuaternion(temporaryQ.copy(rootRotation).invert());
    }
    if(headLinearVelocity){
      if(!finiteVector(headLinearVelocity))throw new Error('Nonfinite cranial linear velocity');
      rootVelocity.copy(headLinearVelocity).multiplyScalar(scale);
    }
    const rates=Math.abs(Math.cos(angles.pitch))>1e-7?eulerRatesFromBodyOmega(angles,omega):{yawRate:0,pitchRate:0,bankRate:omega.x};
    Object.assign(sample,originalFish,visualFish,angles,rates,{age,x:asset.position.x,y:asset.position.y,z:asset.position.z,
      bodyScale:scale,vx:rootVelocity.x,vy:rootVelocity.y,vz:rootVelocity.z});
    // These helpers only articulate facial/intrinsic fin tissue and refresh
    // skin support geometry. instance.pose/step/contain must never follow this
    // call: each would overwrite the physical axial solution.
    facialRig.pose(sample);supports.pose();instance.updateVisual(sample);
    for(const skeleton of instance.ownedSkeletons??[])skeleton.update();
    previousRoot.copy(rootRotation);previousPosition.copy(asset.position);previousAge=age;lastAge=age;hasPose=true;
    return {asset,sample,diagnostics:measure()};
  }
  function restore(){
    asset.position.copy(originalRoot.position);asset.quaternion.copy(originalRoot.quaternion);asset.scale.copy(originalRoot.scale);
    for(const r of originalBones){r.bone.position.copy(r.position);r.bone.quaternion.copy(r.quaternion);r.bone.scale.copy(r.scale);}
    asset.updateMatrixWorld(true);facialRig.pose(originalFish);supports.pose();instance.updateVisual(originalFish);
    for(const skeleton of instance.ownedSkeletons??[])skeleton.update();
    previousAge=undefined;hasPose=false;return asset;
  }
  return {asset,scale,sample,pose,measure,restore,dispose(){if(!disposed){restore();disposed=true;}},
    note:'Exact articulated physics positions and quaternions; skinning and intrinsic-fin display remain the actual koi rig. No fluid optics or hydrodynamic force calibration is implied.'};
}
