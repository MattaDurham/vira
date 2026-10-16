/* A finite mechanics experiment, not the aquarium navigation controller.
 * All displayed axial states come from the same constrained body simulation. */
import {Vector3} from './vendor/three.webgpu.js';
import {createRapierChain} from './aquarium-articulated.js?v=89';
import {headAngles,eulerRatesFromBodyOmega} from './aquarium-attitude.js?v=89';
export const mechanicsSettings=Object.freeze({naturalFrequency:360,dampingRatio:16,motorImplementation:'sdk'});
export const mechanicsDuration=4;
export const mechanicsPresets=Object.freeze(['turn','passive','vertical','twist','compound','roll-impulse','pitch-impulse','yaw-impulse']);
const ease=value=>{const t=Math.max(0,Math.min(1,value));return t*t*t*(10+t*(-15+6*t));};
export function neutralMechanicsFish(){
  const fish={age:0,x:0,y:0,z:0,yaw:0,pitch:0,bank:0,yawRate:0,pitchRate:0,bankRate:0,
    phase:0,finPhase:0,drive:0,stroke:0,muscle1:0,muscle2:0,muscle3:0,speed:0,forwardSpeed:0,
    brake:0,finSteer:0,pitchFin:0,rollFin:0,turnAnterior:0,turnTrunk:0,turnPosterior:0,
    turnRecruitment1:0,turnRecruitment2:0,turnRecruitment3:0,torsionDrive:0};
  for(const prefix of ['yawLag','pitchLag','bankLag'])for(let i=1;i<=3;i++)fish[prefix+i]=0;
  for(const family of ['pectoral','pelvic'])for(const channel of ['Paddle','ForeAft'])for(const side of ['Plus','Minus'])fish[family+channel+side]=0;
  return fish;
}
export function mechanicsTarget(time,preset){
  const fish=neutralMechanicsFish(),load=ease(time/.35)*(1-ease((time-.6)/.5));
  if(preset==='turn'||preset==='compound')fish.turnAnterior=fish.turnTrunk=fish.turnPosterior=2.4*load;
  if(preset==='vertical'||preset==='compound')Object.assign(fish,{lateralGain:preset==='vertical'?0:1,
    twistGain:preset==='vertical'?0:1,muscle1:load,muscle2:load,muscle3:load,phase:Math.PI/2-.7,verticalGain:30});
  if(preset==='twist'||preset==='compound')fish.torsionDrive=.5*load;
  return fish;
}
export async function createMechanicsTimeline(sampler,profile,{preset='turn',rate=240}={}){
  if(!mechanicsPresets.includes(preset))throw new Error('Unknown mechanics experiment');
  if(![240,480].includes(rate))throw new Error('Mechanics studies use the validated 240 or 480 Hz rates');
  const dt=1/rate,initialFish=preset==='passive'?{...neutralMechanicsFish(),turnAnterior:2.4,turnTrunk:2.4,turnPosterior:2.4}:neutralMechanicsFish();
  const chain=await createRapierChain(sampler,profile,{dt,...mechanicsSettings,initialFish});
  const frames=[];let impulse=false;
  try{
    chain.configure(preset==='passive'||preset.includes('impulse')?'restoring':'active');
    function save(){
      const transforms=chain.boneTransforms(),measurement=chain.measure();
      const headOmega=new Vector3().copy(chain.bodies[0].angvel()),cranialVelocity=new Vector3().copy(chain.bodies[0].linvel());
      const orientation=transforms.rotations[0].clone().multiply(sampler.rest[0].clone().invert()).normalize();
      const angles=headAngles(orientation),omegaBody=headOmega.clone().applyQuaternion(orientation.clone().invert());
      const rates=Math.abs(Math.cos(angles.pitch))>1e-7?eulerRatesFromBodyOmega(angles,omegaBody):{yawRate:0,pitchRate:0,bankRate:omegaBody.x};
      // Render-origin velocity differs from the cranial rigid body's COM velocity.
      const headVelocity=cranialVelocity.sub(headOmega.clone().cross(chain.records[0].restCenter.clone().applyQuaternion(orientation)));
      const visualFish={...neutralMechanicsFish(),...angles,...rates,age:chain.age};
      if(!Number.isFinite(measurement.kineticEnergy)||!Number.isFinite(measurement.anchorError))throw new Error('Nonfinite mechanics state');
      frames.push({time:chain.age,transforms,measurement,headOmega,headVelocity,visualFish});
    }
    save();
    for(let tick=1;tick<=mechanicsDuration*rate;tick++){
      const time=(tick-.5)*dt;
      if(preset.includes('impulse')){
        if(time>=.1&&!impulse){
          const torque=preset==='roll-impulse'?{x:.1,y:0,z:0}:preset==='pitch-impulse'?{x:0,y:0,z:-.25}:{x:0,y:.5,z:0};
          chain.bodies[0].applyTorqueImpulse(torque,true);impulse=true;
        }
      }else if(preset!=='passive'){
        chain.setTargets(mechanicsTarget(time,preset));
        if(time>=1.1&&chain.mode!=='restoring')chain.configure('restoring');
      }
      chain.step();save();
    }
    return {frames,rate,dt,duration:mechanicsDuration,preset,settings:mechanicsSettings};
  }finally{chain.free();}
}
