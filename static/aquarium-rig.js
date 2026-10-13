import {Vector3,Quaternion} from './vendor/three.webgpu.js';
import {rigAngles} from './aquarium-motion.js';

const bendAxis=new Vector3(0,0,1),turn=new Quaternion();

export function applySwimPose(asset,joints,rest,fish){
  asset.position.set(fish.x,fish.y,fish.z);
  asset.rotation.set(0,fish.yaw,-fish.pitch,'YXZ');
  asset.rotateX(fish.bank);
  const angles=rigAngles(fish,joints.length);
  // Blender's bones run along local Y; local Z maps to the fish's vertical
  // axis. Post-multiply there to bend the spine rather than roll the body.
  joints.forEach((bone,i)=>{
    bone.quaternion.copy(rest[i]);
    turn.setFromAxisAngle(bendAxis,angles[i]);
    bone.quaternion.multiply(turn);
  });
  asset.updateMatrixWorld(true);
}
