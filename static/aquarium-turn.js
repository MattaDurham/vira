/* Authored finite turn control. This is a kinematic fluid-response closure,
 * not CFD or measured koi muscle/pressure coefficients. Radians and seconds. */
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const ease=x=>clamp(x*x*x*(10+x*(-15+6*x)),0,1);
const easeD=x=>30*x*x*(1-x)*(1-x);
const easeDD=x=>60*x*(1-x)*(1-2*x);
const positive=value=>{if(value<=0)return 0;if(value>=.5)return value;const t=value/.5;return .5*t*t*t*(6+t*(-8+3*t));};
const handoffDuration=.42;
const delay=[0,.055,.11],release=[.39,.54,.69],weights=[.25,.45,.30];
const blank=()=>({b:[0,0,0],v:[0,0,0],a:[0,0,0]});
const regionalLoad=(b,v)=>v*ease(clamp(b*v/(.18*Math.hypot(v,.20)),0,1));
function eventShape(event,age){
  const q=blank();if(!event)return q;
  for(let i=0;i<3;i++){
    const t=(age-event.start-event.preparation)/event.duration,local=t-delay[i];
    if(local>0&&local<.30){const x=local/.30,s=1/(.30*event.duration);q.b[i]=event.amplitude*ease(x);q.v[i]=event.amplitude*easeD(x)*s;q.a[i]=event.amplitude*easeDD(x)*s*s;}
    else if(local>=.30&&t<release[i])q.b[i]=event.amplitude;
    else if(t>=release[i]&&t<release[i]+.45){const x=(t-release[i])/.45,s=1/(.45*event.duration);q.b[i]=event.amplitude*(1-ease(x));q.v[i]=-event.amplitude*easeD(x)*s;q.a[i]=-event.amplitude*easeDD(x)*s*s;}
  }
  return q;
}
function composedShape(current,outgoing,handoff,age){
  const q=eventShape(current,age);if(!outgoing)return q;
  const p=eventShape(outgoing,age),t=clamp((age-handoff)/handoffDuration,0,1),f=ease(t),d=easeD(t)/handoffDuration,dd=easeDD(t)/(handoffDuration*handoffDuration);
  for(let i=0;i<3;i++){
    const b=q.b[i],v=q.v[i],a=q.a[i];
    q.b[i]=p.b[i]*(1-f)+b*f;q.v[i]=p.v[i]*(1-f)+v*f+d*(b-p.b[i]);q.a[i]=p.a[i]*(1-f)+a*f+2*d*(v-p.v[i])+dd*(b-p.b[i]);
  }
  return q;
}
const eventEnd=e=>e?e.start+e.preparation+1.14*e.duration:-Infinity;
function finPlan(event,age){
  if(!event?.preparation)return 1;
  const t=age-event.start;
  // Clear the recorded .24s distal response after a .12s C2 pre-tuck.
  // The issued body load begins only after this explicit .36s preparation.
  if(t<.12)return 1-ease(clamp(t/.12,0,1));
  if(age<eventEnd(event))return 0;
  return ease(clamp((age-eventEnd(event))/.24,0,1));
}
function plannedActivity(event,age,region){
  if(!event)return 0;
  const start=event.preparation?0:Math.max(0,delay[region]*event.duration-.08);
  const t=age-event.start,loaded=ease(clamp((t-start)/.16,0,1));
  const returned=event.preparation+(release[region]+.45)*event.duration;
  const recovery=1-ease(clamp((t-returned)/.40,0,1));
  // An issued finite stroke retains recruitment through regional straightening,
  // then releases the cruise carrier over a finite C2 recovery. Cancellation
  // between opposing C shapes cannot reveal a large unrelated S beat.
  // The shared carrier blend treats R=.25 as complete replacement. Reach
  // that only for a clearly body-driven C (A>=.9rad); a small steering trim
  // must retain its ordinary propulsive wave rather than borrow its power.
  return .25*ease(clamp(event.amplitude*event.amplitude/.81,0,1))*loaded*recovery;
}
function composedActivity(current,outgoing,handoff,age){
  const mix=outgoing?ease(clamp((age-handoff)/handoffDuration,0,1)):1;
  return [0,1,2].map(i=>plannedActivity(current,age,i)*mix+plannedActivity(outgoing,age,i)*(1-mix));
}
// request(angle, age, {urgent}) latches an intended heading budget; it selects
// a finite shape, never an instantaneous desired heading rate. sample(age)
// returns reused scalar/array data: callers retaining history must copy it.
// peek(age) predicts that issued trajectory without advancing or mutating it.
export function createTurnController(){
  let current=null,outgoing=null,queued=null,handoff=0,serial=0,lastAge;
  const out=Object.assign(blank(),{load:[0,0,0],activity:[0,0,0]});
  const end=eventEnd;
  function descriptor(angle,urgent=false){
    const budget=clamp(angle,-Math.PI,Math.PI),magnitude=Math.abs(budget);
    const onset=ease(clamp((magnitude-.16)/.60,0,1));
    const amplitude=Math.min(urgent?2.4:1.8,(urgent?.40:.30)+(urgent?.64:.48)*magnitude)*onset;
    return {amplitude:Math.sign(budget)*amplitude,budget,preparation:urgent?.36:0,duration:urgent?.72:1.08};
  }
  function advance(age,inclusive=true){
    while(outgoing&&(inclusive?age>=handoff+handoffDuration:age>handoff+handoffDuration+1e-10)){
      outgoing=null;
      if(queued){const next=queued;queued=null;outgoing=current;current={...next,start:handoff+handoffDuration,serial:++serial};handoff+=handoffDuration;}
    }
  }
  function request(angle,age,{urgent=false}={}){
    if(!Number.isFinite(age)||!Number.isFinite(angle))return false;
    if(lastAge!==undefined&&age<lastAge)reset();
    // Earlier deadlines are already issued history. Reconcile new intent
    // before an exactly-current deadline, never retroactively before a past one.
    advance(age,false);
    if(Math.abs(angle)<.16){queued=null;advance(age);return false;}
    const next=descriptor(angle,urgent);
    if(outgoing)queued=Math.sign(current.amplitude)===Math.sign(next.amplitude)?null:next;
    advance(age);
    if(outgoing){queued=Math.sign(current.amplitude)===Math.sign(next.amplitude)?null:next;return false;}
    if(current&&age<end(current)){
      if(Math.sign(current.amplitude)===Math.sign(next.amplitude))return false;
      outgoing=current;handoff=age;
    }else if(current&&age<end(current)+.65){outgoing=current;handoff=age;}
    current={...next,start:age,serial:++serial};return true;
  }
  function reset(){current=outgoing=queued=null;handoff=serial=0;lastAge=undefined;for(const key of ['b','v','a','load','activity'])out[key].fill(0);Object.assign(out,{serial:0,age:0,active:false,settling:false,settleWeight:0,finClearance:1,preparing:false,handoffMix:1,outgoingBudget:0,outgoingSerial:0,outgoingSettleWeight:0,budget:0,recovery:0,intensity:0,energy:0,power:0});return out;}
  reset();
  return {request,reset,ready:age=>!outgoing&&age>=end(current)+.16,
    peek(age){
      let c=current,o=outgoing,q=queued,h=handoff;
      while(o&&age>=h+handoffDuration){o=null;if(q){const next=q;q=null;o=c;c={...next,start:h+handoffDuration};h+=handoffDuration;}}
      const pose=composedShape(c,o,h,age);pose.activity=composedActivity(c,o,h,age);return pose;
    },
    sample(age){
      if(!Number.isFinite(age))return out;
      if(lastAge!==undefined&&age<lastAge)reset();
      lastAge=age;advance(age);const q=composedShape(current,outgoing,handoff,age),activity=composedActivity(current,outgoing,handoff,age);
      let recovery=0,intensity=0,energy=0,speedEnergy=0;
      for(let i=0;i<3;i++){
        out.b[i]=q.b[i];out.v[i]=q.v[i];out.a[i]=q.a[i];
        // Only commanded motion increasing a region's bend can load it. A
        // smooth product gate avoids a force kink at load/recovery reversal.
        const product=q.b[i]*q.v[i],scale=.18*Math.hypot(q.v[i],.20);
        out.load[i]=regionalLoad(q.b[i],q.v[i]);
        out.activity[i]=activity[i];
        recovery+=weights[i]*ease(clamp(-product/scale,0,1));intensity=Math.max(intensity,Math.abs(q.b[i]));energy+=weights[i]*q.b[i]*q.b[i];speedEnergy+=weights[i]*q.v[i]*q.v[i];
      }
      const mix=outgoing?ease(clamp((age-handoff)/handoffDuration,0,1)):1;
      Object.assign(out,{finClearance:finPlan(current,age)*mix+finPlan(outgoing,age)*(1-mix),
        preparing:!!current&&age<current.start+current.preparation,outgoingBudget:outgoing?.budget??0,outgoingSerial:outgoing?.serial??0,
        outgoingSettleWeight:outgoing?1-ease(clamp((age-end(outgoing))/.65,0,1)):0,
        handoffMix:outgoing?ease(clamp((age-handoff)/handoffDuration,0,1)):1,serial,age:current?age-current.start:0,active:!!outgoing||age<end(current),settling:!!current&&age<end(current)+.65,settleWeight:current?1-ease(clamp((age-end(current))/.65,0,1)):0,budget:current?.budget??0,recovery,intensity,energy,power:ease(clamp(speedEnergy/4,0,1))});return out;
    }};
}
export function regionalTurnAngle(s,b){
  const t=clamp(s,0,1),square=t*t,cube=square*t;
  return -(b[0]*(t-square+cube/3)+b[1]*(square-2*cube/3)+b[2]*cube/3);
}
export function turnResponse(gesture,yawRate,dt,{finMoment=0,finDamping=0,allowed=1,headingProgress,outgoingHeadingProgress}={}){
  // The same issued regional velocities used by the rig supply a bounded
  // authored angular impulse. Root motion and passive yaw lag cannot energize
  // this term. Recovery only removes actual angular momentum.
  const energy=weights.reduce((sum,w,i)=>sum+w*gesture.b[i]*gesture.b[i],0);
  const engagement=ease(clamp(energy/.36,0,1));
  const load=weights.reduce((sum,w,i)=>sum+w*regionalLoad(gesture.b[i],gesture.v[i]),0);
  const moment=3.6*load*engagement*clamp(allowed,0,1);
  let damping=1.25+3.0*gesture.recovery+Math.max(0,finDamping),budgetBraking=0;
  const stop=(budget,progress,weight)=>{
    if(!weight||!Number.isFinite(progress))return 0;
    const sign=Math.sign(budget),remaining=Math.abs(budget)-progress*sign;
    // This stopping surface only opposes actual rotation. It cannot seek a
    // target heading or create a turn from rest. No endpoint angle is clamped.
    const stopping=Math.max(0,yawRate*sign)/(.08+positive(remaining-.08));
    return 20*Math.tanh(positive(stopping-damping)/20)*weight;
  };
  // Budget surfaces follow the SAME C2 handoff as their regional gesture;
  // reversing intent cannot instantly remove an already deployed brake.
  const mix=gesture.handoffMix??1;
  budgetBraking=stop(gesture.budget,headingProgress,gesture.settleWeight??0)*mix
    +stop(gesture.outgoingBudget,outgoingHeadingProgress,gesture.outgoingSettleWeight??0)*(1-mix);
  damping+=budgetBraking;
  const permittedFinMoment=finMoment*clamp(allowed,0,1);
  const rate=(yawRate+(moment+permittedFinMoment)*dt)/(1+damping*dt);
  return {yawRate:rate,moment,finMoment:permittedFinMoment,recoveryMoment:-(3.0*gesture.recovery+budgetBraking)*rate,damping,budgetBraking};
}
