import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {createTurnController,turnResponse} from '../static/aquarium-turn.js';

const fields=['b','v','a','load','activity'];
const clone=value=>structuredClone(value);
const near=(actual,wanted,tolerance=1e-9,message='turn control agrees')=>
  assert.ok(Number.isFinite(actual)&&Math.abs(actual-wanted)<=tolerance,`${message}: ${actual} versus ${wanted}`);
const compare=(a,b,message)=>{
  for(const field of fields)for(let i=0;i<3;i++)near(a[field][i],b[field][i],1e-10,`${message}: ${field}[${i}]`);
};
function finiteGesture(sample){
  for(const field of fields){assert.equal(sample[field].length,3);assert.ok(sample[field].every(Number.isFinite),`${field} is finite`);}
  for(const field of ['serial','age','budget','recovery','intensity','energy','power','settleWeight','handoffMix','outgoingBudget','outgoingSerial','outgoingSettleWeight'])assert.ok(Number.isFinite(sample[field]),`${field} is finite`);
  assert.ok(sample.energy>=0&&sample.recovery>=0&&sample.recovery<=1+1e-12,'energy and recovery stay bounded and nonnegative');
  assert.ok(sample.power>=0&&sample.power<=1,'issued power stays bounded and nonnegative');
}

function evaluate(makeController,time,events){
  const controller=makeController();
  for(const [age,angle,options] of events)if(age<=time){controller.request(angle,age,options);controller.sample(age);}
  return clone(controller.sample(time));
}

function continuity(makeController,events,boundaries){
  for(const time of boundaries){
    if(time<=0)continue;
    const jumps=epsilon=>{
      const before=evaluate(makeController,time-epsilon,events),after=evaluate(makeController,time+epsilon,events);
      return ['b','v','a'].map(field=>Math.max(...before[field].map((value,i)=>Math.abs(value-after[field][i]))));
    };
    const large=jumps(1e-4),small=jumps(1e-6);
    small.forEach((jump,i)=>assert.ok(jump<=large[i]*.04+1e-7,`${['bend','velocity','acceleration'][i]} remains continuous at ${time}: ${jump} / ${large[i]}`));
  }
  // Returned velocity and acceleration must describe the issued shape, not
  // independently authored signals that happen to look smooth at boundaries.
  for(let time=.019;time<1.65;time+=.037){
    if(events.some(([age])=>Math.abs(time-age)<1e-4))continue;
    const epsilon=1e-5,before=evaluate(makeController,time-epsilon,events),middle=evaluate(makeController,time,events),after=evaluate(makeController,time+epsilon,events);
    for(let i=0;i<3;i++){
      near((after.b[i]-before.b[i])/(2*epsilon),middle.v[i],2e-5*(1+Math.abs(middle.v[i])),'velocity differentiates the actual bend');
      near((after.v[i]-before.v[i])/(2*epsilon),middle.a[i],2e-4*(1+Math.abs(middle.a[i])),'acceleration differentiates the actual velocity');
    }
  }
}

function integrate(makeController,response,rate,{angle=1,urgent=false}={}){
  const controller=makeController(),dt=1/rate;controller.request(angle,0,{urgent});
  let yaw=0,omega=0,maximumRate=0,maximumMoment=0;
  for(let tick=0;tick<3*rate;tick++){
    const gesture=controller.sample((tick+.5)*dt),result=response(gesture,omega,dt,{headingProgress:yaw});
    for(const field of ['yawRate','moment','finMoment','recoveryMoment','damping','budgetBraking'])assert.ok(Number.isFinite(result[field]),`${field} response is finite`);
    yaw+=(omega+result.yawRate)*dt/2;omega=result.yawRate;
    maximumRate=Math.max(maximumRate,Math.abs(omega));maximumMoment=Math.max(maximumMoment,Math.abs(result.moment));
  }
  return {yaw,omega,maximumRate,maximumMoment};
}

// Factories are injectable: mutation checks exercise behavioral assertions
// without changing production files or relying on source-text assertions.
export function assertTurnControl(makeController=createTurnController,response=turnResponse){
  const held={b:[1,-.5,.7],v:[0,0,0],a:[0,0,0],load:[0,0,0],activity:[1,1,1],energy:1,intensity:1,
    recovery:0,budget:1,settling:false,settleWeight:0};
  for(const yawRate of [-2,-.2,0,.2,2])for(const extra of [0,1,17]){
    const result=response({...held,yawLag1:extra,turnBend3:extra,muscle3:extra,phase:extra,passiveYaw:extra},yawRate,1/120);
    near(result.moment,0,1e-12,'held bend and root/passive motion cannot supply new issued-load moment');
    near(result.finMoment,0,1e-12,'no fin command invents no fin moment');
    if(yawRate===0)near(result.yawRate,0,1e-12,'held shape cannot turn a stationary head');
  }

  let maximumLoad=0,maximumMoment=0,maximumPower=0,maximumNormalBend=0,maximumUrgentBend=0,maximumRecoveryPower=-Infinity;
  let firstLoad,lastBend;
  for(const urgent of [false,true])for(const sign of [-1,1]){
    const controller=makeController();assert.equal(controller.request(sign*100,0,{urgent}),true,'a finite large request starts a bounded episode');
    const first=[Infinity,Infinity,Infinity],last=[0,0,0],cap=urgent?2.4:1.8;
    let peakPower=0,peakPowerTime=0,peakEnergy=0,peakEnergyTime=0;
    for(let tick=0;tick<=1200;tick++){
      const age=tick/600,sample=clone(controller.sample(age));finiteGesture(sample);
      if(sample.v.every(value=>Math.abs(value)<1e-14))near(sample.power,0,1e-12,'held or quiet issued velocities cannot supply propulsive power');
      if(sample.preparing)near(sample.power,0,1e-12,'fin-clearance preparation supplies no body stroke power');
      maximumPower=Math.max(maximumPower,sample.power);
      if(sample.power>peakPower+1e-12){peakPower=sample.power;peakPowerTime=age;}
      if(sample.energy>peakEnergy+1e-12){peakEnergy=sample.energy;peakEnergyTime=age;}
      for(let i=0;i<3;i++){
        const product=sample.b[i]*sample.v[i];
        if(product<=0)near(sample.load[i],0,1e-12,'only growing issued bend supplies regional load');
        assert.ok(Math.abs(sample.load[i])<=Math.abs(sample.v[i])+1e-12,'load cannot exceed its issued regional velocity');
        if(Math.abs(sample.load[i])>1e-7){assert.ok(sample.load[i]*sample.v[i]>0,'regional load has its issued velocity sign');first[i]=Math.min(first[i],age);}
        if(Math.abs(sample.b[i])>1e-7)last[i]=age;
        assert.ok(Math.abs(sample.b[i])<=cap+1e-10,'normal and urgent episodes obey their authored bend bounds');
      }
      const actual=response(sample,sign*.1,1/120),blocked=response(sample,sign*.1,1/120,{allowed:0});
      near(blocked.moment,0,1e-12,'blocked issued load cannot supply a body moment');
      near(response({...sample,v:[0,0,0]},sign*.1,1/120).moment,0,1e-12,'removing issued shape velocity removes body moment despite stale load metadata');
      near(response({...sample,load:[7,-9,11],energy:17},sign*.1,1/120).moment,actual.moment,1e-12,'cached load or energy metadata cannot replace the issued bend and velocity');
      maximumLoad=Math.max(maximumLoad,...sample.load.map(Math.abs));maximumMoment=Math.max(maximumMoment,Math.abs(actual.moment));
      if(urgent)maximumUrgentBend=Math.max(maximumUrgentBend,...sample.b.map(Math.abs));else maximumNormalBend=Math.max(maximumNormalBend,...sample.b.map(Math.abs));
    }
    assert.ok(first[0]<first[1]&&first[1]<first[2],'issued loading travels from anterior to posterior regions');
    assert.ok(last[0]<last[1]&&last[1]<last[2],'posterior shape recovers after the anterior region');
    assert.ok(peakPowerTime+1/120<peakEnergyTime,'stroke power peaks during issued motion before maximum stored bend');
    firstLoad=first;lastBend=last;
    const settled=clone(controller.sample(4));
    for(const field of fields)assert.deepEqual(settled[field],[0,0,0],'completed gesture has no continuing shape or activity');
    near(settled.energy,0);near(settled.power,0);assert.equal(settled.active,false);assert.equal(settled.settling,false);
    near(response(settled,sign*.3,1/120).moment,0,1e-12,'completed episode supplies no new moment');
  }
  assert.ok(maximumLoad>.1&&maximumMoment>.1,'actual issued loading produces a nontrivial angular response');
  assert.ok(maximumPower>.1,'actual issued shape motion supplies a nontrivial propulsive power signal');
  assert.ok(maximumNormalBend>1&&maximumUrgentBend>2,'the bound checks exercise large normal and urgent episodes');

  // Recovery and budget stopping must remove actual angular momentum even if
  // the heading has overshot or the current rate opposes the original budget.
  for(const sign of [-1,1])for(const omega of [-2,-.1,0,.1,2])for(const recovery of [0,.2,1])for(const headingProgress of [-2,0,2])for(const handoffMix of [0,.5,1]){
    const gesture={...held,budget:sign,load:[0,0,0],recovery,settling:true,settleWeight:1,
      outgoingBudget:-sign,outgoingSettleWeight:1,handoffMix};
    const result=response(gesture,omega,1/120,{headingProgress,outgoingHeadingProgress:-headingProgress});
    assert.ok(Math.abs(result.yawRate)<=Math.abs(omega)+1e-12,'recovery without issued load or fin input cannot increase angular speed');
    const power=result.recoveryMoment*result.yawRate;maximumRecoveryPower=Math.max(maximumRecoveryPower,power);
    assert.ok(power<=1e-12,'recovery and budget stopping have nonpositive power');
    assert.ok(result.damping>=0&&result.budgetBraking>=0,'passive stopping cannot become negative damping');
  }
  const quiet={...held,load:[0,0,0],recovery:0,settleWeight:0,handoffMix:1};
  const coast=response(quiet,.5,1/120),recover=response({...quiet,recovery:1},.5,1/120);
  assert.ok(Math.abs(recover.yawRate)<Math.abs(coast.yawRate),'recovery supplies additional stopping beyond ordinary coast drag');
  assert.ok(recover.recoveryMoment*recover.yawRate<0,'a moving recovery surface actually removes angular energy');
  const budget={...quiet,budget:1,settleWeight:1};
  const early=response(budget,1,1/120,{headingProgress:0}),nearBudget=response(budget,1,1/120,{headingProgress:.99});
  assert.ok(nearBudget.budgetBraking>early.budgetBraking&&Math.abs(nearBudget.yawRate)<Math.abs(early.yawRate),'a used heading budget deploys an effective passive stopping surface');

  const brakeHandoff=makeController();brakeHandoff.request(2,0);
  const beforeBrake=response(clone(brakeHandoff.sample(.4)),1.5,1/120,{headingProgress:1.99});
  assert.ok(beforeBrake.budgetBraking>0,'the reversal fixture begins with a deployed brake');
  brakeHandoff.request(-2,.4);
  const afterBrake=response(clone(brakeHandoff.sample(.4)),1.5,1/120,{headingProgress:0,outgoingHeadingProgress:1.99});
  for(const field of ['yawRate','damping','budgetBraking','moment'])near(afterBrake[field],beforeBrake[field],1e-10,'reversal retains the already deployed outgoing stopping surface');

  const repeated=makeController(),unchanged=makeController();repeated.request(2,0);unchanged.request(2,0);
  assert.equal(repeated.request(.4,.18,{urgent:true}),false,'same-sign active request keeps its latched episode');
  for(const time of [.18,.31,.6,1,1.4]){
    const a=clone(repeated.sample(time)),b=clone(unchanged.sample(time));compare(a,b,'same-sign request does not restart shape');
    assert.equal(a.serial,b.serial);assert.equal(a.budget,b.budget);
  }

  const boundary=.2+.42,baselineEvents=[[0,2],[.2,-2]];
  for(const latest of [-2,0]){
    const controller=makeController();
    for(const [time,angle] of [[0,2],[.2,-2],[.25,2]]){controller.request(angle,time);controller.sample(time);}
    controller.request(latest,boundary);
    for(const time of [boundary,boundary+.07,boundary+.2,boundary+.42]){
      const actual=clone(controller.sample(time)),expected=evaluate(makeController,time,baselineEvents);
      assert.equal(actual.serial,2,'latest demand cancels an obsolete queued reversal before exact handoff activation');
      assert.equal(actual.budget,-2);compare(actual,expected,'latest neutral or same-current demand preserves the valid recovery');
    }
  }
  const longGap=makeController();for(const [angle,time] of [[1,0],[-1,.04],[1,.08]])longGap.request(angle,time);
  const first=clone(longGap.sample(3)),second=clone(longGap.sample(3));
  assert.deepEqual(first,second,'same-age sampling is idempotent after draining expired handoffs');
  assert.equal(first.active,false);assert.equal(first.settling,false);assert.equal(longGap.ready(3),true);
  assert.equal(longGap.request(1,3),true,'an elapsed queued handoff cannot consume a fresh request');

  const reset=makeController();reset.request(2,0);reset.request(-2,.2);reset.request(2,.25);reset.sample(.4);
  const cleared=clone(reset.reset());
  for(const field of fields)assert.deepEqual(cleared[field],[0,0,0],'reset clears issued shape, derivatives and load');
  for(const field of ['serial','age','budget','recovery','intensity','energy','power','settleWeight','outgoingBudget','outgoingSerial','outgoingSettleWeight'])near(cleared[field],0,1e-12,`reset clears ${field}`);
  near(cleared.handoffMix,1,1e-12,'reset leaves no outgoing handoff');
  assert.equal(cleared.active,false);assert.equal(cleared.settling,false);assert.equal(reset.ready(0),true);
  reset.request(2,0);reset.sample(.5);compare(clone(reset.sample(.1)),clone(makeController().sample(.1)),'rewinding the clock clears stale motion');

  for(const urgent of [false,true]){
    const duration=urgent?.72:1.08,preparation=urgent?.36:0;
    const boundaries=[0,.055,.11,.30,.355,.41,.39,.54,.69,.84,.99,1.14].map(t=>preparation+t*duration);
    continuity(makeController,[[0,2,{urgent}]],boundaries);
  }
  continuity(makeController,[[0,2],[.18,-2]],[.18,.18+.42]);
  continuity(makeController,[[0,2],[.18,-2],[.25,2]],[.18,.18+.42,.18+.84]);
  // A request arriving after an unobserved queue deadline must not cancel
  // motion that already began at that earlier simulation time.
  for(const angle of [-2,0])continuity(makeController,[[0,2],[.2,-2],[.25,2],[.7,angle]],[.7,.7+.42]);

  const positive=makeController(),negative=makeController();
  for(const [time,angle,options] of [[0,2], [.18,-1], [.25,1.4], [.7,0], [1.8,-2,{urgent:true}]]){
    positive.request(angle,time,options);negative.request(-angle,time,options);
    const a=clone(positive.sample(time)),b=clone(negative.sample(time));
    for(const field of ['b','v','a','load'])for(let i=0;i<3;i++)near(a[field][i],-b[field][i],1e-10,'opposite requested turns mirror their issued shape');
    for(const field of ['energy','power','recovery','intensity'])near(a[field],b[field],1e-10,'mirrored episode keeps unsigned control values');
    const ra=response(a,.3,1/120,{finRate:.04,headingProgress:.2}),rb=response(b,-.3,1/120,{finRate:-.04,headingProgress:-.2});
    for(const field of ['yawRate','moment','finMoment','recoveryMoment'])near(ra[field],-rb[field],1e-10,'angular response mirrors with command and rate');
  }

  const runs=[60,120,240,480,960].map(rate=>({rate,...integrate(makeController,response,rate)}));
  const reference=runs.at(-1),errors=runs.slice(0,-1).map(run=>Math.hypot(run.yaw-reference.yaw,run.omega-reference.omega));
  assert.ok(reference.yaw>.02&&reference.maximumRate>.02,'a finite issued episode actually turns the head');
  assert.ok(errors[1]<errors[0]*.8&&errors[2]<errors[1]*.8,'head response converges under timestep refinement');
  const urgent=integrate(makeController,response,120,{angle:Math.PI,urgent:true});
  assert.ok(Number.isFinite(urgent.yaw)&&Number.isFinite(urgent.omega),'urgent finite episode ends with a finite response');
  return {maximumNormalBend,maximumUrgentBend,maximumLoad,maximumMoment,maximumPower,maximumRecoveryPower,
    firstRegionalLoadSeconds:firstLoad,lastRegionalBendSeconds:lastBend,
    convergence:runs.map((run,i)=>({hz:run.rate,yaw:run.yaw,yawRate:run.omega,errorTo960:errors[i]??0}))};
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const report=assertTurnControl();
  console.log(`Finite turn control passed: ${JSON.stringify({maximumNormalBend:report.maximumNormalBend,maximumUrgentBend:report.maximumUrgentBend,maximumRecoveryPower:report.maximumRecoveryPower,refinementError120:report.convergence[1].errorTo960})}`);
}
