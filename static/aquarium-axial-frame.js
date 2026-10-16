/* Neutral rig coordinates and active shape targets only. Physical mass and
 * actual dynamic transforms belong to the articulated solver. */
import {Vector3,Quaternion} from './vendor/three.webgpu.js';
import {spinalField} from './aquarium-spine.js?v=90';
const y=new Vector3(0,1,0),z=new Vector3(0,0,1);
export function createAxialFrame(asset){
  asset.updateMatrixWorld(true);
  const joints=[];asset.traverse(bone=>{if(bone.isBone&&bone.name.startsWith('spine_'))joints.push(bone);});
  joints.sort((a,b)=>a.name.localeCompare(b.name));
  if(joints.length!==34)throw new Error('Expected all 34 axial joints');
  const inverse=asset.getWorldQuaternion(new Quaternion()).invert();
  const centers=joints.map(bone=>asset.worldToLocal(bone.getWorldPosition(new Vector3())));
  const rest=joints.map(bone=>bone.getWorldQuaternion(new Quaternion()).premultiply(inverse));
  const parents=joints.map(bone=>joints.indexOf(bone.parent)),axial=centers.map(point=>point.x);
  if(parents[0]!==-1||parents.slice(1).some((parent,i)=>parent!==i))throw new Error('Expected a consecutive axial chain');
  const inverses=rest.map(rotation=>rotation.clone().invert());
  const tangents=rest.map(rotation=>y.clone().applyQuaternion(rotation).normalize());
  const createFrame=()=>({age:0,positions:centers.map(()=>new Vector3()),rotations:rest.map(()=>new Quaternion())});
  const scratch=createFrame(),turn=new Quaternion(),bend=new Quaternion(),twist=new Quaternion(),rotation=new Quaternion();
  function capture(fish,out=scratch){
    const field=spinalField(fish,axial);out.age=fish.age??0;
    for(let i=0;i<34;i++){
      const f=field[i];turn.setFromAxisAngle(y,f.lateral);bend.setFromAxisAngle(z,f.vertical);twist.setFromAxisAngle(tangents[i],f.twist);
      out.rotations[i].copy(turn).multiply(bend).multiply(twist).multiply(rest[i]);
      const parent=parents[i];
      if(parent<0)out.positions[i].copy(centers[i]);
      else out.positions[i].copy(centers[i]).sub(centers[parent]).applyQuaternion(rotation.copy(out.rotations[parent]).multiply(inverses[parent])).add(out.positions[parent]);
    }
    return out;
  }
  return {joints,centers,rest,parents,axial,createFrame,capture};
}
