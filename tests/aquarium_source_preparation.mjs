// The actual warm navigation head jet must not recruit the new body cue before
// finite fin tuck/history drain. Geometry/history joins are tested separately.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createSourceSteeringController,createSourceTurnProfile} from '../static/aquarium-source-steering.js';
const file=new URL('../static/aquarium-source-steering.js',import.meta.url);
const data=JSON.parse(await readFile(new URL('../static/aquarium/reference-turn.json',import.meta.url),'utf8'));
const clone=x=>structuredClone(x),close=(a,b,e=1e-10)=>assert.ok(Math.abs(a-b)<e,`${a} versus ${b}`);
function check(create){
 const duration=1.25,age=.6166666666666669,warm={headYaw:-.033882023375103855,headRate:-.04662615261489467,headAcceleration:.05828269079078047},angle=-(Math.PI-.08),c=create(data);
 c.reset({...warm,age});assert.equal(c.request(angle,age,{...warm,urgent:true}),true);
 const entry=clone(c.sample());close(entry.heading,warm.headYaw);close(entry.headRate,warm.headRate);close(entry.headAcceleration,warm.headAcceleration);
 const boundary=Math.max(.36,c.metadata.handoffDuration),before=clone(c.sample());
 for(let i=0;i<100;i++)c.peek(1.3,{authority:i%2});assert.deepEqual(c.sample(),before,'forecasting must not consume the preparation or alter the warm jet');
 let firstBody=null;
 for(let tick=0;tick<110;tick++){
  const proposal=c.propose(1/120),peek=clone(c.peek(1/120));assert.deepEqual(peek.b,proposal.gesture.b);close(peek.heading,proposal.gesture.heading);
  const q=clone(c.commit(proposal));
  if(q.phaseClock<=boundary+1e-12){
   for(const key of ['b','v','a'])assert.ok(q[key].every(x=>Math.abs(x)<1e-12),'warm prepared body remains neutral until the C2 entry bridge finishes');
   if(q.phaseClock>=.12)close(q.finClearance,0);
  }
  if(firstBody===null&&q.b.some(x=>Math.abs(x)>1e-5))firstBody=q.phaseClock;
 }
 assert.ok(firstBody>=boundary,'new body loading follows the complete tuck and history-drain interval');
 const expected=createSourceTurnProfile(data,{angle,duration,start:boundary,heading:warm.headYaw}),t=boundary+.31;
 // Reconstruct from a fresh unadvanced plan so the comparison uses one shared
 // absolute phase clock, rather than two independently shifted channel clocks.
 const fresh=create(data);fresh.reset({...warm,age});fresh.request(angle,age,{...warm,urgent:true});const shared=fresh.peek(t),source=expected.sample(t);
 for(const key of ['b','v','a'])for(let i=0;i<3;i++)close(shared[key][i],source[key][i],1e-9);
 close(shared.heading,source.heading);close(shared.headRate,source.headRate);close(shared.headAcceleration,source.headAcceleration,1e-8);
 const h=1e-7,left=fresh.peek(boundary-h),right=fresh.peek(boundary+h);
 close(left.heading,right.heading,1e-8);close(left.headRate,right.headRate,1e-7);close(left.headAcceleration,right.headAcceleration,1e-3);
 // Cold entry retains its existing .36 preparation; only a required bridge
 // extends the source start. No extra delay is imposed on a resting swimmer.
 const cold=create(data);cold.request(angle,0,{urgent:true});const coldExpected=createSourceTurnProfile(data,{angle,duration,start:.36});
 const coldAt=cold.peek(.7),coldSource=coldExpected.sample(.7);close(coldAt.heading,coldSource.heading);for(let i=0;i<3;i++)close(coldAt.b[i],coldSource.b[i]);
 return {firstBodyClock:firstBody,neutralUntil:boundary,warmHeadRate:entry.headRate,forecastPure:true,sharedHeadBodyClock:true,coldPreparation:.36};
}
const report=check(createSourceSteeringController);
// Remove only the clock fix in memory; no tracked source is rewritten/stashed.
const original=await readFile(file,'utf8'),block=/   if\(preparation&&preparation<handoffDuration\)\{\n[\s\S]*?\n   \}\n/;
assert.ok(block.test(original),'counterfactual removes the warm preparation clock fix');
const source=original.replace(block,'').replace(/from (['"])(\.[^'"]+)\1/g,(_,quote,path)=>`from ${quote}${new URL(path,file).href}${quote}`);
const mutant=await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
assert.throws(()=>check(mutant.createSourceSteeringController),/warm prepared body remains neutral/,'old future-body bridge must fail the actual warm-jet preparation guard');
console.log(JSON.stringify({...report,oldBridgeMutationRejected:true}));
