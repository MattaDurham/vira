const assert = require("node:assert/strict");
const { create, project, unproject, inWater, camera, skinMesh, projectedMesh, optics, space, world, floorAt, bodyPoint, volumeMesh, anatomy, segments } = require("../static/koi-pond.js");
let seed = 947;
const random = () => { seed = (Math.imul(seed,1664525)+1013904223) >>> 0; return seed/4294967296; };
const pond = create(random);
const species = new Set(pond.fish.map(f => f.type));
assert.equal(species.size,4,"the pond contains four different photographic fish varieties");
for (const x of [.2,.5,.8]) for (const y of [.15,.5,.85]) {
  const screen = project(x,y), water = unproject(screen.x,screen.y);
  assert.ok(Math.abs(water.x-x) < 1e-9 && Math.abs(water.y-y) < 1e-9,
    "perspective must map a click back to the same water point");
}
assert.ok(project(.8,.1).x < project(.8,.9).x,"the far end foreshortens");
const shallow=project(.7,.5,.05),deep=project(.7,.5,1.5);
assert.ok(deep.y-shallow.y>.08,"a dive moves below its surface point through oblique refraction");
assert.ok(deep.x<shallow.x,"deeper objects recede toward the camera's vanishing axis");
const nearLight=optics(.05),deepLight=optics(1.5);
assert.ok(nearLight.clarity>.85 && deepLight.clarity<.24,"deep fish must lose substantial contrast in pond water");
assert.ok(deepLight.transmission[0]<deepLight.transmission[2] && deepLight.transmission[2]<deepLight.transmission[1],
  "warm scales lose light faster than green through the fitted pond medium");
assert.ok(deepLight.blur>nearLight.blur*5,"suspended water softens distant scale detail");
const pose={...pond.fish[0],heading:0,bank:0,pitch:0,depth:.08};
const nearSkin=projectedMesh(pose).flat();pose.depth=1.5;
const deepSkin=projectedMesh(pose).flat();
const width=mesh=>Math.max(...mesh.map(p=>p.x))-Math.min(...mesh.map(p=>p.x));
const centerY=mesh=>mesh.reduce((sum,p)=>sum+p.y,0)/mesh.length;
assert.ok(width(deepSkin)<width(nearSkin)*.97 && centerY(deepSkin)-centerY(nearSkin)>.08,
  "the actual skin mesh must recede and move through depth, not just its centre marker");
// A rendered nose must face the projected physical direction of travel. The
// metric matters especially when a photographed pond is cropped vertically.
for (const heading of [.2,1.1,2.7]) for(const depth of [0,.7,1.5]) {
  const x=.68, y=.64, epsilon=1e-5, hx=Math.cos(heading), hy=Math.sin(heading);
  const a=project(x,y,depth), b=project(x+hx*epsilon,y+hy*(16/9)*.76*epsilon,depth);
  const c=camera(x,y,16/9,depth), screen={x:c.xx*hx+c.xy*hy,y:c.yy*hy};
  const travel={x:b.x-a.x,y:(b.y-a.y)/(16/9)};
  assert.ok(Math.abs(Math.atan2(screen.y,screen.x)-Math.atan2(travel.y,travel.x)) < 1e-5,
    "camera foreshortening must not make a straight swimmer slide sideways");
}
const ranges = pond.fish.map(() => ({ min: 1, max: 0, bank: 0, depth: [] }));
let curl = 0, tailMotion = 0;
for (let i=0;i<2400;i++) {
  const before = pond.fish.map(f => [f.x,f.y,f.heading,f.depth]);
  pond.step(.025);
  pond.fish.forEach((f,j) => {
    const r = ranges[j]; r.min = Math.min(r.min,f.speed); r.max = Math.max(r.max,f.speed);
    r.bank = Math.max(r.bank,Math.abs(f.bank)); r.depth.push(f.depth);
    assert.ok(Math.hypot(f.x-before[j][0],(f.y-before[j][1])/((16/9)*.76)) < .004,"swimmers must not teleport");
    assert.ok(Number.isFinite(f.heading) && Math.abs(f.heading-before[j][2]) < .1,"turns remain smooth");
    assert.ok(Math.abs(f.depth-before[j][3])<=.42*.025+1e-9,"vertical water resistance prevents depth jumps");
    assert.ok(Math.hypot((f.x-.5)/.46,(f.y-.5)/.44) <= .940001,"fish stay inside the water");
    let length=0;
    for (let k=1;k<f.spine.length;k++) {
      const a=f.spine[k-1], b=f.spine[k], segment=Math.hypot(b.x-a.x,b.y-a.y);
      assert.ok(Math.abs(segment-segments[k-1].length) < 1e-10,"bending must not stretch bones");
      length+=segment;
      if (b.s <= .20) assert.ok(Math.abs(b.y) < 1e-10,"the skull must stay rigid");
    }
    assert.ok(Math.abs(length-1)<1e-10,"arc length stays constant through a turn");
    curl=Math.max(curl,Math.abs(f.spine.at(-1).angle));
    tailMotion=Math.max(tailMotion,Math.abs(f.spine.at(-1).y));
    const mesh=skinMesh(f);
    assert.ok(mesh.flat().every(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)),"the connected skin stays finite");
    assert.ok(projectedMesh(f).flat().every(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)&&p.depth>0),
      "banking and pitching skin stays below the surface in the shared camera");
  });
}
assert.ok(ranges.filter(r => r.min < .009 && r.max > .07).length >= 5,
  "individual fish alternate hovering/coasting with fast bursts");
assert.ok(ranges.every(r => r.bank > .1),"fish bank into organic turns");
assert.ok(ranges.every(r => Math.max(...r.depth)-Math.min(...r.depth) > .2),"swimmers vary their depth");
assert.ok(pond.drops.length > 0,"occasional droplets disturb the surface");
assert.ok(curl>1 && tailMotion>.2,"turning produces substantial flowing bends, not tiny tail offsets");
assert.equal(pond.flick(NaN,.5),false);
assert.equal(pond.flick(.5,Infinity),false);
assert.equal(pond.flick(1.4,-.3),true,"visible water extends beyond the fish roaming area");
const attention=create(()=>.5),control=create(()=>.5);
attention.follow(.7,.5);
for(let i=0;i<1200;i++){attention.step(.025);control.step(.025);}
const distance=p=>p.fish.reduce((sum,f)=>sum+Math.hypot(f.x-.7,(f.y-.5)/((16/9)*.76)),0)/p.fish.length;
assert.ok(distance(attention)<distance(control)*.9,"cursor interest biases paths without trapping fish at a single point");
assert.ok(Math.max(...attention.fish.flatMap(a=>attention.fish.map(b=>Math.hypot(a.x-b.x,a.y-b.y))))>.12,"curious swimmers remain loosely spread");
attention.follow(null);
const reacting=create(()=>.5);
Object.assign(reacting.fish[0],{x:.5,y:.5,depth:.4,vx:0,vy:0,heading:0});
Object.assign(reacting.fish[1],{x:.72,y:.5,depth:.7});
reacting.flick(.49,.5);
assert.equal(reacting.fish[0].reaction.kind,"flee","a nearby ripple startles shallow fish");
assert.equal(reacting.fish[1].reaction.kind,"inspect","more distant fish investigate after a delay");
const origin={x:reacting.fish[0].x,y:reacting.fish[0].y,depth:reacting.fish[0].depth};
reacting.step(1/120);
assert.ok(Math.abs(reacting.fish[0].x-origin.x)<.002 && Math.abs(reacting.fish[0].depth-origin.depth)<.004,"a startle supplies thrust, never teleports fish");
for(let i=0;i<180;i++)reacting.step(1/120);
assert.ok(reacting.fish[0].x>.55,"the close swimmer escapes from the disturbance");
for (let i=0;i<120;i++) reacting.flick(.5,.5);
assert.ok(reacting.drops.length<=12,"rapid clicks do not accumulate unlimited effects");
const paused=JSON.stringify(reacting.fish);reacting.step(0);
assert.equal(JSON.stringify(reacting.fish),paused,"zero-time frames retain the pose");
for(let i=0;i<800;i++)reacting.step(.025);
assert.ok(reacting.fish.every(f=>!f.reaction),"temporary disturbance responses return to wandering");
assert.equal(inWater(.5,.5),true);
// Water resistance must preserve forward momentum and damp sideways drift.
const coast=create(()=>.5); coast.fish.splice(1);
const f=coast.fish[0];
Object.assign(f,{x:.5,y:.5,heading:0,vx:.09,vy:.06,angularVelocity:0,bend:0,
  drive:0,pace:0,mode:"hover",timer:100,target:{x:.9,y:.5}});
for(let i=0;i<36;i++) coast.step(1/120);
assert.ok(f.x>.51 && f.vx>.04 && f.vx<.09,"relaxed fish must coast and gradually slow");
assert.ok(Math.abs(f.vy)<.02,"the water damps lateral slip more strongly than forward motion");
assert.equal(f.thrust,0,"momentum is not secretly supplied by a stopped tail");
Object.assign(f,{pace:.04,mode:"cruise",timer:100});
let low=Infinity,high=0;
for(let i=0;i<360;i++) {
  coast.step(1/120);
  if(i>180){low=Math.min(low,f.thrust);high=Math.max(high,f.thrust);}
}
assert.ok(high>low*1.7,"tail strokes must produce pulses of thrust");
const wave=create(()=>.5);wave.fish.splice(1);
const swimmer=wave.fish[0];
Object.assign(swimmer,{heading:0,vx:.04,vy:0,angularVelocity:0,bend:0,bendVelocity:0,
  pace:.04,mode:"cruise",timer:100,drive:.0348,target:{x:.9,y:.5}});
const crossings=[[],[]], previous=[0,0];
for(let i=0;i<1200;i++){
  swimmer.x=.5;swimmer.y=.5;wave.step(1/120);
  [10,24].forEach((joint,j)=>{
    const value=swimmer.joints[joint];
    if(i>600 && previous[j]<0 && value>=0)crossings[j].push(i/120);
    previous[j]=value;
  });
}
const start=crossings[0][0], downstream=crossings[1].find(t=>t>start);
assert.ok(downstream-start>.08 && downstream-start<.4,
  "the mechanical bend travels down the connected spine toward the tail");
const thirty=create(()=>.5), sixty=create(()=>.5);
for(let i=0;i<240;i++){thirty.step(1/30);sixty.step(1/60);sixty.step(1/60);}
thirty.fish.forEach((a,i)=>{
  const b=sixty.fish[i];
  assert.ok(Math.hypot(a.x-b.x,a.y-b.y)<1e-9,"render frame rate must not change the physics");
  assert.ok(Math.abs(a.spine.at(-1).angle-b.spine.at(-1).angle)<1e-9,"the backbone stays stable across frame rates");
});
const slow=create(()=>.5),normal=create(()=>.5);
for(let i=0;i<24;i++){slow.step(.25);for(let j=0;j<15;j++)normal.step(1/60);}
slow.fish.forEach((a,i)=>assert.ok(Math.hypot(a.x-normal.fish[i].x,a.y-normal.fish[i].y)<1e-9,
  "low frame rates retain elapsed swimming and depth time while using stable substeps"));
console.log("Connected spine, skin, momentum, water resistance, stroke thrust and pond behavior pass.");
