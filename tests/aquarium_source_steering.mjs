// Actual shipped source data and controller: finite authored steering, no
// prescribed COM/path or fluid validation. Joined material tests live separately.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {regionalTurnAngle} from '../static/aquarium-turn.js';
import {createSourceTurnProfile,createSourceSteeringController} from '../static/aquarium-source-steering.js';
const data=JSON.parse(await readFile(new URL('../static/aquarium/reference-turn.json',import.meta.url),'utf8'));
function profileChecks(){
const copy=q=>JSON.parse(JSON.stringify(q)),profiles=[];
let knotError=0,mirrorError=0,maxVelocityFD=0,maxAccelerationFD=0;
for(const angle of [Math.PI/6,Math.PI/2,Math.PI]){
 const p=createSourceTurnProfile(data,{angle,duration:1.6}),n=createSourceTurnProfile(data,{angle:-angle,duration:1.6}),M=p.metadata;
 const shapeMax={angle:0,head:0,time:0},head90={head:0,tail:0,time:0};let maxHeadRate=0;
 for(let i=0;i<=1920;i++){
  const t=i/1200,q=copy(p.sample(t)),r=copy(n.sample(t)),theta=regionalTurnAngle(1,q.b);
  for(const key of ['b','v','a'])for(let j=0;j<3;j++)mirrorError=Math.max(mirrorError,Math.abs(q[key][j]+r[key][j]));
  mirrorError=Math.max(mirrorError,Math.abs(q.heading+r.heading),Math.abs(q.headRate+r.headRate),Math.abs(q.headAcceleration+r.headAcceleration));
  if(Math.abs(theta)>shapeMax.angle)Object.assign(shapeMax,{angle:Math.abs(theta),head:q.heading,time:t});
  if(!head90.time&&q.heading>=angle/2)Object.assign(head90,{head:q.heading,tail:q.heading+theta,time:t});
  maxHeadRate=Math.max(maxHeadRate,Math.abs(q.headRate));
 }
 const at0=copy(p.sample(0)),atEnd=copy(p.sample(1.6));assert.equal(at0.heading,0);assert.equal(atEnd.heading,angle);
 for(const q of [at0,atEnd]){assert.equal(q.headRate,0);assert.equal(q.headAcceleration,0);for(const k of ['b','v','a'])assert.ok(q[k].every(x=>x===0));}
 for(const f of data.trials.fast.frames.filter(f=>f.time>=0&&f.time<=data.trials.fast.turnDuration)){
  const t=(f.time-M.sourceTimeRange[0])/(M.sourceTimeRange[1]-M.sourceTimeRange[0])*M.duration,q=p.sample(t),expected=angle*(f.headReference-data.trials.fast.frames[0].headReference)/M.sourceHeadingRadians;
  knotError=Math.max(knotError,Math.abs(q.heading-expected));
 }
 for(const t of [.1,.253,.49,.703,1.01,1.27,1.52]){
  const h=1e-6,a=copy(p.sample(t-h)),b=copy(p.sample(t)),c=copy(p.sample(t+h));
  maxVelocityFD=Math.max(maxVelocityFD,Math.abs((c.heading-a.heading)/(2*h)-b.headRate));maxAccelerationFD=Math.max(maxAccelerationFD,Math.abs((c.headRate-a.headRate)/(2*h)-b.headAcceleration));
 }
 profiles.push({requestedDegrees:angle*180/Math.PI,shapePeak:{degrees:shapeMax.angle*180/Math.PI,headDegrees:shapeMax.head*180/Math.PI,time:shapeMax.time},headHalfTurn:{headDegrees:head90.head*180/Math.PI,tailDegrees:head90.tail*180/Math.PI,time:head90.time},maximumHeadRateRadiansPerSecond:maxHeadRate});
}
assert.ok(knotError<1e-12&&mirrorError<1e-12&&maxVelocityFD<1e-5&&maxAccelerationFD<.001);
// Source modeled-COM and posterior absolute reference are not steering inputs.
const changed=structuredClone(data);for(const f of changed.trials.fast.frames){f.authorCOMOverL=[999,-999];f.tailReference=999;}
const original=createSourceTurnProfile(data),mutated=createSourceTurnProfile(changed);for(let i=0;i<200;i++)assert.deepEqual(original.sample(i/120),mutated.sample(i/120),'head+regionalcue never consumes observedCOM/path or independent posteriorworldheading');
const report={profiles,knotError,mirrorError,maxVelocityFD,maxAccelerationFD,notes:['Single source fasttrial is a crossspecies authored template, not mechanics or genericfishlaw','Sourcehead progress and relativebodyshareclock; fixedstageC2sourceentry/exit are authored','No observedCOM, translation or rootpath is present']};
return report;
}
function controllerChecks(){
const copy=x=>structuredClone(x),approximately=(a,b,e=1e-10)=>assert.ok(Math.abs(a-b)<=e,`${a} versus ${b}`),fields=['b','v','a','activity'];
const factory=()=>createSourceSteeringController(data);
const a=factory(),b=factory();a.request(Math.PI,0);b.request(-Math.PI,0);let maxMirror=0,maxClockBound=0;
for(let i=0;i<600;i++){
 const authority=i<120?1:i<220?0:i<330?.35:1,pa=a.propose(1/120,{authority}),pb=b.propose(1/120,{authority});
 const before=copy(a.sample()),peek=a.peek(1/120,{authority});assert.deepEqual(a.sample(),before,'prediction never advances committed output');
 for(const key of ['heading','headRate','headAcceleration'])maxMirror=Math.max(maxMirror,Math.abs(pa.gesture[key]+pb.gesture[key]));
 for(const key of ['b','v','a'])for(let j=0;j<3;j++)maxMirror=Math.max(maxMirror,Math.abs(pa.gesture[key][j]+pb.gesture[key][j]));
 approximately(peek.headDelta,pa.headDelta);for(const key of fields)assert.deepEqual(peek[key],pa.gesture[key]);
 approximately(pa.stepMeanHeadRate/120,pa.headDelta);assert.ok(pa.gesture.phaseRate>=-1e-12&&pa.gesture.phaseRate<=1+1e-12);maxClockBound=Math.max(maxClockBound,Math.abs(pa.gesture.phaseRate));
 const committed=copy(a.commit(pa));b.commit(pb);assert.deepEqual(committed,pa.gesture);assert.throws(()=>a.commit(pa),/fresh/,'sameproposal cannotadvance twice');
}
assert.ok(maxMirror<1e-12);approximately(a.sample().heading,Math.PI);approximately(b.sample().heading,-Math.PI);
assert.equal(a.sample().active,false);assert.equal(a.sample().sourceReplacementWeight,0);assert.equal(a.sample().finClearance,1);
// The same analytic authority clock must be independent of physical tick cadence.
function route(hz){const c=factory();c.request(Math.PI,0);for(let i=0;i<5*hz;i++){const t=i/hz,authority=t<1?1:t<2?0:t<3?.35:1;c.commit(c.propose(1/hz,{authority}));}return copy(c.sample());}
const r120=route(120),r240=route(240);for(const key of ['phaseClock','phaseRate','heading','headRate','headAcceleration'])approximately(r120[key],r240[key],3e-12);for(const key of fields)for(let j=0;j<3;j++)approximately(r120[key][j],r240[key][j],3e-12);
// Reversals capture the complete issuedjet; no pose/rate/acceleration restart.
const c=factory();c.request(2.5,0);for(let i=0;i<108;i++)c.commit(c.propose(1/120));
const before=copy(c.sample());assert.ok(c.request(-2.5,.9,{headYaw:before.heading,headRate:before.headRate,headAcceleration:before.headAcceleration}));const after=copy(c.sample());
for(const key of ['heading','headRate','headAcceleration'])approximately(before[key],after[key],2e-10);for(const key of ['b','v','a'])for(let j=0;j<3;j++)approximately(before[key][j],after[key][j],2e-10);
const past=copy(c.peek(.001));for(let i=0;i<100;i++)c.peek(1.5,{authority:i%2});assert.deepEqual(c.peek(.001),past,'long futures/queuedhandoffs do not mutate current plan');
// Latest release at a handoff boundary cancels queued obsolete work before it
// becomes issued history; already-past deadlines remain actual history.
const q=factory();q.request(2,0);for(let i=0;i<24;i++)q.commit(q.propose(1/120));q.request(-2,.2);
for(let i=0;i<6;i++)q.commit(q.propose(1/120));q.request(2,.25);
for(let i=0;i<44;i++)q.commit(q.propose(1/120)); // t.616666, just before .62phasehandoff
q.request(-2,.625,{headYaw:q.sample().heading});q.commit(q.propose(1/120));assert.equal(q.sample().serial,2,'newest same direction cancels obsolete queued opposite');
// Planned fin tuck precedes a large gesture by enough history travel; both
// head and body stay neutral during preparation when starting fromrest.
const prep=factory();prep.request(Math.PI,0,{urgent:true});for(let i=0;i<43;i++){const p=prep.propose(1/120);prep.commit(p);assert.ok(p.gesture.b.every(x=>x===0));assert.equal(p.gesture.heading,0);}assert.equal(prep.sample().finClearance,0);
const exit=factory();exit.request(Math.PI,0);exit.commit(exit.propose(2.16));assert.equal(exit.sample().active,true);assert.equal(exit.sample().bodyActive,false);assert.ok(exit.sample().sourceReplacementWeight>0&&exit.sample().sourceReplacementWeight<1,'quietprogram exitfades carrierhistory without issuinganother bodyfinbout');
// A current actualfinrate is a finiteinitialjet, never an instantaneous signflip.
const initial=factory();initial.reset({headYaw:.3,headRate:.2,headAcceleration:-.1});initial.request(-1,0,{headYaw:.3,headRate:.2,headAcceleration:-.1});approximately(initial.sample().headRate,.2);approximately(initial.sample().headAcceleration,-.1);
// Slow/stopped authority preserves the complete PHYSICAL jet at reversal.
for(const authority of [.35,0]){
 const slow=factory();slow.request(2.7,0);for(let i=0;i<108;i++)slow.commit(slow.propose(1/120));
 for(let i=0;i<180;i++)slow.commit(slow.propose(1/120,{authority}));const left=copy(slow.sample());
 slow.request(-2.7,slow.sample().physicalAge,{headYaw:left.heading,headRate:left.headRate,headAcceleration:left.headAcceleration});const right=copy(slow.sample());
 for(const key of ['heading','headRate','headAcceleration','phaseRate','phaseAcceleration'])approximately(left[key],right[key],1e-9);
 for(const key of ['b','v','a'])for(let j=0;j<3;j++)approximately(left[key][j],right[key][j],1e-9);
}
// Re-entering from real finite-fin motion after a completed source episode
// seeds its physicaljet, even though an expired source object still exists.
const reenter=factory();reenter.request(.6,0);for(let i=0;i<360;i++)reenter.commit(reenter.propose(1/120));
assert.equal(reenter.sample().active,false);reenter.request(2.5,3,{headYaw:.7,headRate:.2,headAcceleration:-.1});approximately(reenter.sample().heading,.7);approximately(reenter.sample().headRate,.2);approximately(reenter.sample().headAcceleration,-.1);
for(let i=0;i<15;i++)reenter.commit(reenter.propose(1/120));approximately(reenter.sample().finClearance,0);
let maxStressCoefficient=0,maxStressRate=0;
const stress=factory(),stressMirror=factory();
for(let i=0;i<3600;i++){
 const t=i/120;if(i%71===0){const angle=Math.sin(i*.17)*Math.PI;stress.request(angle,t,{headYaw:stress.sample().heading,urgent:i%3===0});stressMirror.request(-angle,t,{headYaw:stressMirror.sample().heading,urgent:i%3===0});}
 const authority=i%410<190?1:.15,p=stress.propose(1/120,{authority}),n=stressMirror.propose(1/120,{authority});stress.commit(p);stressMirror.commit(n);
 for(const x of [...p.gesture.b,...p.gesture.v,...p.gesture.a,p.headDelta,p.gesture.headRate,p.gesture.headAcceleration])assert.ok(Number.isFinite(x));
 for(const x of [p.gesture.finClearance,p.gesture.sourceReplacementWeight,...p.gesture.activity.map(x=>x*4)])assert.ok(x>=-1e-12&&x<=1+1e-12,'convexC2auxiliaryhandoffs staybounded');
 approximately(p.headDelta,-n.headDelta,1e-9);for(let j=0;j<3;j++)approximately(p.gesture.b[j],-n.gesture.b[j],1e-9);
 maxStressCoefficient=Math.max(maxStressCoefficient,...p.gesture.b.map(Math.abs));maxStressRate=Math.max(maxStressRate,Math.abs(p.gesture.headRate));
}
// reset replays same trace and lowrequests remain completely untouched fortrim.
const fresh=factory();fresh.request(.1,0);assert.ok(fresh.sample().b.every(x=>x===0));assert.equal(fresh.sample().serial,0);c.reset();const reset=factory();assert.deepEqual(c.sample(),reset.sample());
const report={maxStressCoefficient,maxStressRate,maximumMirrorError:maxMirror,maximumPhaseRate:maxClockBound,coarseFineClockError:Math.abs(r120.phaseClock-r240.phaseClock),fullTurnFinalHeading:a.sample().heading,reverseJetMaximumError:Math.max(...['heading','headRate','headAcceleration'].map(k=>Math.abs(before[k]-after[k]))),notes:['No observedCOM/path or freefluidclaim','Samehead/bodyclock includesauthority, exactheadingincrements andpurepredictions','FullC2sourceexitisactivebeforeordinaryfinitefintrimrejoins']};
return report;
}
console.log(JSON.stringify({profile:profileChecks(),controller:controllerChecks()}));
