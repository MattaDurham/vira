const assert = require("node:assert/strict");
const { create, project, unproject, inWater, camera, skinMesh } = require("../static/koi-pond.js");
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
// A rendered nose must face the projected physical direction of travel. The
// metric matters especially when a photographed pond is cropped vertically.
for (const heading of [.2,1.1,2.7]) {
  const x=.68, y=.64, epsilon=1e-5, hx=Math.cos(heading), hy=Math.sin(heading);
  const a=project(x,y), b=project(x+hx*epsilon,y+hy*(16/9)*.76*epsilon);
  const c=camera(x,y), screen={x:c.xx*hx+c.xy*hy,y:c.yy*hy};
  const travel={x:b.x-a.x,y:(b.y-a.y)/(16/9)};
  assert.ok(Math.abs(Math.atan2(screen.y,screen.x)-Math.atan2(travel.y,travel.x)) < 1e-5,
    "camera foreshortening must not make a straight swimmer slide sideways");
}
const ranges = pond.fish.map(() => ({ min: 1, max: 0, bank: 0, depth: [] }));
let curl = 0, tailMotion = 0;
for (let i=0;i<2400;i++) {
  const before = pond.fish.map(f => [f.x,f.y,f.heading]);
  pond.step(.025);
  pond.fish.forEach((f,j) => {
    const r = ranges[j]; r.min = Math.min(r.min,f.speed); r.max = Math.max(r.max,f.speed);
    r.bank = Math.max(r.bank,Math.abs(f.bank)); r.depth.push(f.depth);
    assert.ok(Math.hypot(f.x-before[j][0],(f.y-before[j][1])/((16/9)*.76)) < .004,"swimmers must not teleport");
    assert.ok(Number.isFinite(f.heading) && Math.abs(f.heading-before[j][2]) < .1,"turns remain smooth");
    assert.ok(Math.hypot((f.x-.5)/.46,(f.y-.5)/.44) <= .940001,"fish stay inside the water");
    let length=0;
    for (let k=1;k<f.spine.length;k++) {
      const a=f.spine[k-1], b=f.spine[k], segment=Math.hypot(b.x-a.x,b.y-a.y);
      assert.ok(Math.abs(segment-1/(f.spine.length-1)) < 1e-10,"bending must not stretch bones");
      length+=segment;
      if (k/(f.spine.length-1) < .20) assert.ok(Math.abs(b.y) < 1e-10,"the skull must stay rigid");
    }
    assert.ok(Math.abs(length-1)<1e-10,"arc length stays constant through a turn");
    curl=Math.max(curl,Math.abs(f.spine.at(-1).angle));
    tailMotion=Math.max(tailMotion,Math.abs(f.spine.at(-1).y));
    const mesh=skinMesh(f);
    assert.ok(mesh.flat().every(p=>Number.isFinite(p.x)&&Number.isFinite(p.y)),"the connected skin stays finite");
  });
}
assert.ok(ranges.filter(r => r.min < .009 && r.max > .07).length >= 5,
  "individual fish alternate hovering/coasting with fast bursts");
assert.ok(ranges.every(r => r.bank > .1),"fish bank into organic turns");
assert.ok(ranges.every(r => Math.max(...r.depth)-Math.min(...r.depth) > .2),"swimmers vary their depth");
assert.ok(pond.drops.length > 0,"occasional droplets disturb the surface");
assert.ok(curl>1 && tailMotion>.2,"turning produces substantial flowing bends, not tiny tail offsets");
const invalid = pond.food.length;
assert.equal(pond.feed(0,0),false); assert.equal(pond.feed(NaN,.5),false);
assert.equal(pond.food.length,invalid,"feeding the bank must not create food");
const center = { x:.5, y:.5 };
const distance = () => pond.fish.reduce((sum,f) => sum+Math.hypot(f.x-center.x,f.y-center.y),0)/pond.fish.length;
const initialDistance = distance();
assert.equal(pond.feed(center.x,center.y),true); assert.equal(pond.food.length,8);
let shallowest = Infinity;
for (let i=0;i<240;i++) {
  pond.step(.025);
  shallowest = Math.min(shallowest,pond.fish.reduce((sum,f) => sum+f.depth,0)/pond.fish.length);
}
assert.ok(distance() < initialDistance*.70,"fish gather toward the actual feeding point");
assert.ok(shallowest < .22,
  "feeding fish rise toward the surface");
assert.ok(pond.food.length < 8,"fish reach and eat pellets before they expire");
for (let i=0;i<120;i++) pond.feed(.5,.5);
assert.ok(pond.food.length <= 24 && pond.drops.length <= 12,"rapid clicks do not accumulate unlimited effects");
const paused = JSON.stringify(pond.fish);
pond.step(0); assert.equal(JSON.stringify(pond.fish),paused,"zero-time frames retain the pose");
for (let i=0;i<800;i++) pond.step(.025);
assert.equal(pond.food.length,0,"uneaten food expires and the pond returns to wandering");
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
  [10,17].forEach((joint,j)=>{
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
console.log("Connected spine, skin, momentum, water resistance, stroke thrust and pond behavior pass.");
