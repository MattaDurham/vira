import {Vector3,Quaternion} from './vendor/three.webgpu.js';
import {spinalField} from './aquarium-motion.js?v=90';

const up=new Vector3(0,1,0),side=new Vector3(0,0,1),axis=new Vector3(1,0,0);
const turn=new Quaternion(),bend=new Quaternion(),twist=new Quaternion(),center=new Vector3();
const bodyRotation=new Quaternion(),bodyInverse=new Quaternion(),parentRotation=new Quaternion(),desired=new Quaternion();

export function applySwimPose(asset,joints,rest,fish){
  asset.position.set(fish.x,fish.y,fish.z);
  asset.rotation.set(0,fish.yaw,-fish.pitch,'YXZ');
  asset.rotateX(fish.bank??0);
  joints.forEach((bone,i)=>bone.quaternion.copy(rest[i]));
  asset.updateMatrixWorld(true);asset.getWorldQuaternion(bodyRotation);bodyInverse.copy(bodyRotation).invert();
  joints.forEach(bone=>{
    if(!bone.userData.bodyRest){
      bone.userData.bodyRest=bone.getWorldQuaternion(new Quaternion()).premultiply(bodyInverse);
      bone.getWorldPosition(center);asset.worldToLocal(center);
      bone.userData.bodyCenter=center.clone();bone.userData.axial=center.x;
    }
    // The authored bone's local Y follows its length, including the rest arch.
    bone.userData.bodyTangent??=new Vector3(0,1,0).applyQuaternion(bone.userData.bodyRest).normalize();
  });
  const field=spinalField(fish,joints.map(b=>b.userData.axial));
  joints.forEach((bone,i)=>{
    const f=field[i];
    // Absolute orientations in the animal's frame keep the rest arch intact.
    // Roll is about the bent longitudinal axis, not a fixed world axis.
    turn.setFromAxisAngle(up,f.lateral);bend.setFromAxisAngle(side,f.vertical);twist.setFromAxisAngle(bone.userData.bodyTangent,f.twist);
    desired.copy(bodyRotation).multiply(turn).multiply(bend).multiply(twist).multiply(bone.userData.bodyRest);
    bone.parent.getWorldQuaternion(parentRotation).invert();
    bone.quaternion.copy(parentRotation.multiply(desired));bone.updateMatrixWorld(true);
  });
  asset.updateMatrixWorld(true);
  // Keep the same Gaussian-weighted geometric center in all three axes as
  // the articulated body folds. This is geometric centering, not a physical
  // angular-momentum or mass-reaction solver; the neutral rest arch is retained.
  let longitudinal=0,lateral=0,vertical=0,total=0;
  joints.forEach(bone=>{
    bone.getWorldPosition(center);asset.worldToLocal(center);
    const original=bone.userData.bodyCenter,weight=Math.exp(-Math.pow((original.x+.18)/.30,2));
    longitudinal+=(center.x-original.x)*weight;lateral+=(center.z-original.z)*weight;vertical+=(center.y-original.y)*weight;total+=weight;
  });
  if(total){asset.translateX(-longitudinal/total);asset.translateZ(-lateral/total);asset.translateY(-vertical/total);}
  asset.updateMatrixWorld(true);
}

export function createFacialRig(asset){
  const names=['jaw_lower','jaw_upper','operculum_L','operculum_R','eye_L','eye_R'];
  const facial=names.map(name=>asset.getObjectByName(name)).filter(Boolean);
  const records=facial.map(bone=>({bone,position:bone.position.clone(),rotation:bone.quaternion.clone(),bodyRest:null}));
  const offset=new Vector3(),parentInverse=new Quaternion();
  return {
    jointCount:records.length,
    pose(fish){
      asset.getWorldQuaternion(bodyRotation);bodyInverse.copy(bodyRotation).invert();
      records.forEach(record=>{
        const {bone,position,rotation}=record;bone.position.copy(position);bone.quaternion.copy(rotation);bone.updateMatrixWorld(true);
        record.bodyRest??=bone.getWorldQuaternion(new Quaternion()).premultiply(bodyInverse);
      });
      const time=fish.age??0,cycle=time*5.3+.15*Math.sin(time*.7);
      const inhale=Math.pow(.5+.5*Math.sin(cycle),2),exhale=Math.pow(.5+.5*Math.sin(cycle-.85),2);
      records.forEach(record=>{
        const {bone,bodyRest}=record;
        const eye=bone.name.startsWith('eye_');
        if(eye){
          const phase=bone.name.endsWith('L')?0:1.7;
          turn.setFromAxisAngle(up,.018*Math.sin(time*.47+phase)-(fish.yawRate??0)*.035);
          twist.setFromAxisAngle(axis,.012*Math.sin(time*.31+phase)-(fish.bank??0)*.45);
          desired.copy(bodyRotation).multiply(turn).multiply(twist).multiply(bodyRest);
          bone.parent.getWorldQuaternion(parentRotation).invert();
          bone.quaternion.copy(parentRotation).multiply(desired);bone.updateMatrixWorld(true);return;
        }
        const jaw=bone.name.startsWith('jaw_');
        const angle=bone.name==='jaw_lower'?.018+.048*inhale:bone.name==='jaw_upper'?-.012*inhale:
          (bone.name.endsWith('L')?1:-1)*(.008+.026*exhale);
        turn.setFromAxisAngle(jaw?side:up,angle);
        desired.copy(bodyRotation).multiply(turn).multiply(bodyRest);
        bone.parent.getWorldQuaternion(parentRotation);parentInverse.copy(parentRotation).invert();
        bone.quaternion.copy(parentInverse).multiply(desired);
        if(bone.name==='jaw_upper'){
          offset.set(-.0018*inhale,.0003*inhale,0).applyQuaternion(bodyRotation).applyQuaternion(parentInverse);
          bone.position.add(offset);
        }
        bone.updateMatrixWorld(true);
      });
      asset.updateMatrixWorld(true);
      return {inhale,exhale};
    },
  };
}
