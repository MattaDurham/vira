/* A metre-scaled swimmer: tail thrust, anisotropic drag and inertial steering. */
const TAU=Math.PI*2;
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const wrap=a=>Math.atan2(Math.sin(a),Math.cos(a));
export const referencePose=Object.freeze({x:.30,y:.77,z:.14,yaw:-.12,pitch:0,phase:0});

export function createSwimmer(random=Math.random){
  const fish={...referencePose,vx:0,vy:0,vz:0,speed:0,drive:0,yawRate:0,pitchRate:0,bank:0,age:0};
  let cursor=null,goal={x:-.8,y:.85,z:0},timer=6,reaction=0;
  function follow(point){cursor=point&&[point.x,point.y,point.z].every(Number.isFinite)?{...point}:null;}
  function flick(point){
    if(!point||![point.x,point.y,point.z].every(Number.isFinite))return;
    const dx=fish.x-point.x,dy=fish.y-point.y,dz=fish.z-point.z,distance=Math.hypot(dx,dy,dz);
    if(distance<.72){
      const scale=1/Math.max(distance,.04);
      goal={x:clamp(fish.x+dx*scale*.9,-1.1,1.1),y:clamp(fish.y+dy*scale*.35,.38,1.28),z:clamp(fish.z-.30,-.63,.27)};
      reaction=1.7;timer=2.4;
    }else {goal={x:clamp(point.x,-.95,.95),y:clamp(point.y,.48,1.15),z:.22};timer=4;}
  }
  function reset(){Object.assign(fish,referencePose,{vx:0,vy:0,vz:0,speed:0,drive:0,yawRate:0,pitchRate:0,bank:0,age:0});cursor=null;goal={x:-.8,y:.85,z:0};timer=6;reaction=0;}
  function step(dt){
    if(!Number.isFinite(dt)||dt<=0)return;
    dt=Math.min(dt,.05);fish.age+=dt;timer-=dt;reaction=Math.max(0,reaction-dt);
    if(timer<0||Math.hypot(fish.x-goal.x,fish.y-goal.y,fish.z-goal.z)<.18){
      goal={x:(random()-.5)*1.9,y:.46+random()*.72,z:-.45+random()*.68};timer=4+random()*6;
    }
    let target=goal;
    if(cursor&&!reaction){
      // Curious approach with a broad personal space, never a rigid chase.
      const weight=.62;
      target={x:goal.x*(1-weight)+clamp(cursor.x,-1.0,1.0)*weight,
        y:goal.y*(1-weight)+clamp(cursor.y+.05,.44,1.22)*weight,z:goal.z*(1-weight)+.24*weight};
    }
    const dx=target.x-fish.x,dy=target.y-fish.y,dz=target.z-fish.z,range=Math.hypot(dx,dy,dz);
    let wantedYaw=Math.atan2(dz,-dx),wantedPitch=clamp(Math.atan2(dy,Math.hypot(dx,dz)),-.32,.32);
    if(range<.12){wantedYaw=fish.yaw;wantedPitch=0;}
    const turnLimit=reaction?1.3:.52;
    fish.yawRate+=(clamp(wrap(wantedYaw-fish.yaw)*1.7,-turnLimit,turnLimit)-fish.yawRate)*(1-Math.exp(-dt*2.6));
    fish.pitchRate+=(clamp((wantedPitch-fish.pitch)*1.9,-.25,.25)-fish.pitchRate)*(1-Math.exp(-dt*3));
    fish.yaw=wrap(fish.yaw+fish.yawRate*dt);fish.pitch=clamp(fish.pitch+fish.pitchRate*dt,-.35,.35);
    const desired=(reaction?.48:.10+.09*clamp(range,0,1))*(.50+.50*Math.max(0,Math.cos(wrap(wantedYaw-fish.yaw))));
    fish.drive+=(desired-fish.drive)*(1-Math.exp(-dt*(reaction?5:1.4)));
    const direction={x:-Math.cos(fish.yaw)*Math.cos(fish.pitch),y:Math.sin(fish.pitch),z:Math.sin(fish.yaw)*Math.cos(fish.pitch)};
    const forward=fish.vx*direction.x+fish.vy*direction.y+fish.vz*direction.z;
    const thrust=.65*fish.drive+1.1*fish.drive*fish.drive+(fish.drive-forward)*1.6;
    const drag=.65*forward+1.1*forward*Math.abs(forward);
    for(const [p,v] of [['x','vx'],['y','vy'],['z','vz']]){
      const sideways=fish[v]-direction[p]*forward;
      fish[v]+=(direction[p]*(Math.max(0,thrust)-drag)-sideways*3.5)*dt;
      fish[p]+=fish[v]*dt;
    }
    // A solid tank boundary is the final collision guard; goals turn earlier.
    for(const [p,v,lo,hi] of [['x','vx',-1.16,1.16],['y','vy',.34,1.34],['z','vz',-.67,.31]]){
      if(fish[p]<lo||fish[p]>hi){fish[p]=clamp(fish[p],lo,hi);fish[v]*=-.12;timer=0;}
    }
    fish.speed=Math.hypot(fish.vx,fish.vy,fish.vz);
    fish.phase+=TAU*(.42+fish.speed*1.7)*dt;
    fish.bank+=(clamp(-fish.yawRate*.13,-.13,.13)-fish.bank)*(1-Math.exp(-dt*3));
  }
  return {fish,follow,flick,reset,step};
}

export function rigAngles(fish,joints=11){
  return Array.from({length:joints},(_,i)=>{
    if(i===0)return 0;
    const s=i/(joints-1),amplitude=(.014+fish.speed*.12)*s*s;
    return amplitude*Math.sin(fish.phase-s*4.0)+clamp(fish.yawRate,-1,1)*s*.045;
  });
}
