/* The pond's simulation uses water-plane coordinates, independent of pixels,
   rendering and the desktop. A supplied random source makes behavior testable. */
((root) => {
  "use strict";
  const TAU = Math.PI * 2;
  // Physical water-plane units use image width. The pond is wider than tall,
  // with modest oblique foreshortening; steering and rendering share this metric.
  const WATER_Y = (16/9)*.76;
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
  const camera = (x,y,photoRatio=16/9) => ({ xx:.70+.30*y, yx:0,
    xy:.30*(x-.5)*WATER_Y,
    yy:.80*1.18/Math.pow(1.18-.18*y,2)*WATER_Y/photoRatio });
  const separation = (x,y) => Math.hypot(x,y/WATER_Y);
  // A reduced articulated backbone, NOT a literal vertebral count. Regional
  // rigidity, muscle waves and water damping are based on the carp studies
  // linked in backgrounds/kinematics.md; coefficients are tuned for this pond.
  const BONES = 24;
  function buildSpine(f) {
    let x = 0, y = 0, angle = 0;
    f.spine = [{ x, y, angle }];
    for (let i = 0; i < BONES; i++) {
      angle += f.joints[i];
      x += Math.cos(angle)/BONES; y += Math.sin(angle)/BONES;
      f.spine.push({ x, y, angle });
    }
    // Mass is concentrated in the trunk. This keeps bending about the body,
    // rather than making the entire fish orbit its nose as the tail curls.
    let mass = 0, cx = 0, cy = 0;
    f.spine.forEach((p,i) => {
      const s = i/BONES, weight = .12+Math.exp(-Math.pow((s-.38)/.25,2));
      mass += weight; cx += p.x*weight; cy += p.y*weight;
    });
    f.center = { x: cx/mass, y: cy/mass };
  }
  function flex(f, dt) {
    const effort = Math.sqrt(clamp(f.drive/.19,0,1));
    f.tail += dt*TAU*(.18+2.35*effort);
    const amplitude = .04+.96*effort;
    for (let i = 0; i < BONES; i++) {
      const s = (i+.5)/BONES;
      // Skull stays rigid. Flexion spreads through the trunk and becomes
      // strongest at the narrow peduncle. The fin trails its attachment.
      const mobile = clamp((s-.20)/.18,0,1);
      const wave = Math.sin(f.tail-TAU*.92*s);
      const muscle = (.22+3.1*s*s)*amplitude*wave;
      const steering = -f.bend*2.7*Math.sin(Math.PI*clamp((s-.2)/.8,0,1));
      const target = mobile*(muscle+steering)/BONES;
      const stiffness = 170-75*s, damping = 19-7*s;
      f.jointVelocity[i] += ((target-f.joints[i])*stiffness
        -f.jointVelocity[i]*damping)*dt;
      f.joints[i] = clamp(f.joints[i]+f.jointVelocity[i]*dt,-.24,.24);
    }
    f.finPhase += dt*(2.2+effort*1.3);
    f.finLeft = .88+.12*Math.sin(f.finPhase)+clamp(f.bend*.12,-.14,.14);
    f.finRight = .88+.12*Math.sin(f.finPhase+.6)-clamp(f.bend*.12,-.14,.14);
    buildSpine(f);
  }
  function skinMesh(f, aspect = .5) {
    // Three rows let both pectoral fins move independently of the midline.
    // Each column's skin lies NORMAL to its bone, instead of shifting an
    // upright image strip. Bone arc length and scale texture stay continuous.
    return f.spine.map((p,i) => {
      const s = i/BONES;
      const next = f.spine[Math.min(BONES,i+1)];
      const angle = i === BONES ? p.angle : (p.angle+next.angle)/2;
      const roll = f.bank*(.25+.75*s);
      const fins = Math.exp(-Math.pow((s-.26)/.13,2));
      return [0,.5,1].map(v => {
        const fin = v < .5 ? f.finLeft : f.finRight;
        const offset = (v-.5)*aspect*Math.cos(roll)*(1+fins*(fin-1));
        return { u:s, v, x:p.x-f.center.x-Math.sin(angle)*offset,
          y:p.y-f.center.y+Math.cos(angle)*offset };
      });
    });
  }
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
        vx: Math.cos(a+Math.PI/2)*.018, vy: Math.sin(a+Math.PI/2)*.018,
        angularVelocity: 0, bend: 0, bendVelocity: 0, drive: .006,
        joints: Array(BONES).fill(0), jointVelocity: Array(BONES).fill(0),
        finPhase: i*2.1, finLeft: 1, finRight: 1, thrust: 0,
        startAge: 1, startSign: 0, startCooldown: 0,
        size: between(.82,1.15), depth: between(.25,.85), target: point(),
        eating: 0, attention: 0, mode: "cruise", pace: .025, timer: 0, targetDepth: .5 };
      change(f); f.timer = between(.5,3.5); buildSpine(f); fish.push(f);
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
    function advance(dt) {
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
            const d = separation(p.x-f.x,p.y-f.y);
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
            y: f.y+Math.sin(f.heading)*.035*f.size*WATER_Y };
          if (separation(nose.x-pellet.x,nose.y-pellet.y) < .022 || distance < .016) {
            food.splice(food.indexOf(pellet),1);
            f.eating = between(.45,.95); pace = .001;
            ripple(pellet.x,pellet.y,.22);
          }
        } else if (f.eating) {
          depth = .10;
        } else if (separation(target.x-f.x,target.y-f.y) < .055 && f.mode !== "hover") {
          f.target = point(); target = f.target;
        }
        let dx = target.x-f.x, dy = (target.y-f.y)/WATER_Y;
        // A soft shoreline turns fish inward before their bodies meet the bank.
        if (radius(f.x,f.y) > .83) {
          dx += (.5-f.x)*2; dy += (.5-f.y)*2/WATER_Y;
        }
        if (!f.eating) for (const other of fish) {
          if (other === f) continue;
          const sx = f.x-other.x, sy = (f.y-other.y)/WATER_Y, d = Math.hypot(sx,sy);
          if (d > .001 && d < .065) {
            const strength = (.065-d)*.8/d;
            dx += sx*strength; dy += sy*strength;
          }
        }
        const error = angleDiff(Math.atan2(dy,dx),f.heading);
        const resting = f.eating || f.mode === "hover" && !pellet;
        f.startCooldown = Math.max(0,f.startCooldown-dt);
        if (!resting && pace-f.speed > .045 && Math.abs(error) > .8 && !f.startCooldown) {
          // A bend loads first; the subsequent counterstroke supplies the burst.
          f.startAge = 0; f.startSign = Math.sign(error); f.startCooldown = 2;
        }
        f.startAge += dt;
        const loading = f.startAge < .18;
        const stroke = f.startAge >= .18 && f.startAge < .48;
        const impulse = loading ? 1.4*f.startSign*Math.sin(Math.PI*f.startAge/.36)
          : stroke ? -.35*f.startSign*Math.sin(Math.PI*(f.startAge-.18)/.30) : 0;
        const bend = resting ? 0 : clamp(error*.75,-1.2,1.2)+impulse;
        f.bendVelocity += ((bend-f.bend)*35-f.bendVelocity*10)*dt;
        f.bend = clamp(f.bend+f.bendVelocity*dt,-1.8,1.8);
        const torque = f.bend*(.6+f.speed*13);
        f.angularVelocity += (torque-f.angularVelocity)*3.5*dt;
        f.heading += f.angularVelocity*dt;
        f.turn = f.angularVelocity;
        f.bank += (clamp(f.turn*.34,-.65,.65)-f.bank)*(1-Math.exp(-dt*3));
        // Muscle effort responds before velocity. Tail strokes supply pulsed
        // thrust; a fish with relaxed muscles still coasts on its momentum.
        const power = pace < .004 ? 0 : pace*(.55+8*pace);
        f.drive += (power-f.drive)*(1-Math.exp(-dt*(power > f.drive ? 4 : 7)));
        flex(f,dt);
        const tailRate = f.jointVelocity.slice(-6).reduce((sum,v) => sum+v,0);
        const pulse = .25+1.5*clamp(tailRate*tailRate/1.4,0,1);
        f.thrust = f.drive*pulse*(loading ? .15 : stroke ? 2 : 1);
        const hx = Math.cos(f.heading), hy = Math.sin(f.heading);
        const forward = f.vx*hx+f.vy*hy, lateral = -f.vx*hy+f.vy*hx;
        // Sideways resistance exceeds forward drag: the water grips the
        // flank, while longitudinal momentum outlasts a stopped tail stroke.
        const drag = forward*(.55+8*Math.abs(forward));
        const sideDrag = lateral*(4.5+18*Math.abs(lateral));
        f.vx += ((f.thrust-drag)*hx+sideDrag*hy)*dt;
        f.vy += ((f.thrust-drag)*hy-sideDrag*hx)*dt;
        f.speed = Math.hypot(f.vx,f.vy);
        f.x += f.vx*dt; f.y += f.vy*WATER_Y*dt;
        const r = radius(f.x,f.y);
        if (r > .94) {
          f.x = .5+(f.x-.5)*.94/r; f.y = .5+(f.y-.5)*.94/r;
          // Remove only outward momentum at the bank; retain tangential glide.
          const nx = (f.x-.5)/(.46*.46), ny = (f.y-.5)*WATER_Y/(.44*.44);
          const outward = Math.max(0,(f.vx*nx+f.vy*ny)/(nx*nx+ny*ny));
          f.vx -= outward*nx; f.vy -= outward*ny;
          f.speed = Math.hypot(f.vx,f.vy);
        }
        f.depth += (depth-f.depth)*(1-Math.exp(-dt*.8));
      }
    }
    function step(delta) {
      // Bounded 120 Hz substeps keep springs stable at both 30 and 60 fps.
      // A resumed tab advances from its last state, never by hidden minutes.
      const dt = clamp(delta,0,.1), count = Math.ceil(dt*120);
      for (let i=0;i<count;i++) advance(dt/count);
    }
    return { fish, food, drops, feed, step, get time() { return time; } };
  }
  const api = { create, project, unproject, inWater, skinMesh, camera };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ViraKoiPond = api;
})(typeof window !== "undefined" ? window : {});
