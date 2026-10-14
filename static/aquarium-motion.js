/* A metre-scaled swimmer: tail thrust, anisotropic drag and inertial steering. */
const TAU=Math.PI*2;
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const wrap=a=>Math.atan2(Math.sin(a),Math.cos(a));
export const referencePose=Object.freeze({x:.30,y:.77,z:.14,yaw:-.12,pitch:0,phase:0});

export function waterFlow(point,time){
  // A weak, divergence-free circulation. Its normal velocity is zero at the
  // tank walls; both swimmers and suspended particles share this same water.
  const kx=Math.PI/4,kz=Math.PI/1.79;
  const x=kx*(point.x+2),z=kz*(point.z+1.1),strength=.005*(.8+.2*Math.sin(time*.17));
  return {x:strength*kz*Math.sin(x)*Math.cos(z),y:0,z:-strength*kx*Math.cos(x)*Math.sin(z)};
}

export function createSwimmer(random=Math.random){
  const fish={...referencePose,vx:0,vy:0,vz:0,speed:0,drive:0,stroke:1,yawRate:0,pitchRate:0,bank:0,age:0};
  let cursor=null,goal={x:-.8,y:.85,z:0},timer=6,reaction=0,thrusting=true,boutTimer=2.1;
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
  function navigate(point){
    if(!point||![point.x,point.y,point.z].every(Number.isFinite))return false;
    cursor=null;goal={x:clamp(point.x,-1.05,1.05),y:clamp(point.y,.50,1.22),z:clamp(point.z,-.55,.24)};
    timer=30;reaction=0;return true;
  }
  function reset(){thrusting=true;boutTimer=2.1;Object.assign(fish,referencePose,{vx:0,vy:0,vz:0,speed:0,drive:0,stroke:1,yawRate:0,pitchRate:0,bank:0,age:0});cursor=null;goal={x:-.8,y:.85,z:0};timer=6;reaction=0;}
  function contain(correction){
    if(!['x','y','z'].some(axis=>Math.abs(correction[axis])>1e-6))return false;
    for(const [axis,velocity] of [['x','vx'],['y','vy'],['z','vz']]){
      fish[axis]+=correction[axis];
      if(fish[velocity]*correction[axis]<0)fish[velocity]*=-.12;
    }
    const length=Math.hypot(correction.x,correction.y,correction.z);
    goal={x:clamp(fish.x+correction.x/length*.6,-1.05,1.05),
      y:clamp(fish.y+correction.y/length*.25,.55,1.17),z:clamp(fish.z+correction.z/length*.55,-.6,.2)};
    timer=3;fish.drive*=.7;return true;
  }
  function step(dt){
    if(!Number.isFinite(dt)||dt<=0)return;
    dt=Math.min(dt,.05);fish.age+=dt;timer-=dt;reaction=Math.max(0,reaction-dt);
    if(timer<0||Math.hypot(fish.x-goal.x,fish.y-goal.y,fish.z-goal.z)<.18){
      goal={x:(random()-.5)*1.9,y:.55+random()*.62,z:-.45+random()*.68};timer=4+random()*6;
    }
    let target=goal;
    if(cursor&&!reaction){
      // Curious approach with a broad personal space, never a rigid chase.
      const weight=.62;
      target={x:goal.x*(1-weight)+clamp(cursor.x,-1.0,1.0)*weight,
        y:goal.y*(1-weight)+clamp(cursor.y+.05,.44,1.22)*weight,z:goal.z*(1-weight)+.24*weight};
    }
    const dx=target.x-fish.x,dy=target.y-fish.y,dz=target.z-fish.z,range=Math.hypot(dx,dy,dz);
    let wantedYaw=Math.atan2(dz,-dx),wantedPitch=clamp(Math.atan2(dy,Math.hypot(dx,dz)),-.42,.42);
    if(range<.12){wantedYaw=fish.yaw;wantedPitch=0;}
    const turnLimit=reaction?1.3:.52;
    fish.yawRate+=(clamp(wrap(wantedYaw-fish.yaw)*1.7,-turnLimit,turnLimit)-fish.yawRate)*(1-Math.exp(-dt*2.6));
    fish.pitchRate+=(clamp((wantedPitch-fish.pitch)*1.9,-.38,.38)-fish.pitchRate)*(1-Math.exp(-dt*3));
    fish.yaw=wrap(fish.yaw+fish.yawRate*dt);fish.pitch=clamp(fish.pitch+fish.pitchRate*dt,-.45,.45);
    // A propulsive bout followed by a glide: speed decays under water drag
    // while the tail relaxes, rather than translating a continuously rigid fish.
    boutTimer-=dt;
    if(boutTimer<=0){thrusting=!thrusting;boutTimer=(thrusting?1.4:.9)+random()*1.4;}
    const bout=thrusting||reaction>0;
    fish.stroke+=((bout?1:.12)-fish.stroke)*(1-Math.exp(-dt*5));
    const desired=(reaction?.54:(.12+.10*clamp(range,0,1))*(bout?1.15:.30))*(.50+.50*Math.max(0,Math.cos(wrap(wantedYaw-fish.yaw))));
    fish.drive+=(desired-fish.drive)*(1-Math.exp(-dt*(reaction?5:1.4)));
    const direction={x:-Math.cos(fish.yaw)*Math.cos(fish.pitch),y:Math.sin(fish.pitch),z:Math.sin(fish.yaw)*Math.cos(fish.pitch)};
    const flow=waterFlow(fish,fish.age);
    const forward=(fish.vx-flow.x)*direction.x+(fish.vy-flow.y)*direction.y+(fish.vz-flow.z)*direction.z;
    const tailVelocity=Math.cos(fish.phase-4.8);
    const powerStroke=fish.stroke*fish.stroke*(.35+1.3*tailVelocity*tailVelocity);
    const thrust=(.65*fish.drive+1.1*fish.drive*fish.drive+(fish.drive-forward)*1.6)*powerStroke;
    const drag=.65*forward+1.1*forward*Math.abs(forward);
    for(const [p,v] of [['x','vx'],['y','vy'],['z','vz']]){
      const sideways=fish[v]-flow[p]-direction[p]*forward;
      fish[v]+=(direction[p]*(Math.max(0,thrust)-drag)-sideways*3.5)*dt;
      fish[p]+=fish[v]*dt;
    }
    // A solid tank boundary is the final collision guard; goals turn earlier.
    for(const [p,v,lo,hi] of [['x','vx',-1.16,1.16],['y','vy',.47,1.27],['z','vz',-.67,.31]]){
      if(fish[p]<lo||fish[p]>hi){fish[p]=clamp(fish[p],lo,hi);fish[v]*=-.12;timer=0;}
    }
    fish.speed=Math.hypot(fish.vx,fish.vy,fish.vz);
    fish.phase+=TAU*(.75+fish.drive*2.2)*dt;
    fish.bank+=(clamp(-fish.yawRate*.13,-.13,.13)-fish.bank)*(1-Math.exp(-dt*3));
  }
  return {fish,follow,flick,navigate,reset,contain,step};
}

export function spinalField(fish,axial){
  const length=axial.at(-1)-axial[5];
  const stroke=fish.stroke??1,drive=clamp(fish.drive??fish.speed??0,0,.5);
  const lateralGain=fish.lateralGain??1,verticalGain=fish.verticalGain??1,twistGain=fish.twistGain??1;
  return axial.map((x,i)=>{
    if(i<=4)return {lateral:0,vertical:0,twist:0};
    const s=clamp((x-axial[5])/length,0,1),phase=fish.phase-4.8*s;
    const amplitude=(.09+drive*.10)*stroke;
    const lateral=(Math.atan(amplitude/length*(2*s*Math.sin(phase)-4.8*s*s*Math.cos(phase)))
      +clamp(fish.yawRate??0,-1.3,1.3)*(.18*s+.42*s*s))*lateralGain;
    // Carp propulsion remains predominantly lateral. Vertical flexion follows
    // pitch manoeuvres; the small secondary wave is compliance, not a dolphin kick.
    const vertical=(clamp(fish.pitchRate??0,-.5,.5)*(.70*s-.20*s*s)
      +(fish.pitch??0)*.08*s+.018*stroke*s*s*Math.sin(phase+.7))*verticalGain;
    // A banking head and lagging tail put a small torsion into the trunk.
    const torsion=(-clamp(fish.bank??0,-.3,.3)*.55*s
      +clamp(fish.yawRate??0,-1.3,1.3)*.045*s*s+(fish.torsionDrive??0)*s+.022*stroke*s*s*Math.sin(phase-1))*twistGain;
    return {lateral,vertical,twist:torsion};
  });
}
