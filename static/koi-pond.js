/* The pond's simulation uses water-plane coordinates, independent of pixels,
   rendering and the desktop. A supplied random source makes behavior testable. */
((root) => {
  "use strict";
  const TAU = Math.PI * 2;
  const clamp = (x, low, high) => Math.max(low, Math.min(high, x));
  const angleDiff = (a, b) => Math.atan2(Math.sin(a - b), Math.cos(a - b));
  const radius = (x, y) => Math.hypot((x - .5) / .46, (y - .5) / .44);
  const inWater = (x, y) => Number.isFinite(x) && Number.isFinite(y) && radius(x, y) < .94;
  // Far water compresses in both axes. The inverse is also used for feeding,
  // so a clicked ripple and a fish's destination remain aligned when cropped.
  const project = (x, y) => ({ x: .5 + (x - .5) * (.70 + .30 * y),
    y: .12 + .80 * y / (1.18 - .18 * y) });
  const unproject = (u, v) => {
    const y = (v - .12) * 1.18 / (.80 + .18 * (v - .12));
    return { x: .5 + (u - .5) / (.70 + .30 * y), y };
  };
  function create(random = Math.random) {
    let time = 0, nextDrop = 1.2;
    const fish = [], food = [], drops = [];
    const between = (a, b) => a + (b - a) * random();
    const point = () => {
      const a = random() * TAU, r = Math.sqrt(random()) * .82;
      return { x: .5 + Math.cos(a) * .46 * r, y: .5 + Math.sin(a) * .44 * r };
    };
    const change = f => {
      const r = random();
      f.mode = r < .23 ? "hover" : r < .48 ? "coast" : r < .85 ? "cruise" : "dart";
      f.pace = f.mode === "hover" ? .001 : f.mode === "coast" ? between(.006,.016)
        : f.mode === "cruise" ? between(.025,.050) : between(.085,.13);
      f.timer = f.mode === "dart" ? between(.6,1.4) : between(2.2,5.5);
      if (f.mode !== "hover") f.target = point();
      f.targetDepth = between(.25,1.1);
    };
    const types = [0, 1, 2, 3, 0, 2, 3];
    types.forEach((type, i) => {
      const a = i * TAU / types.length;
      const f = { id: i, type, x: .5 + Math.cos(a) * .27, y: .5 + Math.sin(a) * .26,
        heading: a + Math.PI / 2, speed: .018, bank: 0, turn: 0, tail: i * 1.7,
        size: between(.82,1.15), depth: between(.25,.85), target: point(),
        eating: 0, attention: 0, mode: "cruise", pace: .025, timer: 0, targetDepth: .5 };
      change(f); f.timer = between(.5,3.5); fish.push(f);
    });
    function ripple(x, y, strength) {
      // Twelve ripple uniforms fit the low-power water shader; retire oldest
      // impacts instead of growing GPU or simulation work after repeated clicks.
      if (drops.length === 12) drops.shift();
      drops.push({ x, y, strength, age: 0 });
    }
    function feed(x, y) {
      if (!inWater(x, y)) return false;
      // Three small feedings can coexist; bounding them keeps click bursts cheap.
      while (food.length > 16) food.shift();
      for (let i = 0; i < 8; i++) {
        const a = i * TAU / 8, r = between(.006,.021);
        const px = x + Math.cos(a) * r, py = y + Math.sin(a) * r;
        food.push({ x: px, y: py, life: 15 });
      }
      ripple(x, y, 1);
      fish.forEach(f => { f.attention = between(.12,.9); f.eating = 0; });
      return true;
    }
    function step(delta) {
      // A resumed tab advances from its last simulation state, never by minutes.
      const dt = clamp(delta, 0, .1);
      if (!dt) return;
      time += dt;
      nextDrop -= dt;
      if (nextDrop <= 0) {
        const p = point(); ripple(p.x,p.y,between(.3,.6)); nextDrop = between(1.1,3.8);
      }
      for (let i = drops.length - 1; i >= 0; i--) {
        drops[i].age += dt;
        if (drops[i].age > 3.5) drops.splice(i,1);
      }
      for (let i = food.length - 1; i >= 0; i--) {
        food[i].life -= dt;
        if (food[i].life <= 0) food.splice(i,1);
      }
      for (const f of fish) {
        f.attention = Math.max(0,f.attention-dt);
        f.eating = Math.max(0,f.eating-dt);
        f.timer -= dt;
        if (f.timer <= 0) change(f);
        let pellet = null, distance = Infinity;
        if (!f.attention && !f.eating) {
          for (const p of food) {
            const d = Math.hypot(p.x-f.x,p.y-f.y);
            if (d < distance) { distance = d; pellet = p; }
          }
        }
        let target = f.target, pace = f.eating ? .001 : f.pace;
        let depth = f.targetDepth;
        if (pellet) {
          target = pellet;
          // An accelerating approach becomes a slow nibble near the food.
          pace = distance > .13 ? .115 : distance > .045 ? .040 : .010;
          depth = .07;
          const nose = { x: f.x+Math.cos(f.heading)*.035*f.size,
            y: f.y+Math.sin(f.heading)*.035*f.size };
          if (Math.hypot(nose.x-pellet.x,nose.y-pellet.y) < .022 || distance < .016) {
            food.splice(food.indexOf(pellet),1);
            f.eating = between(.45,.95); pace = .001;
            ripple(pellet.x,pellet.y,.22);
          }
        } else if (f.eating) {
          depth = .10;
        } else if (Math.hypot(target.x-f.x,target.y-f.y) < .055 && f.mode !== "hover") {
          f.target = point(); target = f.target;
        }
        let dx = target.x-f.x, dy = target.y-f.y;
        // A soft shoreline turns fish inward before their bodies meet the bank.
        if (radius(f.x,f.y) > .83) {
          dx += (.5-f.x)*2; dy += (.5-f.y)*2;
        }
        if (!f.eating) for (const other of fish) {
          if (other === f) continue;
          const sx = f.x-other.x, sy = f.y-other.y, d = Math.hypot(sx,sy);
          if (d > .001 && d < .065) {
            const strength = (.065-d)*.8/d;
            dx += sx*strength; dy += sy*strength;
          }
        }
        const desired = Math.atan2(dy,dx);
        const limit = (.35+f.speed*17)*dt;
        const turn = f.eating || f.mode === "hover" && !pellet ? 0
          : clamp(angleDiff(desired,f.heading)*2.6*dt,-limit,limit);
        f.heading += turn;
        f.turn += (turn/dt-f.turn)*(1-Math.exp(-dt*5));
        f.bank += (clamp(f.turn*.45,-.8,.8)-f.bank)*(1-Math.exp(-dt*3));
        const acceleration = pace > f.speed ? 2.8 : 1.6;
        f.speed += (pace-f.speed)*(1-Math.exp(-dt*acceleration));
        f.x += Math.cos(f.heading)*f.speed*dt;
        f.y += Math.sin(f.heading)*f.speed*dt;
        const r = radius(f.x,f.y);
        if (r > .94) { f.x = .5+(f.x-.5)*.94/r; f.y = .5+(f.y-.5)*.94/r; }
        f.depth += (depth-f.depth)*(1-Math.exp(-dt*.8));
        // Slow coasting barely moves the tail; bursts use faster, wider strokes.
        f.tail += dt*(.65+f.speed*58);
      }
    }
    return { fish, food, drops, feed, step, get time() { return time; } };
  }
  const api = { create, project, unproject, inWater };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ViraKoiPond = api;
})(typeof window !== "undefined" ? window : {});
