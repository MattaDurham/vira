import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createSwimmer} from '../static/aquarium-motion.js';
import {createStudyRandom} from '../static/aquarium-study.js';
import {tankCenterZ} from '../static/aquarium-space.js';

const dt=1/120,start={x:.85,y:.84,z:tankCenterZ,yaw:0};
const finite=fish=>{for(const [key,value] of Object.entries(fish))if(typeof value==='number')assert.ok(Number.isFinite(value),`finite live ${key}`);};
const snapshot=swimmer=>({fish:{...swimmer.fish},gait:swimmer.behavior.gait,intent:swimmer.behavior.intent,contacts:swimmer.behavior.contacts});
function harness(create,goal){
  const random=createStudyRandom(913),swimmer=create(random);
  const reset=()=>{random.reset();swimmer.reset();Object.assign(swimmer.fish,start);swimmer.navigate(goal??{x:start.x,y:start.y,z:start.z});};
  const step=()=>{swimmer.step(dt);finite(swimmer.fish);return snapshot(swimmer);};
  reset();return {swimmer,reset,step};
}
function warmHover(h){for(let tick=0;tick<240;tick++)h.step();assert.equal(h.swimmer.behavior.intent,'hover');assert.equal(h.swimmer.behavior.gait,'coast');assert.ok(h.swimmer.fish.speed<.025&&h.swimmer.fish.muscle3<.03,'real arrival supplies a settled low-flow start');}
const farGoal=fish=>({x:-1.05,y:fish.y+.30,z:tankCenterZ});
function departure(h,{repeat=false}={}){
  const began=snapshot(h.swimmer),goal=farGoal(h.swimmer.fish),records=[];assert.ok(h.swimmer.navigate(goal));
  for(let tick=1;tick<=360;tick++){
    // Updating an identical waypoint during the first active departure must
    // not repeatedly restart the finite stroke or draw a new gait duration.
    if(repeat&&[12,24,36].includes(tick))h.swimmer.navigate(goal);
    records.push({...h.step(),elapsed:tick*dt});
  }
  return {began,goal,records};
}
const first=(records,predicate)=>records.find(r=>predicate(r.fish,r));
function departureMetrics(trace){
  const {began,records}=trace;
  const recruited=first(records,f=>f.muscle3>.1),flow=first(records,f=>f.forwardSpeed>.003);
  assert.ok(recruited&&recruited.elapsed<=.5,'a settled departure visibly recruits the posterior before the inherited coast could expire');
  assert.ok(flow&&flow.elapsed<=.65,'a far departure develops meaningful water-relative forward motion promptly');
  assert.ok(Math.abs(flow.fish.pitch-began.fish.pitch)<.06,'forward swimming starts before a stationary pitch pivot accumulates');
  const after=records[239].fish;
  assert.ok(began.fish.x-after.x>.02&&after.y-began.fish.y>.003,'the requested existing stroke produces forward and upward progress');
  assert.ok(records.some(r=>r.elapsed>.5&&r.gait==='coast'&&r.fish.stroke<.02&&r.fish.muscle3<.03),'departure remains a finite kick followed by a quiet glide');
  assert.ok(records.every(r=>r.contacts===0),'the centered departure needs no boundary correction');
  return {posteriorOnset:recruited.elapsed,forwardOnset:flow.elapsed,pitchAtForwardOnset:flow.fish.pitch,
    forwardProgressAtTwoSeconds:began.fish.x-after.x,riseAtTwoSeconds:after.y-began.fish.y,
    firstCoast:first(records,(_,r)=>r.gait==='coast')?.elapsed};
}

async function observeActualRig(trace){
  const [{GLTFLoader},{Texture,Vector3,Quaternion},{createKoiInstance},{createResearchShape}]=await Promise.all([
    import('../static/vendor/GLTFLoader.js'),import('../static/vendor/three.webgpu.js'),import('../static/aquarium-fish-instance.js'),import('../static/aquarium-research-shape.js')]);
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const source=(await new GLTFLoader().register(()=>({name:'DepartureGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())})).parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;
  const sourceBones=[];source.traverse(n=>{if(n.isBone)sourceBones.push({bone:n,p:n.position.clone(),q:n.quaternion.clone()});});
  const instance=createKoiInstance(source,{random:()=>.5}),head=instance.asset.getObjectByName('spine_01');
  const rest=head.getWorldQuaternion(new Quaternion()).premultiply(instance.asset.getWorldQuaternion(new Quaternion()).invert()),shape=createResearchShape(instance.asset);
  const controllers=[...Object.values(instance.pairedControllers).flatMap(f=>Object.values(f)),instance.caudalController,...Object.values(instance.medianControllers)];
  for(const controller of controllers){const sample=controller.sample.bind(controller);controller.sample=fish=>sample(fish,'departure-geometry');}
  let maximumHeadError=0,maximumVisibleBend=0;const selected=[];
  for(let tick=0;tick<trace.records.length;tick++){
    const fish=trace.records[tick].fish;for(const controller of controllers)controller.sample(fish);
    if(![23,59,119,239].includes(tick))continue;
    instance.pose(fish);instance.updateVisual(fish);
    const q=head.getWorldQuaternion(new Quaternion()).multiply(rest.clone().invert()).normalize(),direction=new Vector3(-1,0,0).applyQuaternion(q);
    const actualPitch=Math.atan2(direction.y,Math.hypot(direction.x,direction.z)),s=shape.sample();
    maximumHeadError=Math.max(maximumHeadError,Math.abs(actualPitch-fish.pitch));maximumVisibleBend=Math.max(maximumVisibleBend,s.maximumDOverL);
    selected.push({age:fish.age,actualPitch,sourcePitch:fish.pitch,visibleDOverL:s.maximumDOverL});
  }
  assert.ok(maximumHeadError<2e-6,'the actual rendered skull follows the observed departure attitude');
  assert.ok(maximumVisibleBend>.005,'posterior activation produces visible body articulation in the actual uploaded skin');
  for(const r of sourceBones){assert.deepEqual(r.bone.position.toArray(),r.p.toArray());assert.deepEqual(r.bone.quaternion.toArray(),r.q.toArray(),'posing the owned instance preserves its pristine source bones');}
  instance.dispose();return {selected,maximumHeadError,maximumVisibleBend};
}

export async function assertNavigationDeparture(create=createSwimmer,{geometry=true}={}){
  const report={};
  // 1. A real quiet hover becomes a finite, propulsive climb promptly.
  const hover=harness(create);warmHover(hover);const trace=departure(hover);report.hover=departureMetrics(trace);

  // 2. A slow coast that is still navigating also re-arms when a genuinely far
  // new destination arrives. This start is obtained through normal dynamics.
  const stopped=harness(create,{x:.55,y:start.y,z:start.z});let observed=false;
  for(let tick=0;tick<720;tick++){
    stopped.step();const f=stopped.swimmer.fish,b=stopped.swimmer.behavior;
    if(b.intent==='navigate'&&b.gait==='coast'&&f.speed<.025&&f.stroke<.02&&f.muscle3<.03){observed=true;break;}
  }
  assert.ok(observed,'fixture observes a genuine slow coast outside arrival');
  const slow=departure(stopped),recruited=first(slow.records,f=>f.muscle3>.1);
  assert.ok(recruited&&recruited.elapsed<=.5,'a low-speed navigating coast does not defer a new far travel decision');
  assert.ok(slow.records.slice(0,90).some(r=>r.fish.forwardSpeed>slow.began.fish.forwardSpeed+.01),'the new stroke increases forward flow from the slow coast');
  report.slowCoast={initialSpeed:slow.began.fish.speed,posteriorOnset:recruited.elapsed};

  // 3. A useful glide keeps its existing gait when its far waypoint changes.
  const moving=harness(create,{x:-1.05,y:start.y,z:start.z});let movingStart;
  for(let tick=0;tick<480;tick++){
    moving.step();const f=moving.swimmer.fish;
    if(moving.swimmer.behavior.gait==='coast'&&f.speed>.05){movingStart={...f};break;}
  }
  assert.ok(movingStart,'fixture observes an actual moving glide');
  const goal={x:-1.05,y:movingStart.y,z:movingStart.z+(movingStart.x+1.05)*Math.tan(movingStart.yaw)};
  moving.swimmer.navigate(goal);const glide=[];for(let tick=0;tick<42;tick++)glide.push(moving.step());
  assert.ok(glide.every(r=>r.gait==='coast'),'a moving-coast waypoint update preserves the glide');
  assert.ok(glide.at(-1).fish.stroke<movingStart.stroke*.4,'the existing carrier continues releasing rather than restarting');
  report.movingCoast={initialSpeed:movingStart.speed,initialStroke:movingStart.stroke,finalStroke:glide.at(-1).fish.stroke};

  // 4. A destination clearly within the existing arrival neighborhood does not
  // start an unused travel stroke or recruit a settled posterior.
  const nearby=harness(create);warmHover(nearby);const f=nearby.swimmer.fish;
  nearby.swimmer.navigate({x:f.x-.10,y:f.y,z:f.z});const near=[];for(let tick=0;tick<60;tick++)near.push(nearby.step());
  assert.ok(near.every(r=>r.gait==='coast'&&r.fish.stroke<.001&&r.fish.muscle3<.001),'near-arrival navigation stays quiet');
  report.nearArrival={maximumStroke:Math.max(...near.map(r=>r.fish.stroke)),maximumPosterior:Math.max(...near.map(r=>r.fish.muscle3))};

  // A coaxial height decision has no new azimuth. Signed-zero input offsets,
  // micron noise and actual water drift must not turn that into a C turn.
  const wrap=value=>Math.atan2(Math.sin(value),Math.cos(value));report.overhead=[];
  for(const {dx,dz,fresh=false} of [{dx:0,dz:0},{dx:-0,dz:-0},{dx:1e-6,dz:-1e-6},{dx:-1e-6,dz:1e-6},{dx:0,dz:0,fresh:true}]){
    const overhead=harness(create);
    if(fresh)Object.assign(overhead.swimmer.fish,{x:.3,y:start.y,z:start.z,yaw:.7});
    else warmHover(overhead);
    const began={...overhead.swimmer.fish},serial=began.turnGestureSerial;
    const target={x:began.x+dx,y:began.y+.30,z:began.z+dz};overhead.swimmer.navigate(target);
    let maximumHorizontal=0,maximumYawChange=0,maximumBodyTurn=0;
    for(let tick=0;tick<60;tick++){
      const {fish}=overhead.step();maximumHorizontal=Math.max(maximumHorizontal,Math.hypot(target.x-fish.x,target.z-fish.z));
      maximumYawChange=Math.max(maximumYawChange,Math.abs(wrap(fish.yaw-began.yaw)));
      maximumBodyTurn=Math.max(maximumBodyTurn,...['turnAnterior','turnTrunk','turnPosterior'].map(key=>Math.abs(fish[key])));
      assert.equal(fish.turnGestureSerial,serial,'a height-only goal does not issue a new body-turn gesture');
    }
    assert.ok(maximumHorizontal<.02,'the observed decision remains genuinely coaxial despite live drift');
    assert.ok(maximumYawChange<.03&&maximumBodyTurn<.02,'coaxial height goals preserve actual heading and avoid a spurious C turn');
    report.overhead.push({dx,dz,fresh,maximumHorizontal,maximumYawChange,maximumBodyTurn});
  }
  const course=harness(create);warmHover(course);const courseStart={...course.swimmer.fish};
  course.swimmer.navigate({x:courseStart.x,y:courseStart.y+.30,z:courseStart.z+.08});
  let courseMaximumYaw=0,courseMaximumBend=0;
  for(let tick=0;tick<60;tick++){
    const {fish}=course.step();courseMaximumYaw=Math.max(courseMaximumYaw,wrap(fish.yaw-courseStart.yaw));
    courseMaximumBend=Math.max(courseMaximumBend,Math.abs(fish.turnTrunk));
  }
  assert.ok(courseMaximumYaw>.02&&courseMaximumBend>.1,'a real horizontal course still recruits a visible turn');
  report.horizontalCourse={courseMaximumYaw,courseMaximumBend};

  // 5. Repeating an identical waypoint cannot turn one departure into a stream
  // of newly timed strokes. Resetting RNG and the actual swimmer replays it.
  const repeated=harness(create);warmHover(repeated);const repeatedTrace=departure(repeated,{repeat:true});
  assert.deepEqual(repeatedTrace.records,trace.records,'identical active waypoint updates preserve the physical departure history');
  hover.reset();warmHover(hover);const replay=departure(hover);
  assert.deepEqual(replay,trace,'reset/replay reproduces the complete live departure');
  report.identicalWaypointPreserved=true;report.resetReplay=true;
  if(geometry)report.geometry=await observeActualRig(trace);
  return report;
}

if(process.argv[1]&&new URL(process.argv[1],'file://').href===import.meta.url)console.log(JSON.stringify(await assertNavigationDeparture()));
