const assert = require("node:assert/strict");
const { create, project, unproject, inWater } = require("../static/koi-pond.js");
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
const ranges = pond.fish.map(() => ({ min: 1, max: 0, bank: 0, depth: [] }));
for (let i=0;i<2400;i++) {
  const before = pond.fish.map(f => [f.x,f.y,f.heading]);
  pond.step(.025);
  pond.fish.forEach((f,j) => {
    const r = ranges[j]; r.min = Math.min(r.min,f.speed); r.max = Math.max(r.max,f.speed);
    r.bank = Math.max(r.bank,Math.abs(f.bank)); r.depth.push(f.depth);
    assert.ok(Math.hypot(f.x-before[j][0],f.y-before[j][1]) < .004,"swimmers must not teleport");
    assert.ok(Number.isFinite(f.heading) && Math.abs(f.heading-before[j][2]) < .1,"turns remain smooth");
    assert.ok(Math.hypot((f.x-.5)/.46,(f.y-.5)/.44) <= .940001,"fish stay inside the water");
  });
}
assert.ok(ranges.filter(r => r.min < .009 && r.max > .07).length >= 5,
  "individual fish alternate hovering/coasting with fast bursts");
assert.ok(ranges.every(r => r.bank > .1),"fish bank into organic turns");
assert.ok(ranges.every(r => Math.max(...r.depth)-Math.min(...r.depth) > .2),"swimmers vary their depth");
assert.ok(pond.drops.length > 0,"occasional droplets disturb the surface");
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
console.log("Organic speed, turns, depth, perspective, feeding and bounded water effects pass.");
