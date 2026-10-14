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

const validPoint=point=>point&&[point.x,point.y,point.z].every(Number.isFinite);
const directionOf=(yaw,pitch)=>({x:-Math.cos(yaw)*Math.cos(pitch),y:Math.sin(pitch),z:Math.sin(yaw)*Math.cos(pitch)});
const approach=(value,target,rate,dt)=>value+(target-value)*(1-Math.exp(-rate*dt));

export function createSwimmer(random=Math.random){
  const fish={};
  let intent,intentTime,heading,depth,depthTime,cursor,cursorAge,attention,goal;
  let gait,gaitTime,turnTime,turnCooldown,turnSign,escapeTime,wallTime;
  let decisions=0,contacts=0;
  const duration=(lo,hi)=>lo+(hi-lo)*random();
  function chooseIntent(next){
    intent=next;intentTime=next==='hover'?duration(2.5,5):next==='inspect'?duration(5,9):duration(7,13);
    if(next==='cruise')heading=wrap(fish.yaw+duration(-.45,.45));
    if(next==='inspect')depth=duration(.52,.66);
    decisions++;
  }
  function reset(){
    Object.assign(fish,referencePose,{vx:0,vy:0,vz:0,speed:0,drive:0,stroke:0,yawRate:0,pitchRate:0,bank:0,
      age:0,finPhase:0,brake:0,maneuver:0,forwardSpeed:0,finSteer:0});
    intent='cruise';intentTime=9;heading=fish.yaw;depth=fish.y;depthTime=18;
    cursor=goal=null;cursorAge=attention=escapeTime=wallTime=0;
    gait='stroke';gaitTime=1.15;turnTime=0;turnCooldown=.4;turnSign=1;
    decisions=contacts=0;
  }
  reset();
  function follow(point){
    if(!validPoint(point)){cursor=null;return;}
    // New motion attracts attention; a motionless pointer is gradually ignored.
    if(!cursor||Math.hypot(point.x-cursor.x,point.y-cursor.y,point.z-cursor.z)>.045)cursorAge=0;
    cursor={...point};
  }
  function flick(point){
    if(!validPoint(point))return;
    const dx=fish.x-point.x,dy=fish.y-point.y,dz=fish.z-point.z,distance=Math.hypot(dx,dy,dz);
    cursor=null;attention=0;
    if(distance<.72){
      // Choose and hold an escape direction. Do not reverse the turn on the
      // next frame because a cursor happens to cross the fish's centre.
      const length=Math.hypot(dx,dz);
      heading=length>.06?Math.atan2(dz,-dx):wrap(fish.yaw+(Math.sign(-.205-fish.z)||1)*.8);
      depth=clamp(fish.y+dy*.35,.55,1.16);goal=null;
      intent='escape';intentTime=2.6;escapeTime=.95;gait='stroke';gaitTime=1.1;
      turnTime=.9;turnSign=Math.sign(wrap(heading-fish.yaw))||1;
    }else {
      goal={x:clamp(point.x,-1.0,1.0),y:clamp(point.y,.55,1.17),z:clamp(point.z,-.42,.20)};
      intent='investigate';intentTime=5.5;
    }
    decisions++;
  }
  function navigate(point){
    if(!validPoint(point))return false;
    cursor=null;attention=0;goal={x:clamp(point.x,-1.05,1.05),y:clamp(point.y,.50,1.22),z:clamp(point.z,-.55,.24)};
    intent='navigate';intentTime=45;escapeTime=0;decisions++;return true;
  }
  function contain(correction){
    if(!['x','y','z'].some(axis=>Math.abs(correction[axis])>1e-6))return false;
    contacts++;
    for(const [axis,velocity] of [['x','vx'],['y','vy'],['z','vz']]){
      fish[axis]+=correction[axis];
      // Remove the penetrating component; a fish does not bounce off glass.
      if(fish[velocity]*correction[axis]<0)fish[velocity]=0;
    }
    wallTime=2;fish.brake=1;
    return true;
  }
  function step(dt){
    if(!Number.isFinite(dt)||dt<=0)return;
    dt=Math.min(dt,.05);fish.age+=dt;intentTime-=dt;depthTime-=dt;cursorAge+=dt;
    escapeTime=Math.max(0,escapeTime-dt);wallTime=Math.max(0,wallTime-dt);
    turnCooldown=Math.max(0,turnCooldown-dt);turnTime=Math.max(0,turnTime-dt);
    if(intentTime<=0){
      goal=null;
      const choice=random();
      chooseIntent(intent==='hover'||intent==='inspect'?'cruise':choice<.42?'inspect':choice<.70?'hover':'cruise');
    }
    if(depthTime<=0){
      // Long-lived depth preference. Normal cruising is mostly level; occasional
      // excursions have a reason and finish before another depth is selected.
      if(intent==='cruise')depth=duration(.61,1.07);
      depthTime=duration(16,28);
    }
    attention=approach(attention,cursor&&cursorAge>.28&&cursorAge<6.5?1:0,1.3,dt);
    let wantedYaw=heading,wantedPitch=0,desiredSpeed=intent==='hover'?0:intent==='inspect'?.065:.20;
    let range=Infinity,arrive=false;
    let target=goal;
    const curious=attention>.12&&intent!=='escape'&&intent!=='navigate';
    if(curious){
      target={x:clamp(cursor?.x??fish.x,-1.0,1.0),y:clamp((cursor?.y??fish.y)+.04,.55,1.17),z:.16};
      desiredSpeed=.14;
    }
    if(target){
      const dx=target.x-fish.x,dy=target.y-fish.y,dz=target.z-fish.z,horizontal=Math.hypot(dx,dz);
      range=Math.hypot(horizontal,dy);
      const standOff=goal?.18:.38;
      arrive=range<standOff;
      if(!arrive){
        wantedYaw=Math.atan2(dz,-dx);
        wantedPitch=clamp(Math.atan2(dy,Math.max(.35,horizontal)),-.42,.42);
        desiredSpeed*=clamp((range-standOff)/.55,.12,1);
      }else {wantedYaw=fish.yaw;desiredSpeed=0;}
      if(arrive&&intent==='navigate'){intent='hover';intentTime=4;heading=fish.yaw;depth=fish.y;goal=null;}
    }else if(intent!=='hover')wantedPitch=clamp((depth-fish.y)*.72,-.25,.25);
    if(intent==='hover'&&!curious){wantedYaw=fish.yaw;wantedPitch=0;}
    if(intent==='escape')desiredSpeed=escapeTime>0?.53:.12;

    // Forecast the occupied body, not merely the nose/root. A narrow tank needs
    // a return through its central lane before the long body can turn around.
    const lead=1.5,px=fish.x+fish.vx*lead,pz=fish.z+fish.vz*lead;
    const ex=Math.hypot(.81*Math.cos(fish.yaw),.29*Math.sin(fish.yaw));
    // Reserve the lateral sweep of an upcoming tail/turning stroke as well as
    // the straight body. Otherwise a safe-looking root still clips a pane.
    const ez=Math.hypot(.81*Math.sin(fish.yaw),.29*Math.cos(fish.yaw))
      +.19*Math.abs(Math.cos(fish.yaw))*clamp(Math.abs(fish.yawRate)/.65,0,1);
    const dangerX=Math.max(0,Math.abs(px)+ex+.24-2);
    const dangerZ=Math.max(0,Math.abs(pz+.205-.065*Math.sin(fish.yaw))+ez+.045-.895);
    if(dangerX>0&&wallTime===0)wallTime=3.4;
    let avoiding=dangerX>0||dangerZ>0||wallTime>0;
    if(wallTime>0){
      wantedYaw=fish.x<0?(fish.z>-.205?-Math.PI:Math.PI):0;
      if(!goal&&intent!=='escape')heading=wantedYaw;
    }
    // The centre of the longer tail half is behind the rig origin. Keeping that
    // occupied centre in the return lane gives both ends room during a turn.
    const laneZ=-.205+.065*Math.sin(wantedYaw);
    if(!target||avoiding){
      const d=directionOf(wantedYaw,0);
      wantedYaw=Math.atan2(d.z+clamp((laneZ-fish.z)*1.6,-.55,.55),-d.x);
    }
    const desiredEz=Math.hypot(.81*Math.sin(wantedYaw),.29*Math.cos(wantedYaw))
      +.19*Math.abs(Math.cos(wantedYaw))*clamp(Math.abs(wrap(wantedYaw-fish.yaw))/.7,0,1);
    const turningNearPane=Math.abs(fish.z+.205-.065*Math.sin(wantedYaw))+desiredEz>.845;
    if(avoiding){
      desiredSpeed=Math.min(desiredSpeed,escapeTime>0?.45:.11);wantedPitch*=.65;
      if(dangerX>.09||dangerZ>.04)desiredSpeed=Math.min(desiredSpeed,.025);
    }
    const verticalExtent=.24+.72*Math.abs(Math.sin(fish.pitch));
    if(fish.y+verticalExtent+fish.vy*lead>1.71)wantedPitch=Math.min(wantedPitch,-.10);
    if(fish.y-verticalExtent+fish.vy*lead<.10)wantedPitch=Math.max(wantedPitch,.10);

    const yawError=wrap(wantedYaw-fish.yaw),pitchError=wantedPitch-fish.pitch;
    if(Math.abs(yawError)>.16&&turnCooldown===0&&turnTime===0&&!arrive){
      turnTime=intent==='escape'?.9:.65;turnSign=Math.sign(yawError);turnCooldown=1.05;
      gait='stroke';gaitTime=Math.max(gaitTime,turnTime);
    }
    gaitTime-=dt;
    if(gaitTime<=0){
      if(gait==='stroke'){gait='coast';gaitTime=duration(.9,2.2);}
      else {gait='stroke';gaitTime=desiredSpeed>.1?duration(.8,1.5):duration(.35,.65);}
    }
    const active=(intent!=='hover'||curious)&&!arrive&&(gait==='stroke'||escapeTime>0);
    const turning=turnTime>0&&Math.sign(yawError)===turnSign;
    const finAuthority=intent==='hover'||arrive?.18:.10;
    const rateLimit=intent==='escape'?1.65:avoiding?.95:.76;
    const turnRate=turning?clamp(yawError*2.0,-rateLimit,rateLimit):clamp(yawError*.55,-finAuthority,finAuthority);
    let safeRate=turnRate,blocked=false;
    // A short angular forecast lets fins centre the animal before a tail-first
    // collision. It constrains torque, never teleports the animal away.
    const occupiedDepth=Math.abs(fish.z+.205-.065*Math.sin(fish.yaw))+ez;
    const fits=rate=>{
      const yaw=fish.yaw+rate*.30,extent=Math.hypot(.81*Math.sin(yaw),.29*Math.cos(yaw))
        +.19*Math.abs(Math.cos(yaw))*clamp(Math.max(Math.abs(fish.yawRate),Math.abs(rate))/.65,0,1);
      const projected=Math.abs(fish.z+fish.vz*.3+.205-.065*Math.sin(yaw))+extent;
      return projected<.85||projected<occupiedDepth;
    };
    if(!fits(safeRate)){
      safeRate=0;
      for(const fraction of [.75,.5,.25])if(fits(turnRate*fraction)){safeRate=turnRate*fraction;break;}
      blocked=safeRate===0;
    }
    fish.yawRate=approach(fish.yawRate,safeRate,turning?5.5:7,dt);
    fish.pitchRate=approach(fish.pitchRate,clamp(pitchError*1.7,-.33,.33),3,dt);
    fish.yaw=wrap(fish.yaw+fish.yawRate*dt);fish.pitch=clamp(fish.pitch+fish.pitchRate*dt,-.45,.45);
    const braking=arrive||(intent==='hover'&&!curious)||avoiding||turningNearPane||Math.abs(yawError)>1.15;
    fish.brake=approach(fish.brake,braking?1:0,4,dt);
    fish.maneuver=approach(fish.maneuver,turning&&!blocked?turnSign:0,5,dt);
    fish.drive=approach(fish.drive,desiredSpeed,escapeTime?6:2.3,dt);
    const safeEscape=escapeTime>0&&occupiedDepth<.86&&Math.sin(fish.yaw)*(laneZ-fish.z)>0;
    const propel=active&&(!blocked||safeEscape);
    // During a coast the tail straightens and stops driving. Heading then holds
    // apart from small fin corrections; the next deliberate stroke turns it.
    fish.stroke=approach(fish.stroke,propel?(intent==='inspect'?.42:1):turning&&!blocked?.5:0,propel?7:9,dt);
    fish.phase+=TAU*(.70+fish.drive*2.4)*fish.stroke*dt;
    fish.finPhase+=TAU*(.55+.35*fish.brake+.18*Math.abs(fish.yawRate))*dt;
    const direction=directionOf(fish.yaw,fish.pitch),flow=waterFlow(fish,fish.age);
    const forward=(fish.vx-flow.x)*direction.x+(fish.vy-flow.y)*direction.y+(fish.vz-flow.z)*direction.z;
    fish.forwardSpeed=forward;
    const powerStroke=fish.stroke*fish.stroke*(.30+1.4*Math.cos(fish.phase-4.8)**2);
    const thrust=propel?Math.max(0,(.55*fish.drive+1.0*fish.drive**2+(fish.drive-forward)*2.8)*powerStroke)*(escapeTime>0?1.9:1):0;
    const drag=(.38+fish.brake*.80)*forward+.95*forward*Math.abs(forward);
    const finForce=braking&&!arrive&&(intent!=='hover'||curious)?clamp((laneZ-fish.z)*2.0,-.12,.12)*Math.cos(fish.yaw):0;
    // Near a pane, paired-fin sculling can briefly back the fish out of a tight
    // manoeuvre. This horizontal force is subject to the same water resistance.
    const scull=blocked&&!safeEscape?clamp((laneZ-fish.z)*2.0,-.14,.14)*Math.sin(fish.yaw):0;
    fish.finSteer=approach(fish.finSteer,finForce/.12,4,dt);
    for(const [p,v] of [['x','vx'],['y','vy'],['z','vz']]){
      const sideways=fish[v]-flow[p]-direction[p]*forward;
      const fin=p==='x'?Math.sin(fish.yaw)*finForce+direction.x*scull:
        p==='z'?Math.cos(fish.yaw)*finForce+direction.z*scull:0;
      fish[v]+=(direction[p]*(thrust-drag)-sideways*(2.8+fish.brake*1.8)+fin)*dt;
      fish[p]+=fish[v]*dt;
    }
    // Last-resort root guard; the renderer also checks the actual articulated
    // surface. Navigation is responsible for turning before either is needed.
    for(const [p,v,lo,hi] of [['x','vx',-1.16,1.16],['y','vy',.47,1.27],['z','vz',-.67,.31]]){
      if(fish[p]<lo||fish[p]>hi){fish[p]=clamp(fish[p],lo,hi);fish[v]=0;wallTime=2;}
    }
    fish.speed=Math.hypot(fish.vx,fish.vy,fish.vz);
    fish.bank=approach(fish.bank,clamp(-fish.yawRate*fish.speed*.36,-.13,.13),3,dt);
  }
  return {fish,follow,flick,navigate,reset,contain,step,
    get behavior(){return {intent:attention>.12&&intent!=='escape'&&intent!=='navigate'?'curious':intent,
      gait,attention,decisions,contacts,avoiding:wallTime>0};}};
}

export function finActuation(fish,side,pectoral=true){
  const speed=fish.forwardSpeed??fish.speed??0,brake=fish.brake??0;
  const turn=clamp(((fish.yawRate??0)+(fish.finSteer??0)*.45)*side,-1,1);
  const slow=clamp(1-speed/.24,0,1),phase=fish.finPhase??fish.age*TAU*.65;
  // Independently paddling paired fins open for braking and low-speed balance;
  // they trail closer to the body during a forward propulsive burst.
  return {spread:(.035+.24*slow+.16*brake+Math.max(0,turn)*.13)*(pectoral?1:.45),
    paddle:(.035+.16*slow+.08*brake)*Math.sin(phase+(side<0?.75:0))*(pectoral?1:.40),
    sweep:-.018*speed+.008*slow*Math.sin(phase-.7+(side<0?.75:0))};
}

export function spinalField(fish,axial){
  const length=axial.at(-1)-axial[5];
  const stroke=fish.stroke??1,drive=clamp(fish.drive??fish.speed??0,0,.5);
  const lateralGain=fish.lateralGain??1,verticalGain=fish.verticalGain??1,twistGain=fish.twistGain??1;
  return axial.map((x,i)=>{
    if(i<=4)return {lateral:0,vertical:0,twist:0};
    const s=clamp((x-axial[5])/length,0,1),phase=fish.phase-4.8*s;
    const amplitude=(.09+drive*.10)*stroke*(1-.45*Math.min(1,Math.abs(fish.maneuver??0)));
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
