import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {Texture,Vector3} from '../static/vendor/three.webgpu.js';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {createAxialFrame} from '../static/aquarium-axial-frame.js';
import {createRapierChain} from '../static/aquarium-articulated.js';
import {createMechanicsTimeline,mechanicsSettings,neutralMechanicsFish} from '../static/aquarium-mechanics-study.js';
import {testArticulatedLimits} from './aquarium_articulated_limits.mjs';
const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
const asset=(await new GLTFLoader().register(()=>({name:'ArticulatedMotionGeometry',loadTexture:()=>Promise.resolve(new Texture())})).parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;
const sampler=createAxialFrame(asset),profile=JSON.parse(await readFile(new URL('../static/aquarium/body-mass.json',import.meta.url),'utf8'));
const neutral=neutralMechanicsFish();
const bend=frame=>frame.transforms.rotations[0].clone().multiply(sampler.rest[0].clone().invert()).normalize()
 .angleTo(frame.transforms.rotations[33].clone().multiply(sampler.rest[33].clone().invert()).normalize());
const degrees=180/Math.PI,reports=[];
for(const rate of [240,480]){
 const chain=await createRapierChain(sampler,profile,{dt:1/rate,...mechanicsSettings});
 try{
  chain.configure('off');chain.setWholeVelocity(new Vector3(.03,-.02,.01),new Vector3(.2,.7,-.3));
  const initial=chain.measure();let maxGain=0,maxH=0,maxCOM=0;
  chain.step();assert.ok(chain.measure().kineticEnergy<=initial.kineticEnergy+1e-7,'motor-off rotation cannot acquire the old kinematic follow-through energy kick');
  for(let i=1;i<rate;i++){
   chain.step();const m=chain.measure();maxGain=Math.max(maxGain,(m.kineticEnergy-initial.kineticEnergy)/initial.kineticEnergy);
   maxH=Math.max(maxH,m.H.distanceTo(initial.H));
   maxCOM=Math.max(maxCOM,m.com.distanceTo(initial.com.clone().addScaledVector(initial.P,chain.age/initial.mass)));
  }
  assert.ok(maxGain<.001,'unforced whole-body kinetic energy stays within Float32 solver residual');
  assert.ok(maxH<.002&&maxCOM<2e-5,'free articulated motion preserves momentum and COM trajectory');
 }finally{chain.free();}
 const timeline=await createMechanicsTimeline(sampler,profile,{rate});
 assert.equal(timeline.frames.length,4*rate+1);
 const angles=timeline.frames.map(bend),peak=Math.max(...angles),at=angles.indexOf(peak)/rate;
 assert.ok(peak*degrees>130&&peak*degrees<160,'issued C must reach a deep actual articulated bend');
 assert.ok(at>=.35&&at<=.60,'deepest bend must occur while the target is still loaded');
 assert.ok(angles[Math.round(.35*rate)]*degrees>90,'body must load during the turn, not after the command ends');
 assert.ok(angles[Math.round(.60*rate)]*degrees>125,'deep bend persists through the loaded interval');
 assert.ok(angles[Math.round(1.60*rate)]*degrees<3,'neutral motors must settle rather than retain a delayed large curl');
 assert.ok(Math.max(...timeline.frames.map(f=>Math.abs(f.visualFish.yaw)))>.15,'the physical head reacts to body loading');
 assert.ok(timeline.frames.every(f=>f.measurement.energy===null&&f.measurement.potentialEnergy===null),'active SDK compound servos cannot claim a conservative spring energy');
 assert.ok(timeline.frames.every(f=>Number.isFinite(f.measurement.energyDiagnostic)&&f.measurement.anchorError<3e-6),'physical constraints and the labeled energy estimate remain finite');
 reports.push({rate,peakDegrees:peak*degrees,peakTime:at,loadedDegrees:angles[Math.round(.60*rate)]*degrees});
}
assert.ok(Math.abs(reports[0].peakDegrees-reports[1].peakDegrees)<6&&Math.abs(reports[0].peakTime-reports[1].peakTime)<.025,'deep-turn timing and magnitude remain close under step refinement');
// A root-rate display packet must never reactivate the old pose-history filter
// inside the active joint target path, even if a held-pose flag is supplied.
{
 const chain=await createRapierChain(sampler,profile,{...mechanicsSettings});
 try{
  chain.configure('active');
  for(let i=0;i<120;i++){chain.setTargets({...neutral,poseHistory:false,yawRate:1,pitchRate:.3,bankRate:.2});chain.step();}
  assert.ok(chain.measure().kineticEnergy<1e-8,'root angular-rate metadata must not become an unbudgeted internal motor command');
 }finally{chain.free();}
}
// The explicit geodesic spring branch has an actual capped potential. Check
// that branch separately from the active SDK rest controller.
{
 const chain=await createRapierChain(sampler,profile,{dt:1/240,naturalFrequency:30,initialFish:{...neutral,turnAnterior:2.4,turnTrunk:2.4,turnPosterior:2.4}});
 try{
  chain.configure('passive');let previous=chain.measure().energy;
  assert.ok(previous>0&&Number.isFinite(previous));
  for(let i=0;i<480;i++){chain.step();const m=chain.measure();assert.ok(m.energy<=previous+1e-7,'actual geodesic spring release must account for kinetic and stored energy');previous=m.energy;}
 }finally{chain.free();}
}
const compound=await createMechanicsTimeline(sampler,profile,{preset:'compound'});
const maxima=[0,0,0];
for(const frame of compound.frames){
 const head=frame.transforms.rotations[0].clone().multiply(sampler.rest[0].clone().invert()).normalize();
 for(let i=5;i<34;i++){
  const relative=head.clone().invert().multiply(frame.transforms.rotations[i].clone().multiply(sampler.rest[i].clone().invert())).normalize();
  for(const [axis,key] of ['x','y','z'].entries())maxima[axis]=Math.max(maxima[axis],Math.abs(relative[key]));
 }
}
assert.ok(maxima[0]>.02&&maxima[1]>.2&&maxima[2]>.02,'compound experiment must physically bend and twist on every axis');
await testArticulatedLimits(sampler,profile);
console.log(JSON.stringify({turns:reports,compoundQuaternionComponents:maxima,checks:'Actual joint motion, timing, refinement, energy scope, and rate-metadata isolation passed.'}));
