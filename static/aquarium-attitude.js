/* Euler labels for inspection only. Physical orientation stays a quaternion. */
import {Euler} from './vendor/three.webgpu.js';
export function headAngles(quaternion){
  const e=new Euler().setFromQuaternion(quaternion,'YZX');
  return {yaw:e.y,pitch:-e.z,bank:e.x};
}
export function eulerRatesFromBodyOmega({pitch=0,bank=0},omega){
  if(Math.abs(Math.cos(pitch))<1e-8)throw new Error('Euler diagnostic singularity; use the quaternion state');
  const yawRate=(Math.cos(bank)*omega.y-Math.sin(bank)*omega.z)/Math.cos(pitch);
  const pitchRate=-Math.sin(bank)*omega.y-Math.cos(bank)*omega.z;
  return {yawRate,pitchRate,bankRate:omega.x+Math.sin(pitch)*yawRate};
}
