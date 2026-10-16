/* A metre-scaled swimmer: tail thrust, anisotropic drag and inertial steering. */
import {waterMin,waterMax,tankCenterZ,tankDepth,tankWidth} from './aquarium-space.js?v=80';

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
const axialRates=[14,12,10],axialDelays=[0];
axialRates.forEach(rate=>axialDelays.push(axialDelays.at(-1)+1/rate));
const alongBody=(values,s)=>{
  const t=clamp(s,0,1)*3,i=Math.min(2,Math.floor(t));
  return values[i]+(values[i+1]-values[i])*(t-i);
};
const turnCarrierShare=activity=>{
  const load=clamp(activity/.25,0,1);
  return 1-load*load*(3-2*load);
};

export function createSwimmer(random=Math.random){
  const fish={};
  let intent,intentTime,heading,depth,depthTime,cursor,cursorAge,attention,goal;
  let gait,gaitTime,turnTime,turnCooldown,turnSign,escapeTime,wallTime;
  let bendTime,bendDuration,bendStrength,bendStart;
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
    Object.assign(fish,referencePose,{vx:0,vy:0,vz:0,speed:0,drive:0,stroke:0,yawRate:0,pitchRate:0,pitchFin:0,pitchMoment:0,
      bank:0,bankRate:0,rollFin:0,rollMoment:0,
      age:0,finPhase:0,brake:0,maneuver:0,forwardSpeed:0,finSteer:0,
      yawLag1:0,yawLag2:0,yawLag3:0,pitchLag1:0,pitchLag2:0,pitchLag3:0,
      bankLag1:0,bankLag2:0,bankLag3:0,
      muscle1:0,muscle2:0,muscle3:0,turnBend:0,turnBend1:0,turnBend2:0,turnBend3:0,
      turnRecruitment:0,turnRecruitment1:0,turnRecruitment2:0,turnRecruitment3:0});
    intent='cruise';intentTime=9;heading=fish.yaw;depth=fish.y;depthTime=18;
    cursor=goal=null;cursorAge=attention=escapeTime=wallTime=0;
    gait='stroke';gaitTime=1.15;turnTime=0;turnCooldown=.4;turnSign=1;
    bendTime=bendDuration=1;bendStrength=bendStart=0;
    decisions=contacts=0;
  }
  reset();
  function beginTurnBend(angle,duration){
    // Fin trim needs no conspicuous C bend. Larger intended turns recruit a
    // bounded, one-sided stroke; these magnitudes are authored for this rig.
    const q=clamp((Math.abs(angle)-.16)/(Math.PI/2-.16),0,1);
    bendStrength=Math.sign(angle)*.22*q*q*(3-2*q);
    bendStart=fish.turnBend;
    bendTime=0;bendDuration=duration;
  }
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
    bendTime+=dt;
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

    const yawError=wrap(wantedYaw-fish.yaw),pitchError=wantedPitch-fish.pitch;
    if(Math.abs(yawError)>.16&&turnCooldown===0&&turnTime===0&&!arrive){
      turnTime=intent==='escape'?.9:.65;turnSign=Math.sign(yawError);turnCooldown=1.05;
      beginTurnBend(yawError,turnTime);
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
    const finRate=clamp(yawError*.55,-finAuthority,finAuthority);
    // Turning effort builds and releases with the same activation envelope
    // that bends the body. Low-authority fin trim can lead that loaded stroke.
    const turnEffort=clamp(fish.maneuver*Math.sign(yawError),0,1);
    const turnRate=finRate+turnEffort*(clamp(yawError*2.0,-rateLimit,rateLimit)-finRate);
    let safeRate=turnRate,blocked=false;
    // A short angular forecast lets fins centre the animal before a tail-first
    // collision. It constrains torque, never teleports the animal away.
    const occupiedDepth=Math.abs(fish.z-tankCenterZ-.065*Math.sin(fish.yaw))+ez;
    const fits=rate=>{
      const yaw=fish.yaw+rate*.30,extent=Math.hypot(.81*Math.sin(yaw),.29*Math.cos(yaw))
        +.19*Math.abs(Math.cos(yaw))*clamp(Math.max(Math.abs(fish.yawRate),Math.abs(rate))/.65,0,1);
      const projected=Math.abs(fish.z+fish.vz*.3-tankCenterZ-.065*Math.sin(yaw))+extent;
      return projected<tankDepth/2-.045||projected<occupiedDepth;
    };
    if(!fits(safeRate)){
      safeRate=0;
      for(const fraction of [.75,.5,.25])if(fits(turnRate*fraction)){safeRate=turnRate*fraction;break;}
      blocked=safeRate===0;
    }
    fish.yawRate=approach(fish.yawRate,safeRate,turning?5.5:7,dt);
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
    fish.yaw=wrap(fish.yaw+yawDelta);fish.pitch+=pitchDelta;
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
    fish.maneuver=approach(fish.maneuver,turning&&!blocked?turnSign:0,5,dt);
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
    // A finite loading stroke returns toward straight before the next bout.
    // Regional response carries both loading and release toward the peduncle;
    // it does not reset or borrow the independent propulsive carrier phase.
    const u=clamp(bendTime/bendDuration,0,1),pulse=u<.3?u/.3:(1-u)/.7;
    const shaped=pulse*pulse*(3-2*pulse);
    fish.turnBend=u<.3?bendStart+(bendStrength-bendStart)*shaped:bendStrength*shaped;
    // Unsigned activity shares the bend's causal response. Opposing turns can
    // cross zero while their still-loaded posterior regions remain recruited.
    fish.turnRecruitment=Math.abs(fish.turnBend)/.22;
    let recruited=fish.turnRecruitment;
    axialRates.forEach((rate,i)=>{
      const key='turnRecruitment'+(i+1);
      fish[key]=approach(fish[key],recruited,rate,dt);recruited=fish[key];
    });
    let bending=fish.turnBend;
    axialRates.forEach((rate,i)=>{
      const key='turnBend'+(i+1);
      fish[key]=approach(fish[key],bending,rate,dt);bending=fish[key];
    });
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
    const powerStroke=tailDrive*tailDrive*((1-carrier)+carrier*(.30+1.4*Math.cos(fish.phase-4.8)**2));
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
  function step(elapsed){
    if(!Number.isFinite(elapsed)||elapsed<=0)return;
    // Integrate all ordinary elapsed time with stable, short water-force steps.
    // A quarter-second cap prevents a jump after a suspended tab resumes.
    const dt=Math.min(elapsed,.25),pieces=Math.max(1,Math.ceil(dt*120-1e-9));
    for(let i=0;i<pieces;i++)integrate(dt/pieces);
  }
  return {fish,follow,flick,navigate,reset,contain,step,
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
  return {spread:(.035+.07*slow+.18*stopping+.12*positiveFinDemand(turn*side)+.05*finDemandMagnitude(trim))*gain,
    paddle:(.085*control+.14*hover)*beat*gain,
    sweep:(-.010*speed+.003*(control+.4*hover)*beat)*gain,
    incidence:trim*.25*(pectoral?-1:.35)+(fish.rollFin??0)*side*(pectoral?1:.35)};
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

export function spinalField(fish,axial){
  const length=axial.at(-1)-axial[5];
  const stroke=fish.stroke??1,drive=clamp(fish.drive??fish.speed??0,0,.5);
  const lateralGain=fish.lateralGain??1,verticalGain=fish.verticalGain??1,twistGain=fish.twistGain??1;
  const history=(prefix,rate)=>fish.poseHistory===false||!Number.isFinite(fish[prefix+'1'])?
    axialDelays.map(delay=>-rate*delay):[0,fish[prefix+'1'],fish[prefix+'2'],fish[prefix+'3']];
  const yawHistory=history('yawLag',clamp(fish.yawRate??0,-1.3,1.3));
  const pitchHistory=history('pitchLag',clamp(fish.pitchRate??0,-.5,.5));
  const bankHistory=history('bankLag',clamp(fish.bankRate??0,-.5,.5));
  const turnHistory=fish.poseHistory===false?[0,0,0,0]:
    [fish.turnBend??0,fish.turnBend1??0,fish.turnBend2??0,fish.turnBend3??0];
  const recruitmentHistory=fish.poseHistory===false?[0,0,0,0]:
    [fish.turnRecruitment??0,fish.turnRecruitment1??0,fish.turnRecruitment2??0,fish.turnRecruitment3??0];
  // Authored studies have no elapsed history: show a steady bend at the given
  // angular rate and a full travelling stroke, rather than borrowing live state.
  const muscleHistory=fish.poseHistory===false||!Number.isFinite(fish.muscle1)?
    [stroke,stroke,stroke,stroke]:[stroke,fish.muscle1,fish.muscle2,fish.muscle3];
  return axial.map((x,i)=>{
    if(i<=4)return {lateral:0,vertical:0,twist:0};
    const s=clamp((x-axial[5])/length,0,1),phase=fish.phase-4.8*s;
    const muscle=alongBody(muscleHistory,s);
    // A body-driven turn replaces the independent cruise wave where it loads.
    // Full recruitment at a quarter of maximum bend is an authored mode
    // policy, not a koi measurement; tiny fin-trim corrections recruit little.
    const carrier=turnCarrierShare(alongBody(recruitmentHistory,s));
    const amplitude=(.09+drive*.10)*muscle*carrier;
    const lateral=(Math.atan(amplitude/length*(2*s*Math.sin(phase)-4.8*s*s*Math.cos(phase)))
      +alongBody(yawHistory,s)-alongBody(turnHistory,s)*s*s*(3-2*s))*lateralGain;
    // Carp propulsion remains predominantly lateral. Vertical flexion follows
    // pitch manoeuvres; the small secondary wave is compliance, not a dolphin kick.
    const vertical=(-alongBody(pitchHistory,s)+.018*muscle*s*s*Math.sin(phase+.7))*verticalGain;
    // A banking head and lagging tail put a small torsion into the trunk.
    const torsion=(alongBody(bankHistory,s)+(fish.torsionDrive??0)*s
      +.022*muscle*s*s*Math.sin(phase-1))*twistGain;
    return {lateral,vertical,twist:torsion};
  });
}
