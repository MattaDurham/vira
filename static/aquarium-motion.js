import {axialRates,turnCarrierShare} from './aquarium-spine.js?v=89';
export {spinalField} from './aquarium-spine.js?v=89';
import {createFinYawController,trimFinAngles} from './aquarium-fin-yaw.js?v=89';
import {finClearanceFactor,finClearanceFold} from './aquarium-fin-clearance.js?v=89';
import {createTurnController,turnResponse} from './aquarium-turn.js?v=89';
/* A metre-scaled swimmer: tail thrust, anisotropic drag and inertial steering. */
import {waterMin,waterMax,tankCenterZ,tankDepth,tankWidth} from './aquarium-space.js?v=89';

const TAU=Math.PI*2;
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
// The .04 blend widths, in normalized demand and yaw-rate radians/second,
// are authored. Compact C2 support preserves loaded fin controls and keeps
// zero demand quiet without introducing a second phase clock.
const positiveFinDemand=value=>{
  if(value<=0)return 0;
  if(value>=.04)return value;
  const t=value/.04;
  return .04*t*t*t*(6+t*(-8+3*t));
};
const finDemandMagnitude=value=>positiveFinDemand(Math.abs(value));
const wrap=a=>Math.atan2(Math.sin(a),Math.cos(a));
export const referencePose=Object.freeze({x:.30,y:.77,z:.14,yaw:-.12,pitch:0,phase:0});
const rootBounds=[['x','vx',waterMin[0]+.84,waterMax[0]-.84],
  ['y','vy',waterMin[1]+.45,waterMax[1]-.52],['z','vz',waterMin[2]+.43,waterMax[2]-.38]];

export function finPhaseRate(fish){
  return TAU*(.55+.35*(fish.brake??0)+.18*finDemandMagnitude(fish.yawRate??0));
}

export function waterFlow(point,time){
  // A weak, divergence-free circulation. Its normal velocity is zero at the
  // tank walls; both swimmers and suspended particles share this same water.
  const kx=Math.PI/tankWidth,kz=Math.PI/tankDepth;
  const x=kx*(point.x-waterMin[0]),z=kz*(point.z-waterMin[2]),strength=.005*(.8+.2*Math.sin(time*.17));
  return {x:strength*kz*Math.sin(x)*Math.cos(z),y:0,z:-strength*kx*Math.cos(x)*Math.sin(z)};
}

const validPoint=point=>point&&[point.x,point.y,point.z].every(Number.isFinite);
const directionOf=(yaw,pitch)=>({x:-Math.cos(yaw)*Math.cos(pitch),y:Math.sin(pitch),z:Math.sin(yaw)*Math.cos(pitch)});
const approach=(value,target,rate,dt)=>value+(target-value)*(1-Math.exp(-rate*dt));

const finEase=t=>t*t*t*(10+t*(-15+6*t));
const finBump=(age,start,duration)=>{
  const t=(age-start)/duration;
  return t>0&&t<1?64*t*t*t*(1-t)*(1-t)*(1-t):0;
};
const finExcursion=(age,delay=0)=>finBump(age,delay,.28)-.28*finBump(age,delay+.30,.38);
const finTaskFields=['pectoralPaddlePlus','pectoralPaddleMinus','pectoralForeAftPlus','pectoralForeAftMinus',
  'pelvicPaddlePlus','pelvicPaddleMinus','pelvicForeAftPlus','pelvicForeAftMinus'];

// Authored task repertoire, not measured koi timing or hydrodynamic forces.
// sample({age,turn,yawError,yawRate,stopping,mode}) consumes seconds, normalized
// signed steering REQUEST, heading error (rad), actual heading rate (rad/s),
// flow-weighted brake demand [0,1], and swim/hover/retreat context. Output angles
// are radians; Plus/Minus identify the authored +Z/-Z fin. Copy its scalar
// fields when storing snapshots: this output object is deliberately reused.
export function createFinTaskController(){
  const output={},a=new Float64Array(8),b=new Float64Array(8);
  let age,serial=0,requested=0,episode=0,peak=0,previousRate=0,inside=false,outside=false,brakeLatched=false,lastStrength=0;
  let hoverSince,lastBalance=-Infinity,balanceSide=1;
  function trajectory(event,now,values){
    values.fill(0);if(!event)return;
    const t=now-event.start,{kind,direction,strength}=event;
    if(kind==='trim'){
      for(const side of [1,-1]){
        const motion=trimFinAngles(t,strength,side===direction),slot=side>0?0:1;
        values[slot]=motion.paddle;values[slot+2]=motion.foreAft;
      }
    }else if(kind==='steer'||kind==='brake'||kind==='balance'){
      for(const side of [1,-1]){
        const leading=side===direction,delay=kind==='brake'?0:leading?0:.10;
        const stroke=finExcursion(t,delay),slot=side>0?0:1;
        values[slot]=.085*strength*stroke;
        // Separate basal fore/aft presentation, not the unused legacy sweep.
        values[slot+2]=(kind==='brake'?.02:(leading?1:-1)*(kind==='balance'?.02:.06))*strength*stroke;
      }
    }else{
      const slot=direction>0?4:5,stroke=finExcursion(t);
      values[slot]=.085*.35*strength*stroke;
      values[slot+2]=(kind==='inside'?.04:-.04)*.35*strength*stroke;
    }
  }
  function track(){
    let current=null,outgoing=null,queued=null,handoff=0,lastEnd=-Infinity;
    function request(descriptor,now){
      if(outgoing){
        queued=current.kind===descriptor.kind&&current.direction===descriptor.direction?null:descriptor;
        return;
      }
      const next={...descriptor,start:now,serial:++serial};
      if(current&&now<current.start+.78){outgoing=current;handoff=now;}
      current=next;
    }
    return {request,
      advance(now){
        if(outgoing&&now-handoff>=.12){
          outgoing=null;
          if(queued){const next=queued;queued=null;request(next,now);}
        }
        if(!outgoing&&current&&now>=current.start+.78){lastEnd=current.start+.78;current=null;}
      },
      ready:now=>!current&&!outgoing&&now>=lastEnd+.10,
      render(now,values){
        trajectory(current,now,values);
        if(outgoing){
          trajectory(outgoing,now,b);const blend=finEase(clamp((now-handoff)/.12,0,1));
          for(let i=0;i<8;i++)values[i]=b[i]*(1-blend)+values[i]*blend;
        }
      },
      get current(){return current;},
      cancelQueued(kind){if(queued?.kind===kind)queued=null;},
      reconcileSteering(kind,direction){
        if((queued?.kind==='trim'||queued?.kind==='steer')&&
          (queued.kind!==kind||queued.direction!==direction))queued=null;
      },
      reset(){current=outgoing=queued=null;handoff=0;lastEnd=-Infinity;}};
  }
  const pectoral=track(),pelvic=track();
  function reset(){
    age=undefined;serial=requested=episode=peak=previousRate=lastStrength=0;
    hoverSince=undefined;lastBalance=-Infinity;balanceSide=1;
    inside=outside=brakeLatched=false;pectoral.reset();pelvic.reset();
    for(const field of finTaskFields)output[field]=0;
    Object.assign(output,{finTaskMode:'swim',finTaskSerial:0,finTaskAge:0,finTaskDirection:0});return output;
  }
  reset();
  return {reset,sample(input){
    const now=input.age??0;
    if(now===age)return output;
    if(age!==undefined&&(now<age||now-age>.25))reset();
    age=now;
    const turn=clamp(input.turn??0,-1,1),error=input.yawError??turn,rate=input.yawRate??0,stopping=clamp(input.stopping??0,0,1);
    const trim=input.steeringMode==='trim';
    const significant=trim?Math.abs(error)>.04:Math.abs(turn)>.04&&Math.abs(error)>.04;
    const wanted=significant?Math.sign(turn):(trim?Math.abs(error)<.025:Math.abs(turn)<.025||Math.abs(error)<.025)?0:requested;
    const strength=trim?clamp(Math.abs(error)/.12,.35,1):clamp(.75*finDemandMagnitude(turn)+.65*stopping,0,1);
    const kind=trim?'trim':'steer',changed=wanted!==requested;
    pectoral.reconcileSteering(kind,wanted);
    // A task changes its complete C2 trajectory when the same signed intent
    // crosses between fin trim and a body manoeuvre. Direction alone cannot
    // select presentation: retaining the old stroke would delay that join.
    const presentationChanged=wanted&&pectoral.current&&
      (pectoral.current.kind!==kind||pectoral.current.direction!==wanted);
    if(changed||presentationChanged){
      requested=wanted;
      if(wanted){
        pectoral.request({kind,direction:wanted,strength},now);
        episode=wanted;peak=0;inside=outside=false;lastStrength=strength;
      }else{pectoral.cancelQueued('steer');pectoral.cancelQueued('trim');}
    }
    // Reconcile the latest request before a completed handoff may drain its
    // queue, including a release observed exactly at that handoff boundary.
    if(stopping<.04){brakeLatched=false;pectoral.cancelQueued('brake');}
    if(input.mode!=='hover'&&input.mode!=='retreat')pectoral.cancelQueued('balance');
    pectoral.advance(now);pelvic.advance(now);
    if(!changed&&requested&&pectoral.ready(now)){
      pectoral.request({kind,direction:requested,strength},now);lastStrength=strength;
    }
    // A maintained braking surface need not paddle continuously. A later
    // brake episode rearms only after actual flow-weighted demand releases.
    if(!requested&&stopping>.12&&!brakeLatched){
      pectoral.request({kind:'brake',direction:0,strength:clamp(.65*stopping,0,1)},now);brakeLatched=true;
    }
    // Explicit station-holding context permits sparse finite corrections.
    // A neutral glide does not acquire an age-driven flutter. These quiet
    // intervals and alternating leading sides are authored balance behavior.
    if(input.mode==='hover'||input.mode==='retreat'){
      if(requested||stopping>=.12||Math.abs(rate)>.025||pectoral.current||pelvic.current)hoverSince=now;
      else hoverSince??=now;
      if(!requested&&stopping<.12&&pectoral.ready(now)&&now-hoverSince>1.8&&now-lastBalance>2.4){
        pectoral.request({kind:'balance',direction:balanceSide,strength:.12},now);
        balanceSide=-balanceSide;lastBalance=now;
      }
    }else{hoverSince=undefined;pectoral.cancelQueued('balance');}
    // Pelvics respond to the observed heading trajectory, not a fixed phase
    // offset from the pectorals. The exact thresholds and gains are authored.
    if(episode){
      const signed=rate*episode;peak=Math.max(peak,signed);
      if(!inside&&signed>.035){pelvic.request({kind:'inside',direction:episode,strength:lastStrength},now);inside=true;}
      if(inside&&!outside&&peak>.055&&signed<peak*.8&&signed<previousRate*episode){
        pelvic.request({kind:'outside',direction:-episode,strength:lastStrength},now);outside=true;
      }
    }
    previousRate=rate;
    pectoral.render(now,a);for(let i=0;i<4;i++)output[finTaskFields[i]]=a[i];
    pelvic.render(now,a);for(let i=4;i<8;i++)output[finTaskFields[i]]=a[i];
    const active=pectoral.current;
    Object.assign(output,{finTaskMode:active?.kind??input.mode??'swim',finTaskSerial:serial,
      finTaskAge:active?now-active.start:0,finTaskDirection:requested});
    return output;
  }};
}

export function createSwimmer(random=Math.random,{bodyScale=1,id,finTaskFactory=createFinTaskController}={}){
  const fish={};let neighbors=[],forecastBounds=null;
  const ownScale=Number.isFinite(bodyScale)?clamp(bodyScale,.15,1.5):1;
  const finTasks=finTaskFactory(),finYaw=createFinYawController();
  const bodyTurns=createTurnController();let turnAuthority=1,totalTurnYaw=0,strokeYawStart=0,strokeSerial=0;const strokeStarts=new Map();
  let intent,intentTime,heading,depth,depthTime,cursor,cursorAge,attention,goal;
  let gait,gaitTime,turnTime,turnCooldown,turnSign,escapeTime,wallTime;
  let decisions=0,contacts=0;
  const duration=(lo,hi)=>lo+(hi-lo)*random();
  function chooseIntent(next){
    intent=next;intentTime=next==='hover'?duration(2.5,5):next==='inspect'?duration(5,9):duration(7,13);
    if(next==='cruise'){
      heading=wrap((Math.cos(fish.yaw)>=0?0:Math.PI)+duration(-.35,.35));
      // A new travel decision starts its own stroke. Reusing an inspection's
      // nearly finished stroke can leave the fish coasting before its delayed
      // posterior activation has had time to propel the new course.
      gait='stroke';gaitTime=duration(.8,1.5);
    }
    if(next==='inspect')depth=duration(.52,.66);
    decisions++;
  }
  function reset(){
    Object.assign(fish,referencePose,{bodyScale:ownScale,id,vx:0,vy:0,vz:0,speed:0,drive:0,stroke:0,yawRate:0,pitchRate:0,pitchFin:0,pitchMoment:0,
      bank:0,bankRate:0,rollFin:0,rollMoment:0,
      age:0,finPhase:0,brake:0,maneuver:0,forwardSpeed:0,finSteer:0,
      yawLag1:0,yawLag2:0,yawLag3:0,pitchLag1:0,pitchLag2:0,pitchLag3:0,
      bankLag1:0,bankLag2:0,bankLag3:0,
      muscle1:0,muscle2:0,muscle3:0,turnBend:0,turnBend1:0,turnBend2:0,turnBend3:0,
      turnRecruitment:0,turnRecruitment1:0,turnRecruitment2:0,turnRecruitment3:0});
    Object.assign(fish,finTasks.reset());finYaw.reset();bodyTurns.reset();turnAuthority=1;totalTurnYaw=strokeYawStart=strokeSerial=0;strokeStarts.clear();
    Object.assign(fish,{turnAnterior:0,turnTrunk:0,turnPosterior:0,turnAnteriorVelocity:0,turnTrunkVelocity:0,turnPosteriorVelocity:0,turnMoment:0,turnRecoveryMoment:0,turnGestureSerial:0,turnIntensity:0,turnHeadingBudget:0,turnProgress:0,turnGestureAge:0,turnGestureActive:false,turnFinClearance:1,turnPreparing:false,turnPower:0,finYawMoment:0,finYawDamping:0});
    intent='cruise';intentTime=9;heading=fish.yaw;depth=fish.y;depthTime=18;
    cursor=goal=null;neighbors=[];cursorAge=attention=escapeTime=wallTime=0;
    gait='stroke';gaitTime=1.15;turnTime=0;turnCooldown=0;turnSign=1;
    decisions=contacts=0;
  }
  reset();
  function beginTurnBend(angle){bodyTurns.request(angle,fish.age,{urgent:intent==='escape'});}
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
      heading=length>.06?Math.atan2(dz,-dx):wrap(fish.yaw+(Math.sign(tankCenterZ-fish.z)||1)*.8);
      depth=clamp(fish.y+dy*.35,.55,1.16);goal=null;
      intent='escape';intentTime=2.6;escapeTime=.95;gait='stroke';gaitTime=1.1;
      turnTime=.9;turnSign=Math.sign(wrap(heading-fish.yaw))||1;
      beginTurnBend(wrap(heading-fish.yaw),turnTime);
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
  // Snapshot-only local awareness. This changes the next finite intent, never
  // teleports a neighbour or permanently replaces the swimmer's destination.
  function observeNeighbors(records){
    neighbors=Array.isArray(records)?records.filter(n=>validPoint(n)&&(id===undefined||n.id!==id)).map(n=>({
      x:n.x,y:n.y,z:n.z,vx:Number.isFinite(n.vx)?n.vx:0,vy:Number.isFinite(n.vy)?n.vy:0,vz:Number.isFinite(n.vz)?n.vz:0,
      radius:Number.isFinite(n.radius)?clamp(n.radius,.03,1):.22,
      length:Number.isFinite(n.length)?clamp(n.length,.1,3):1.5,
      yaw:Number.isFinite(n.yaw)?n.yaw:undefined})):[];
  }
  function footprint(callback){forecastBounds=typeof callback==='function'?callback:null;}
  function forecastPose(time,rate){
    const predicted=bodyTurns.peek(fish.age+time),pose={...fish,
      x:fish.x+fish.vx*time,y:fish.y+fish.vy*time,z:fish.z+fish.vz*time,
      yaw:wrap(fish.yaw+rate*time),pitch:clamp(fish.pitch+fish.pitchRate*time,-.45,.45),
      bank:clamp((fish.bank??0)+(fish.bankRate??0)*time,-.13,.13),
      phase:fish.phase+TAU*(.70+fish.drive*2.4)*fish.stroke*time,
      turnAnterior:predicted.b[0],turnTrunk:predicted.b[1],turnPosterior:predicted.b[2],
      turnBend:predicted.b[0],turnBend1:predicted.b[0],turnBend2:predicted.b[1],turnBend3:predicted.b[2],
      turnRecruitment:predicted.activity[0],turnRecruitment1:predicted.activity[0],turnRecruitment2:predicted.activity[1],turnRecruitment3:predicted.activity[2]};
    const pieces=Math.ceil(time*120),dt=time/pieces;
    for(const [prefix,angularRate] of [['yawLag',rate],['pitchLag',fish.pitchRate],['bankLag',fish.bankRate??0]]){
      for(let i=0;i<pieces;i++){
        let previous=0;
        axialRates.forEach((response,j)=>{const key=prefix+(j+1);pose[key]=approach(pose[key]-angularRate*dt,previous,response,dt);previous=pose[key];});
      }
    }
    return pose;
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
  function integrate(dt){
    fish.age+=dt;intentTime-=dt;depthTime-=dt;cursorAge+=dt;
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
    const dangerX=Math.max(0,Math.abs(px)+ex+.24-tankWidth/2);
    const dangerZ=Math.max(0,Math.abs(pz-tankCenterZ-.065*Math.sin(fish.yaw))+ez+.045-tankDepth/2);
    if(dangerX>0&&wallTime===0)wallTime=3.4;
    let avoiding=dangerX>0||dangerZ>0||wallTime>0;
    if(wallTime>0){
      wantedYaw=fish.x<0?(fish.z>tankCenterZ?-Math.PI:Math.PI):0;
      if(!goal&&intent!=='escape')heading=wantedYaw;
    }
    // The centre of the longer tail half is behind the rig origin. Keeping that
    // occupied centre in the return lane gives both ends room during a turn.
    const laneZ=tankCenterZ+.065*Math.sin(wantedYaw);
    if(!target||avoiding){
      const d=directionOf(wantedYaw,0);
      wantedYaw=Math.atan2(d.z+clamp((laneZ-fish.z)*1.6,-.55,.55),-d.x);
    }
    const desiredEz=Math.hypot(.81*Math.sin(wantedYaw),.29*Math.cos(wantedYaw))
      +.19*Math.abs(Math.cos(wantedYaw))*clamp(Math.abs(wrap(wantedYaw-fish.yaw))/.7,0,1);
    const turningNearPane=Math.abs(fish.z-tankCenterZ-.065*Math.sin(wantedYaw))+desiredEz>tankDepth/2-.05;
    if(avoiding){
      desiredSpeed=Math.min(desiredSpeed,escapeTime>0?.45:.11);wantedPitch*=.65;
      if(dangerX>.09||dangerZ>.04)desiredSpeed=Math.min(desiredSpeed,.025);
    }
    const verticalExtent=.24+.72*Math.abs(Math.sin(fish.pitch));
    if(fish.y+verticalExtent+fish.vy*lead>waterMax[1]-.08)wantedPitch=Math.min(wantedPitch,-.10);
    if(fish.y-verticalExtent+fish.vy*lead<waterMin[1]+.08)wantedPitch=Math.max(wantedPitch,.10);

    if(neighbors.length){
      let awayX=0,awayZ=0,awayY=0,weight=0;
      const selfForward=directionOf(fish.yaw,0),scale=fish.bodyScale??ownScale;
      for(const other of neighbors){
        const rx=fish.x-other.x,rz=fish.z-other.z,rvx=fish.vx-other.vx,rvz=fish.vz-other.vz;
        const relative=rvx*rvx+rvz*rvz,t=relative>1e-8?clamp(-(rx*rvx+rz*rvz)/relative,0,1.2):.65;
        const dx=rx+rvx*t,dz=rz+rvz*t,dy=fish.y-other.y+(fish.vy-other.vy)*t;
        const distance=Math.hypot(dx,dz),nx=distance>1e-5?dx/distance:0,nz=distance>1e-5?dz/distance:1;
        const selfAxis=nx*selfForward.x+nz*selfForward.z;
        const selfExtent=Math.hypot(.72*scale*selfAxis,.22*scale*Math.sqrt(Math.max(0,1-selfAxis*selfAxis)));
        const speed=Math.hypot(other.vx,other.vz);
        const ox=other.yaw!==undefined?-Math.cos(other.yaw):speed>.005?other.vx/speed:0;
        const oz=other.yaw!==undefined?Math.sin(other.yaw):speed>.005?other.vz/speed:0;
        const axis=nx*ox+nz*oz;
        const otherExtent=ox||oz?Math.hypot(other.length*.5*axis,other.radius*Math.sqrt(Math.max(0,1-axis*axis))):other.length*.5;
        const reach=selfExtent+otherExtent+.12,vertical=.24*scale+other.radius+.10;
        const closeness=finEase(clamp((reach+.30-distance)/.30,0,1))*finEase(clamp((vertical-Math.abs(dy))/.20,0,1));
        if(!closeness)continue;
        awayX+=nx*closeness;awayZ+=nz*closeness;awayY+=Math.sign(dy)*closeness;weight+=closeness;
      }
      if(weight){
        const direction=directionOf(wantedYaw,0),avoidYaw=Math.atan2(direction.z+.80*awayZ,-direction.x-.80*awayX);
        wantedYaw=wrap(wantedYaw+clamp(wrap(avoidYaw-wantedYaw),-.75,.75));
        wantedPitch=clamp(wantedPitch+clamp(awayY*.08,-.12,.12),-.42,.42);
        desiredSpeed*=1-.65*clamp(weight,0,1);
      }
    }
    const yawError=wrap(wantedYaw-fish.yaw),pitchError=wantedPitch-fish.pitch;
    bodyTurns.request(fish.turnGestureActive&&Math.abs(yawError)>.16&&Math.sign(yawError)!==Math.sign(fish.turnHeadingBudget)?yawError:0,fish.age,{urgent:intent==='escape'});
    if(Math.abs(yawError)>.16&&turnCooldown===0&&bodyTurns.ready(fish.age)&&!arrive){
      turnTime=intent==='escape'?.9:1.25;turnSign=Math.sign(yawError);turnCooldown=.16;
      beginTurnBend(yawError,turnTime);
      gait='stroke';gaitTime=Math.max(gaitTime,turnTime);
    }
    gaitTime-=dt;
    if(gaitTime<=0){
      if(gait==='stroke'){gait='coast';gaitTime=duration(.9,2.2);}
      else {gait='stroke';gaitTime=desiredSpeed>.1?duration(.8,1.5):duration(.35,.65);}
    }
    const active=(intent!=='hover'||curious)&&!arrive&&(gait==='stroke'||escapeTime>0);
    const gesture=bodyTurns.sample(fish.age);
    if(gesture.serial!==strokeSerial){strokeSerial=gesture.serial;strokeYawStart=totalTurnYaw;strokeStarts.set(strokeSerial,strokeYawStart);
      for(const key of strokeStarts.keys())if(key!==gesture.serial&&key!==gesture.outgoingSerial)strokeStarts.delete(key);}
    const headingProgress=totalTurnYaw-strokeYawStart;
    const outgoingHeadingProgress=totalTurnYaw-(strokeStarts.get(gesture.outgoingSerial)??totalTurnYaw);
    Object.assign(fish,{turnAnterior:gesture.b[0],turnTrunk:gesture.b[1],turnPosterior:gesture.b[2],
      turnAnteriorVelocity:gesture.v[0],turnTrunkVelocity:gesture.v[1],turnPosteriorVelocity:gesture.v[2],
      turnGestureSerial:gesture.serial,turnGestureAge:gesture.age,turnGestureActive:gesture.active,turnFinClearance:gesture.finClearance,turnPreparing:gesture.preparing,turnPower:gesture.power,turnHeadingBudget:gesture.budget,turnIntensity:gesture.intensity,turnProgress:headingProgress,
      turnRecruitment:gesture.activity[0],turnRecruitment1:gesture.activity[0],turnRecruitment2:gesture.activity[1],turnRecruitment3:gesture.activity[2],
      turnBend:gesture.b[0],turnBend1:gesture.b[0],turnBend2:gesture.b[1],turnBend3:gesture.b[2]});
    const turning=gesture.active,finAuthority=intent==='hover'||arrive?.18:.10;
    const finRate=clamp(yawError*.55,-finAuthority,finAuthority);
    const occupiedDepth=Math.abs(fish.z-tankCenterZ-.065*Math.sin(fish.yaw))+ez;
    const clearance=bounds=>bounds?Math.min(...['x','y','z'].flatMap((key,i)=>[
      bounds.min[key]-waterMin[i],waterMax[i]-bounds.max[key]])):-Infinity;
    // A complete circumscribed radius gates the more detailed asset forecast;
    // a fish well inside all panes needs no repeated private-rig evaluations.
    const size=fish.bodyScale??ownScale,radius=.98*size+.04;
    const vertical=(.27+.85*Math.abs(Math.sin(fish.pitch))+.85*Math.abs(Math.sin(fish.bank??0)))*size+.04;
    const needsFootprint=forecastBounds&&(Math.abs(fish.x)+Math.abs(fish.vx)*.30+radius>tankWidth/2-.05
      ||Math.abs(fish.z-tankCenterZ)+Math.abs(fish.vz)*.30+radius>tankDepth/2-.05
      ||fish.y-Math.abs(fish.vy)*.30-vertical<waterMin[1]+.05||fish.y+Math.abs(fish.vy)*.30+vertical>waterMax[1]-.05);
    const currentClearance=needsFootprint?clearance(forecastBounds(fish)):-Infinity;
    const fits=rate=>{
      if(needsFootprint){
        // The callback derives bounds from this asset's actual material/rig.
        // Evaluate the issued future C field and causal lag at intermediate
        // times, rather than testing only a rotated straight-body ellipsoid.
        for(const time of [.10,.20,.30]){
          const next=clearance(forecastBounds(forecastPose(time,rate)));
          if(!Number.isFinite(next)||next<.025&&next<currentClearance-1e-6)return false;
        }
        return true;
      }
      const yaw=fish.yaw+rate*.30,extent=Math.hypot(.81*Math.sin(yaw),.29*Math.cos(yaw))
        +.19*Math.abs(Math.cos(yaw))*clamp(Math.max(Math.abs(fish.yawRate),Math.abs(rate))/.65,0,1);
      const projected=Math.abs(fish.z+fish.vz*.3-tankCenterZ-.065*Math.sin(yaw))+extent;
      return projected<tankDepth/2-.045||projected<occupiedDepth;
    };
    Object.assign(fish,finTasks.sample({age:fish.age,turn:gesture.preparing?0:clamp(finRate+(gesture.active?gesture.budget*.35:0),-1,1),yawError,
      steeringMode:gesture.active?'body':'trim',yawRate:fish.yawRate,stopping:fish.brake*clamp((Math.abs(fish.forwardSpeed)-.025)/.075,0,1),
      mode:intent==='hover'&&!arrive?'hover':'swim'}));
    const finReaction=finYaw.sample(fish.age,finActuation(fish,1,true),finActuation(fish,-1,true));
    const finMoment=finReaction.moment,finDamping=finReaction.damping;
    fish.finYawMoment=finMoment;fish.finYawDamping=finDamping;
    const predicted=turnResponse(gesture,fish.yawRate,.30,{finMoment,finDamping,headingProgress,outgoingHeadingProgress}).yawRate;
    let allowed=1;
    if(!fits(predicted)){allowed=0;for(const part of [.75,.5,.25])if(fits(fish.yawRate+(predicted-fish.yawRate)*part)){allowed=part;break;}}
    const blocked=allowed===0;turnAuthority=approach(turnAuthority,allowed,12,dt);
    const response=turnResponse(gesture,fish.yawRate,dt,{finMoment,finDamping,allowed:turnAuthority,headingProgress,outgoingHeadingProgress});
    fish.turnMoment=response.moment;fish.turnRecoveryMoment=response.recoveryMoment;
    fish.yawRate=response.yawRate;
    // A depth decision first changes the paired fins' signed basal incidence.
    // Their shared neutral-relative control moment then accelerates pitch;
    // head rotation is the response, not the command used to decorate the fins.
    // Limits retain the previous manoeuvre authority. Gain and damping remain
    // authored proxies, not lift coefficients or measured koi muscle timing.
    const pitchFinTarget=clamp(pitchError*1.7,-.33,.33)*.15/.33;
    fish.pitchFin=approach(fish.pitchFin,pitchFinTarget,10,dt);
    fish.pitchMoment=finPitchMoment(fish);
    fish.pitchRate=clamp(fish.pitchRate+(fish.pitchMoment-3*fish.pitchRate)*dt,-.33,.33);
    const yawDelta=fish.yawRate*dt,pitchDelta=clamp(fish.pitch+fish.pitchRate*dt,-.45,.45)-fish.pitch;
    fish.yaw=wrap(fish.yaw+yawDelta);fish.pitch+=pitchDelta;totalTurnYaw+=yawDelta;
    // The stiff anterior frame leads; successive axial regions follow it.
    // Relative offsets survive a heading wrap without a two-pi tail impulse.
    for(const [prefix,delta] of [['yawLag',yawDelta],['pitchLag',pitchDelta]]){
      let previous=0;
      axialRates.forEach((rate,i)=>{
        const name=prefix+(i+1);
        fish[name]=approach(fish[name]-delta,previous,rate,dt);previous=fish[name];
      });
    }
    const braking=arrive||(intent==='hover'&&!curious)||avoiding||turningNearPane||Math.abs(yawError)>1.15;
    fish.brake=approach(fish.brake,braking?1:0,4,dt);
    fish.maneuver=approach(fish.maneuver,turning&&!blocked?Math.sign(gesture.budget):0,5,dt);
    fish.drive=approach(fish.drive,desiredSpeed,escapeTime?6:2.3,dt);
    const safeEscape=escapeTime>0&&occupiedDepth<tankDepth/2-.035&&Math.sin(fish.yaw)*(laneZ-fish.z)>0;
    const propel=active&&(!blocked||safeEscape);
    // During a coast the tail straightens and stops driving. Heading then holds
    // apart from small fin corrections; the next deliberate stroke turns it.
    fish.stroke=approach(fish.stroke,propel?(intent==='inspect'?.42:1):turning&&!blocked?.5:0,propel?7:9,dt);
    let activation=fish.stroke;
    axialRates.forEach((rate,i)=>{
      const name='muscle'+(i+1);
      fish[name]=approach(fish[name],activation,rate,dt);activation=fish[name];
    });
    fish.phase+=TAU*(.70+fish.drive*2.4)*fish.stroke*dt;
    fish.finPhase+=finPhaseRate(fish)*dt;
    const direction=directionOf(fish.yaw,fish.pitch),flow=waterFlow(fish,fish.age);
    const forward=(fish.vx-flow.x)*direction.x+(fish.vy-flow.y)*direction.y+(fish.vz-flow.z)*direction.z;
    fish.forwardSpeed=forward;
    // The loaded turn replaces the cruise carrier in the actual posterior
    // body. Let the same regional blend replace its phase-dependent force
    // with the carrier's mean effort; an invisible beat cannot surge the root.
    // This preserves authored mean drive, not a measured hydrodynamic force.
    const carrier=turnCarrierShare(fish.turnRecruitment3);
    // Tail amplitude follows the observed posterior activation, while carrier
    // frequency follows stroke. Their product lets a finishing visible stroke
    // release its effort smoothly after the gait requests a coast. This is an
    // authored force proxy, not tissue mechanics or a fitted EMG relationship.
    const tailDrive=fish.muscle3*fish.stroke;
    // The suppressed carrier can contribute only while the issued finite body
    // gesture actually moves. Preparation and a frozen/settled C field cannot
    // acquire mean forward power from an invisible cruise beat.
    const powerStroke=tailDrive*tailDrive*((1-carrier)*fish.turnPower+carrier*(.30+1.4*Math.cos(fish.phase-4.8)**2));
    const thrust=(!blocked||safeEscape)?Math.max(0,(.55*fish.drive+1.0*fish.drive**2+(fish.drive-forward)*2.8)*powerStroke)*(escapeTime>0?1.9:1):0;
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
    for(const [p,v,lo,hi] of rootBounds){
      if(fish[p]<lo||fish[p]>hi){fish[p]=clamp(fish[p],lo,hi);fish[v]=0;wallTime=2;}
    }
    fish.speed=Math.hypot(fish.vx,fish.vy,fish.vz);
    // Keep the existing restrained bank preference; it is an authored turn
    // policy, not a claim that all fish lean in the same direction. Differential
    // basal fin incidence now produces its neutral-relative control moment.
    const bankTarget=clamp(-fish.yawRate*fish.speed*.36,-.13,.13);
    fish.rollFin=approach(fish.rollFin,clamp((bankTarget-fish.bank)*3,-.15,.15),10,dt);
    fish.rollMoment=finRollMoment(fish);
    fish.bankRate=clamp(fish.bankRate+(fish.rollMoment-8*fish.bankRate)*dt,-.33,.33);
    const previousBank=fish.bank;
    fish.bank=clamp(fish.bank+fish.bankRate*dt,-.13,.13);
    if(Math.abs(fish.bank)===.13&&fish.bankRate*fish.bank>0)fish.bankRate=0;
    let previousLag=0;
    axialRates.forEach((rate,i)=>{
      const key='bankLag'+(i+1);
      fish[key]=approach(fish[key]-(fish.bank-previousBank),previousLag,rate,dt);previousLag=fish[key];
    });
  }
  function step(elapsed,onTick){
    if(!Number.isFinite(elapsed)||elapsed<=0)return;
    // Integrate all ordinary elapsed time with stable, short water-force steps.
    // A quarter-second cap prevents a jump after a suspended tab resumes.
    const dt=Math.min(elapsed,.25),pieces=Math.max(1,Math.ceil(dt*120-1e-9));
    for(let i=0;i<pieces;i++){integrate(dt/pieces);onTick?.(fish);}
  }
  return {fish,follow,flick,navigate,neighbors:observeNeighbors,footprint,reset,contain,step,
    get behavior(){return {intent:attention>.12&&intent!=='escape'&&intent!=='navigate'?'curious':intent,
      gait,attention,decisions,contacts,avoiding:wallTime>0};}};
}

export function finActuation(fish,side,pectoral=true){
  const speed=Math.abs(fish.forwardSpeed??fish.speed??0),brake=clamp(fish.brake??0,0,1),age=fish.age??0;
  const slow=clamp(1-speed/.18,0,1);
  const turn=clamp((fish.yawRate??0)*.9+(fish.finSteer??0)*.35,-1,1);
  // Frozen inspection fixtures have no live control state. Their signed trim
  // remains explicit; a live swimmer always supplies the causal basal state.
  const trim=clamp(Number.isFinite(fish.pitchFin)?fish.pitchFin/.25:
    (fish.pitchRate??0)*2+(fish.pitch??0)*.15,-.6,.6);
  // A stopped fish still has its brake flag set. Deploy against actual flow;
  // that flag alone must not produce perpetual paddling during a quiet hover.
  const stopping=brake*clamp((speed-.025)/.075,0,1);
  const control=clamp(.65*stopping+.75*finDemandMagnitude(turn)+.65*finDemandMagnitude(trim),0,1);
  // Occasional balance corrections have long quiet intervals and independently
  // timed sides. Their restrained timing is authored, not measured carp data.
  const hover=clamp(1-speed/.065,0,1)*(1-clamp((fish.stroke??0)/.3,0,1))
    *Math.max(0,Math.sin(age*.79+1.1*Math.sin(age*.173)+(side<0?1.65:0)+(pectoral?0:1.15)))**10;
  const phase=(fish.finPhase??age*TAU*.65)+(side<0?.75:0)+(pectoral?0:1.35);
  const cycle=((phase/TAU)%1+1)%1;
  // A short loaded excursion and a weaker feathered return replace the equal
  // sine-wave strokes. With no control demand, forward glides hold the fins.
  const beat=cycle<.38?Math.sin(Math.PI*cycle/.38)**2:-.28*Math.sin(Math.PI*(cycle-.38)/.62)**2;
  const gain=pectoral?1:.35;
  const family=pectoral?'pectoral':'pelvic',suffix=side>0?'Plus':'Minus';
  const taskPaddle=fish[family+'Paddle'+suffix],taskForeAft=fish[family+'ForeAft'+suffix];
  const tasked=fish.poseHistory!==false&&Number.isFinite(taskPaddle),clearance=finClearanceFactor(fish);
  return {spread:(.035+.07*slow+.18*stopping+.12*positiveFinDemand(turn*side)+.05*finDemandMagnitude(trim))*gain*clearance,
    paddle:clearance*(tasked?taskPaddle:(.085*control+.14*hover)*beat*gain),
    foreAft:clearance*(tasked?taskForeAft??0:0)+finClearanceFold(clearance,pectoral),
    sweep:(-.010*speed+.003*(control+.4*hover)*beat)*gain,
    incidence:clearance*(trim*.25*(pectoral?-1:.35)+(fish.rollFin??0)*side*(pectoral?1:.35))};
}

export function finPitchMoment(fish){
  const incidence=pectoral=>(finActuation(fish,-1,pectoral).incidence+finActuation(fish,1,pectoral).incidence)/2;
  // Fore and aft controls form a signed couple about the body. The reference
  // fin is already cambered, so this is a change from neutral control rather
  // than an absolute force inferred from a two-dimensional foil angle.
  const effort=clamp((-incidence(true)+incidence(false))/(.15*1.35),-1,1);
  return .99*effort;
}

export function finRollMoment(fish){
  const difference=pectoral=>(finActuation(fish,1,pectoral).incidence-finActuation(fish,-1,pectoral).incidence)/2;
  // Both fore and aft pairs contribute to the same roll couple. Common pitch
  // trim cancels. This proxy is relative to each neutral fin, not an absolute
  // pressure estimate from the asymmetric authored foil surfaces.
  const effort=clamp((difference(true)+difference(false))/(.15*1.35),-1,1);
  return .99*effort;
}
