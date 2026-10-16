/* SOURCE-INFORMED AUTHORED steering, not a force model.
 * This controller never uses or returns source COM/path channels; the shared
 * observed-cue helper also constructs unused reference interpolants. */
import {createObservedTurnCue} from './aquarium-reference-turn-cue.js?v=90';
const profileClamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const profileJet=()=>({p:0,v:0,a:0});
const profilePolynomial=(first,last,T)=>{
 const c0=first.p,c1=T*first.v,c2=.5*T*T*first.a,P=last.p-c0-c1-c2,V=T*last.v-c1-2*c2,A=T*T*last.a-2*c2;
 return [c0,c1,c2,10*P-4*V+.5*A,-15*P+7*V-A,6*P-3*V+.5*A];
};
const profileEvaluate=(c,t,T,out)=>{
 const s=profileClamp(t/T,0,1);out.p=c[0]+s*(c[1]+s*(c[2]+s*(c[3]+s*(c[4]+s*c[5]))));
 out.v=(c[1]+s*(2*c[2]+s*(3*c[3]+s*(4*c[4]+s*5*c[5]))))/T;
 out.a=(2*c[2]+s*(6*c[3]+s*(12*c[4]+s*20*c[5])))/(T*T);return out;
};
function profileScalarSignal(frames,read){
 const times=frames.map(f=>f.time),p=frames.map(read),state=p.map((x,i)=>{
  const lo=Math.max(0,i-1),hi=Math.min(p.length-1,i+1),a=i===0?0:i===p.length-1?i-2:i-1,b=a+1,c=a+2,h1=times[b]-times[a],h2=times[c]-times[b];
  return {p:x,v:(p[hi]-p[lo])/(times[hi]-times[lo]),a:2*((p[c]-p[b])/h2-(p[b]-p[a])/h1)/(h1+h2)};
 }),coefficients=state.slice(0,-1).map((s,i)=>profilePolynomial(s,state[i+1],times[i+1]-times[i]));
 return (t,out=profileJet())=>{let lo=0,hi=times.length-1;while(hi-lo>1){const m=(lo+hi)>>1;if(times[m]<=t)lo=m;else hi=m;}return profileEvaluate(coefficients[lo],t-times[lo],times[hi]-times[lo],out);};
}
export function createSourceTurnProfile(data,{angle=Math.PI,duration=1.6,start=0,heading=0}={}){
 const source=data?.trials?.fast,frames=source?.frames;
 if(!frames||frames.length!==69||![angle,duration,start,heading].every(Number.isFinite)||duration<=0||Math.abs(angle)>Math.PI+1e-12)throw new Error('Finite bounded heading/duration and69sample fastsource required');
 const first=frames[0].time,last=frames.at(-1).time,span=last-first,scale=span/duration,H0=frames[0].headReference,A=frames.at(-1).headReference-H0;
 const headSignal=profileScalarSignal(frames,f=>(f.headReference-H0)/A),onset={...headSignal(0)},end={...headSignal(source.turnDuration)};
 const initial={p:0,v:0,a:0},final={p:1,v:0,a:0},entry=profilePolynomial(initial,onset,-first),exit=profilePolynomial(end,final,last-source.turnDuration);
 const cue=createObservedTurnCue(data,{trial:'fast',duration,start}),scratch=profileJet(),out={b:[0,0,0],v:[0,0,0],a:[0,0,0],heading,headRate:0,headAcceleration:0,progress:0,sourceTime:first};
 const factor=angle/A;
 function sample(age,target=out){
  if(!Number.isFinite(age))throw new Error('Finite age required');
  const sourceTime=first+(age-start)*scale;
  if(age<=start)Object.assign(scratch,initial);
  else if(age>=start+duration)Object.assign(scratch,final);
  else if(sourceTime<0)profileEvaluate(entry,sourceTime-first,-first,scratch);
  else if(sourceTime>source.turnDuration)profileEvaluate(exit,sourceTime-source.turnDuration,last-source.turnDuration,scratch);
  else headSignal(sourceTime,scratch);
  const q=cue.sample(age);for(const key of ['b','v','a'])for(let i=0;i<3;i++)target[key][i]=factor*q[key][i];
  target.heading=heading+angle*scratch.p;target.headRate=angle*scale*scratch.v;target.headAcceleration=angle*scale*scale*scratch.a;
  target.progress=scratch.p;target.sourceTime=profileClamp(sourceTime,first,last);return target;
 }
 return {sample,createPacket:()=>({b:[0,0,0],v:[0,0,0],a:[0,0,0],heading:0,headRate:0,headAcceleration:0,progress:0,sourceTime:first}),
  metadata:Object.freeze({label:'Authored source-informed head/relative-body steering; no observed COM/path',sourceID:source.ID,species:source.species,sourceHeadingRadians:A,angle,duration,start,end:start+duration,markedOnset:start-first/scale,markedEnd:start+(source.turnDuration-first)/scale,
   shapeScale:factor,sourceTimeRange:[first,last],headBoundary:'Authored C2 quints on retained pre/post margins; source core headknots kept; heading endpoint normalized to requested budget',shapeBoundary:cue.metadata.boundary,
   timing:'Explicit authored duration; source13cm bluegill timings are not calibrated koi dynamics'})};
}

/* Finite finite source-informed AUTHORED animation control.
 * Heading and relative body shape share one committed clock. No observed COM,
 * force coefficients, root path, or independent yaw moment is used here. */
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x)),wrap=x=>Math.atan2(Math.sin(x),Math.cos(x));
const ease=x=>x*x*x*(10+x*(-15+6*x)),easeD=x=>30*x*x*(1-x)*(1-x),easeDD=x=>60*x*(1-x)*(1-2*x);
const jet=(p=0,v=0,a=0)=>({p,v,a});
function smooth(time,start,width){const t=(time-start)/width;if(t<=0)return jet();if(t>=1)return jet(1);return jet(ease(t),easeD(t)/width,easeDD(t)/(width*width));}
const times=(a,b)=>jet(a.p*b.p,a.v*b.p+a.p*b.v,a.a*b.p+2*a.v*b.v+a.p*b.a);
const scaled=(a,s)=>jet(a.p*s,a.v*s,a.a*s);
const inverse=a=>jet(1-a.p,-a.v,-a.a);
const mix=(a,b,f)=>jet(a.p*(1-f.p)+b.p*f.p,a.v*(1-f.p)+b.v*f.p+f.v*(b.p-a.p),a.a*(1-f.p)+b.a*f.p+2*f.v*(b.v-a.v)+f.a*(b.p-a.p));
function coefficients(a,b,T){const c0=a.p,c1=T*a.v,c2=.5*T*T*a.a,P=b.p-c0-c1-c2,V=T*b.v-c1-2*c2,A=T*T*b.a-2*c2;return[c0,c1,c2,10*P-4*V+.5*A,-15*P+7*V-A,6*P-3*V+.5*A];}
function polynomial(c,t,T){const s=clamp(t/T,0,1);return jet(c[0]+s*(c[1]+s*(c[2]+s*(c[3]+s*(c[4]+s*c[5])))),(c[1]+s*(2*c[2]+s*(3*c[3]+s*(4*c[4]+s*5*c[5]))))/T,(2*c[2]+s*(6*c[3]+s*(12*c[4]+s*20*c[5])))/(T*T));}
function phaseStep(rate,velocity,target,dt,k){const d=rate-target,c=velocity+k*d,e=Math.exp(-k*dt);return{distance:target*dt+d*(1-e)/k+c*(1-(1+k*dt)*e)/(k*k),rate:target+(d+c*dt)*e,velocity:(velocity-k*c*dt)*e};}
const weights=[.25,.45,.30];
const copyState=s=>({...s,queued:s.queued?{...s.queued}:null});
const emptyPacket=()=>({b:[0,0,0],v:[0,0,0],a:[0,0,0],activity:[0,0,0],load:[0,0,0]});

/**
 * request(angle, age, {headYaw,headRate,headAcceleration,urgent,duration}) only
 * latches intent. sample() returns committed output. propose(dt,{authority})
 * and peek(seconds,{authority}) are pure; commit(proposal) advances exactly once.
 * All heading values are unwrapped radians. `headRate` is analytic endpoint
 * rate; `stepMeanHeadRate` is exact headDelta/dt for legacy Euler application.
 */
export function createSourceSteeringController(data,{duration=1.6,urgentDuration=1.25,handoffDuration=.42,authorityFrequency=24,threshold=.16}={}){
 const source=data?.trials?.fast,frames=source?.frames;
 if(!frames||frames.length!==69||![duration,urgentDuration,handoffDuration,authorityFrequency,threshold].every(Number.isFinite)||Math.min(duration,urgentDuration,handoffDuration,authorityFrequency)<=0||threshold<0)throw new Error('Invalid source steering configuration');
 const headingSpan=frames.at(-1).headReference-frames[0].headReference,maximumCoefficient=Math.max(...frames.flatMap(f=>f.b.map(Math.abs)));
 const maximumRelativeAngle=Math.max(...frames.map(f=>Math.abs(f.b.reduce((a,b)=>a+b,0)/3)));
 let state,revision=0;const proposals=new WeakMap(),out=emptyPacket();
 function base(trajectory,clock){
  const q=trajectory.profile.sample(clock,trajectory.profile.createPacket());return[jet(q.heading,q.headRate,q.headAcceleration),...q.b.map((b,i)=>jet(b,q.v[i],q.a[i]))];
 }
 function field(trajectory,clock){
  if(!trajectory)return[jet(state.heading),jet(),jet(),jet()];
  if(trajectory.bridge&&clock<trajectory.start+handoffDuration)return trajectory.bridge.map(c=>polynomial(c,clock-trajectory.start,handoffDuration));
  return base(trajectory,clock);
 }
 function auxiliary(trajectory,clock){
  if(!trajectory)return{replacement:jet(),activity:[jet(),jet(),jet()],fin:jet(1)};
  const t=clock-trajectory.start,end=trajectory.end-trajectory.start;
  const envelope=times(smooth(t,0,.16),inverse(smooth(t,end,.40)));
  const strength=ease(clamp(trajectory.relativeAngle*trajectory.relativeAngle/.81,0,1));
  const own={replacement:envelope,activity:[0,1,2].map(()=>scaled(envelope,.25*strength)),fin:trajectory.preparation?(t<end?inverse(smooth(t,0,.12)):smooth(t,end,.24)):jet(1)};
  if(trajectory.auxOutgoing&&clock<trajectory.start+handoffDuration){
   const old=auxiliary(trajectory.auxOutgoing,clock),f=smooth(clock,trajectory.start,handoffDuration);
   own.replacement=mix(old.replacement,own.replacement,f);own.fin=mix(old.fin,own.fin,f);own.activity=own.activity.map((a,i)=>mix(old.activity[i],a,f));
  }
  return own;
 }
 function makeTrajectory(descriptor,clock,from,previous,s){
  const requested=wrap(descriptor.target-from[0].p),angle=Math.abs(requested)<threshold?0:requested;
  const T=descriptor.duration??(descriptor.urgent?urgentDuration:duration);
  const preparation=descriptor.urgent||Math.abs(angle/headingSpan)*maximumCoefficient>1.6?.36:0;
  const profile=createSourceTurnProfile(data,{angle,duration:T,start:clock+preparation,heading:from[0].p});
  const trajectory={profile,start:clock,end:clock+preparation+T,preparation,budget:angle,relativeAngle:Math.abs(angle/headingSpan)*maximumRelativeAngle,serial:++s.serial,bridge:null,auxOutgoing:previous&&clock<previous.end+.4?previous:null};
  const atStart=base(trajectory,clock),different=from.some((j,i)=>Math.abs(j.p-atStart[i].p)>1e-12||Math.abs(j.v-atStart[i].v)>1e-12||Math.abs(j.a-atStart[i].a)>1e-10);
  if(different){const to=base(trajectory,clock+handoffDuration);trajectory.bridge=from.map((a,i)=>coefficients(a,to[i],handoffDuration));}
  return trajectory;
 }
 function advanceTrajectories(s,clock){
  while(s.trajectory?.auxOutgoing&&clock>s.trajectory.start+handoffDuration+1e-10){
   const old=s.trajectory;
   s.trajectory={...old,auxOutgoing:null};
   if(s.queued){const descriptor=s.queued;s.queued=null;const at=old.start+handoffDuration,from=field(s.trajectory,at);s.trajectory=makeTrajectory(descriptor,at,from,s.trajectory,s);}
  }
 }
 function evaluate(s,target=emptyPacket()){
  const q=s.trajectory?field(s.trajectory,s.clock):[jet(s.heading),jet(),jet(),jet()],aux=auxiliary(s.trajectory,s.clock),f=s.clockRate,fd=s.clockVelocity;
  target.headHeading=q[0].p;target.heading=q[0].p;target.headRate=q[0].v*f;target.headAcceleration=q[0].a*f*f+q[0].v*fd;
  let energy=0,speedEnergy=0,recovery=0,intensity=0;
  for(let i=0;i<3;i++){
   target.b[i]=q[i+1].p;target.v[i]=q[i+1].v*f;target.a[i]=q[i+1].a*f*f+q[i+1].v*fd;target.activity[i]=aux.activity[i].p;
   const product=target.b[i]*target.v[i],den=.18*Math.hypot(target.v[i],.2),positive=ease(clamp(product/den,0,1));target.load[i]=target.v[i]*positive;
   recovery+=weights[i]*ease(clamp(-product/den,0,1));energy+=weights[i]*target.b[i]**2;speedEnergy+=weights[i]*target.v[i]**2;intensity=Math.max(intensity,Math.abs(target.b[i]));
  }
  const tr=s.trajectory,active=!!tr&&s.clock<tr.end+.4,settling=!!tr&&s.clock<tr.end+.4;
  Object.assign(target,{serial:tr?.serial??0,age:tr?s.clock-tr.start:0,active,bodyActive:!!tr&&s.clock<tr.end,settling,preparing:!!tr&&s.clock<tr.start+tr.preparation,budget:tr?.budget??0,recovery,intensity,energy,power:ease(clamp(speedEnergy/4,0,1)),
   finClearance:aux.fin.p,sourceReplacementWeight:aux.replacement.p,settleWeight:tr?1-smooth(s.clock,tr.end,.4).p:0,
   handoffMix:tr?.auxOutgoing?smooth(s.clock,tr.start,handoffDuration).p:1,outgoingBudget:tr?.auxOutgoing?.budget??0,outgoingSerial:tr?.auxOutgoing?.serial??0,outgoingSettleWeight:0,
   phaseClock:s.clock,phaseRate:f,phaseAcceleration:fd,physicalAge:s.age});return target;
 }
 function reset({headYaw=0,headRate=0,headAcceleration=0,age=0}={}){
  if(![headYaw,headRate,headAcceleration,age].every(Number.isFinite))throw new Error('Finite source steering reset required');
  state={age,clock:0,clockRate:1,clockVelocity:0,heading:headYaw,idleHeadRate:headRate,idleHeadAcceleration:headAcceleration,trajectory:null,queued:null,serial:0};revision++;return evaluate(state,out);
 }
 function request(angle,age,{headYaw,headRate,headAcceleration,urgent=false,duration:requestedDuration}={}){
  if(![angle,age].every(Number.isFinite)||requestedDuration!==undefined&&(!Number.isFinite(requestedDuration)||requestedDuration<=0))return false;
  if(Math.abs(angle)<threshold){state.queued=null;revision++;return false;}
  const current=evaluate(state,emptyPacket()),actual=Number.isFinite(headYaw)?current.headHeading+wrap(headYaw-current.headHeading):current.headHeading;
  const descriptor={target:actual+clamp(angle,-Math.PI,Math.PI),urgent:!!urgent,duration:requestedDuration};
  const tr=state.trajectory;
  if(tr&&state.clock<tr.end){
   if(Math.sign(tr.budget)===Math.sign(angle)){state.queued=null;revision++;return false;}
   if(tr.auxOutgoing&&state.clock<tr.start+handoffDuration){state.queued=descriptor;revision++;return false;}
  }else if(tr&&state.clock<tr.end+.56){state.queued=descriptor;revision++;return false;}
  const wasActive=!!tr&&state.clock<tr.end+.4;
  const from=wasActive?field(tr,state.clock):[jet(actual,Number.isFinite(headRate)?headRate:state.idleHeadRate,Number.isFinite(headAcceleration)?headAcceleration:state.idleHeadAcceleration),jet(),jet(),jet()];
  from[0].p=actual;
  // An active curve already supplies phase-coordinate jets. Retaining its
  // clockrate/clockacceleration preserves the PHYSICAL jets exactly under
  // authority<1, with no ill-conditioned division by a stopped clock. Idle
  // finite-fin motion instead seeds physical jets with a fresh unit-rate clock.
  if(!wasActive){state.clockRate=1;state.clockVelocity=0;}
  state.trajectory=makeTrajectory(descriptor,state.clock,from,tr,state);state.heading=actual;state.queued=null;revision++;evaluate(state,out);return true;
 }
 function predict(dt,authority){
  if(!Number.isFinite(dt)||dt<0||!Number.isFinite(authority))throw new Error('Finite nonnegative proposal interval and authority required');
  const next=copyState(state),clock=phaseStep(state.clockRate,state.clockVelocity,clamp(authority,0,1),dt,authorityFrequency);
  next.age+=dt;next.clock+=clock.distance;next.clockRate=clock.rate;next.clockVelocity=clock.velocity;advanceTrajectories(next,next.clock);
  // A request held through the ordinary quiet recovery can start once ready.
  if(next.queued&&next.trajectory&&!next.trajectory.auxOutgoing&&next.clock>=next.trajectory.end+.56){const descriptor=next.queued;next.queued=null;const at=next.trajectory.end+.56,from=field(next.trajectory,at);next.trajectory=makeTrajectory(descriptor,at,from,next.trajectory,next);}
  const before=evaluate(state,emptyPacket()),gesture=evaluate(next,emptyPacket());next.heading=gesture.headHeading;
  return{next,gesture,headDelta:gesture.headHeading-before.headHeading};
 }
 reset();
 return {reset,request,sample:()=>evaluate(state,out),ready:()=>!state.queued&&(!state.trajectory||state.clock>=state.trajectory.end+.56),
  propose(dt,{authority=1}={}){const p=predict(dt,authority),result={dt,headDelta:p.headDelta,stepMeanHeadRate:dt?p.headDelta/dt:0,gesture:p.gesture};proposals.set(result,{revision,state:p.next});return result;},
  commit(proposal){const p=proposals.get(proposal);if(!p||p.revision!==revision)throw new Error('Only a fresh source steering proposal may be committed once');state=p.state;revision++;proposals.delete(proposal);return evaluate(state,out);},
  peek(seconds,{authority=1}={}){const p=predict(seconds,authority);p.gesture.headDelta=p.headDelta;return p.gesture;},
  metadata:Object.freeze({authored:true,physics:false,sourceID:source.ID,sourceHeadingRadians:headingSpan,sourceSpecies:source.species,duration,urgentDuration,handoffDuration,authorityFrequency,threshold,
   label:'Source-informed authored heading/relative-body steering; no observed COM/path',clock:'One committed physical tick; same analytically timewarped source clock for head and body'})};
}
