/* Shape placement from fixed neutral-envelope mass regions. This is a mass
 * proxy, not fluid reaction or a conservation-of-angular-momentum solver.
 * Navigation retains its neutral-rig reference origin, including during yaw. */
import {Matrix4,Quaternion,Vector3} from './vendor/three.webgpu.js';

const finiteVector=v=>v&&Number.isFinite(v.x)&&Number.isFinite(v.y)&&Number.isFinite(v.z);
const finiteQuaternion=q=>q&&Number.isFinite(q.x)&&Number.isFinite(q.y)&&Number.isFinite(q.z)&&Number.isFinite(q.w)&&
  Math.hypot(q.x,q.y,q.z,q.w)>1e-12;
const matrixPosition=new Vector3(),matrixRotation=new Quaternion(),matrixScale=new Vector3(),recomposed=new Matrix4();

function uniformParent(matrix){
  if(!matrix)return;
  if(!matrix.isMatrix4||!matrix.elements.every(Number.isFinite)||!(matrix.determinant()>0))
    throw new Error('Mass placement requires a finite proper parent transform');
  matrix.decompose(matrixPosition,matrixRotation,matrixScale);
  const magnitude=Math.max(1,Math.abs(matrixScale.x));
  if(!finiteVector(matrixScale)||Math.min(matrixScale.x,matrixScale.y,matrixScale.z)<=0||
    Math.max(Math.abs(matrixScale.x-matrixScale.y),Math.abs(matrixScale.x-matrixScale.z))>1e-8*magnitude)
    throw new Error('Mass placement requires a positive uniform parent scale');
  recomposed.compose(matrixPosition,matrixRotation,matrixScale);
  const tolerance=1e-8*magnitude;
  if(matrix.elements.some((v,i)=>Math.abs(v-recomposed.elements[i])>tolerance))
    throw new Error('Mass placement does not support parent shear or perspective');
}

/** Validate before extracting actual bone orientations: a sheared world matrix
 * cannot be represented by a native-body quaternion plus one uniform scale. */
export function assertMassPlacementFrame(asset){
  const scale=asset?.scale;
  if(!asset?.isObject3D||!finiteVector(scale)||Math.min(scale.x,scale.y,scale.z)<=0||
    Math.max(Math.abs(scale.x-scale.y),Math.abs(scale.x-scale.z))>1e-10*Math.max(1,scale.x))
    throw new Error('Mass placement requires a positive uniform asset scale');
  asset.updateWorldMatrix(true,false);uniformParent(asset.parent?.matrixWorld);
  return scale.x;
}

/** centers/rest are captured from a pristine rig, in unscaled asset coordinates.
 * A profile region follows the actual first bone in region.regions. Its fixed
 * material COM offset rotates with that bone; weights never depend on pose.
 * The profile merges 33 material intervals into 29 bodies; control bone 33 is
 * the terminal endpoint and does not introduce an additional mass interval.
 * All methods leave supplied frames, the profile and pristine rig unchanged. */
export function createMassPlacement({centers,rest,profile}={}){
  if(centers?.length!==34||rest?.length!==34||profile?.bodies29?.length!==29||
    !centers.every(finiteVector)||!rest.every(finiteQuaternion))
    throw new Error('Mass placement requires a pristine 34-bone frame and 29-region profile');
  const neutralCenters=centers.map(v=>new Vector3().copy(v));
  const neutralRotations=rest.map(q=>new Quaternion().copy(q).normalize());
  const inverseRest=neutralRotations.map(q=>q.clone().invert());
  const covered=new Set(),neutralCOM=new Vector3();let totalMass=0;
  const regions=profile.bodies29.map(region=>{
    if(!Number.isFinite(region.mass)||region.mass<=0||!Array.isArray(region.com)||region.com.length!==3||
      !region.com.every(Number.isFinite)||!Array.isArray(region.regions)||!region.regions.length)
      throw new Error('Mass placement requires finite positive fixed region masses and COMs');
    for(const index of region.regions){
      if(!Number.isInteger(index)||index<0||index>=33||covered.has(index))
        throw new Error('Mass placement requires a disjoint complete 33-interval material partition');
      covered.add(index);
    }
    const panel=region.regions[0],point=new Vector3(...region.com);
    totalMass+=region.mass;neutralCOM.addScaledVector(point,region.mass);
    return {panel,mass:region.mass,offset:point.sub(neutralCenters[panel])};
  });
  if(covered.size!==33)throw new Error('Mass placement profile does not cover all 33 material intervals');
  neutralCOM.divideScalar(totalMass);
  const rotation=new Quaternion(),offset=new Vector3(),posedCOM=new Vector3(),correction=new Vector3();
  const bodyInverse=new Quaternion(),worldRotation=new Quaternion();
  const createFrame=()=>({positions:neutralCenters.map(()=>new Vector3()),rotations:neutralRotations.map(()=>new Quaternion())});
  function validateFrame(positions,rotations){
    if(positions?.length!==34||rotations?.length!==34||!positions.every(finiteVector)||!rotations.every(finiteQuaternion))
      throw new Error('Mass placement requires all finite native-body bone positions and orientations');
  }
  function centroid(positions,rotations,out=new Vector3()){
    validateFrame(positions,rotations);out.set(0,0,0);
    for(const region of regions){
      rotation.copy(rotations[region.panel]).normalize().multiply(inverseRest[region.panel]);
      offset.copy(region.offset).applyQuaternion(rotation).add(positions[region.panel]);
      out.addScaledVector(offset,region.mass);
    }
    return out.divideScalar(totalMass);
  }
  function displacement(positions,rotations,out=new Vector3()){
    centroid(positions,rotations,posedCOM);return out.copy(neutralCOM).sub(posedCOM);
  }
  function captureAsset(asset,joints,out=createFrame()){
    assertMassPlacementFrame(asset);
    if(joints?.length!==34||!joints.every(bone=>bone?.isBone)||out.positions?.length!==34||out.rotations?.length!==34)
      throw new Error('Mass placement capture requires the complete actual axial hierarchy');
    asset.updateMatrixWorld(true);asset.getWorldQuaternion(bodyInverse).invert();
    for(let i=0;i<34;i++){
      joints[i].getWorldPosition(out.positions[i]);asset.worldToLocal(out.positions[i]);
      joints[i].getWorldQuaternion(worldRotation);out.rotations[i].copy(bodyInverse).multiply(worldRotation).normalize();
    }
    return out;
  }
  function rootFromReference({positions,rotations,referencePosition,orientation,scale=1,parentMatrix}={},out=new Vector3()){
    validatePlacement(referencePosition,orientation,scale,parentMatrix);
    displacement(positions,rotations,correction);correction.multiplyScalar(scale).applyQuaternion(rotation.copy(orientation).normalize());
    return out.copy(referencePosition).add(correction);
  }
  function rootFromCOM({positions,rotations,comPosition,orientation,scale=1,parentMatrix}={},out=new Vector3()){
    validatePlacement(comPosition,orientation,scale,parentMatrix);
    centroid(positions,rotations,correction);correction.multiplyScalar(scale).applyQuaternion(rotation.copy(orientation).normalize());
    return out.copy(comPosition).sub(correction);
  }
  function validatePlacement(position,orientation,scale,parentMatrix){
    if(!finiteVector(position)||!finiteQuaternion(orientation)||!Number.isFinite(scale)||scale<=0)
      throw new Error('Mass placement needs a finite parent-local anchor/orientation and positive local scale');
    uniformParent(parentMatrix);
  }
  return {createFrame,captureAsset,centroid,displacement,rootFromReference,rootFromCOM,totalMass,
    get neutralCOM(){return neutralCOM.clone();},
    note:'Fixed 29-region neutral-envelope mass proxy. Reference placement preserves F+s*Q*c0, not a fixed-world COM during yaw.',
    coordinates:'Bone P/Q: unscaled asset coordinates. Anchors and root Q: asset-parent coordinates. Scale: asset-local uniform scale only; parentMatrix validates and is never applied twice.'};
}
