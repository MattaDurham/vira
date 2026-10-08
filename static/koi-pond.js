/* The pond's simulation uses water-plane coordinates, independent of pixels,
   rendering and the desktop. A supplied random source makes behavior testable. */
((root) => {
  "use strict";
  const TAU = Math.PI * 2;
  // Physical water-plane units use image width. The pond is wider than tall,
  // with modest oblique foreshortening; steering and rendering share this metric.
  const WATER_Y = (16/9)*.76;
  // A fitted photographic camera, looking 50 degrees down into a six-metre
  // pond. Depth is metres below the surface, rather than a cosmetic alpha.
  const lens = { scale:.82, distance:2.6, sin:Math.sin(50*Math.PI/180),
    cos:Math.cos(50*Math.PI/180), centerY:.51, waterY:WATER_Y, width:6 };
  const space={width:6,length:6/WATER_Y,depth:1.8,radiusX:.46*6,radiusZ:.44*6/WATER_Y};
  const world=f=>({x:(f.x-.5)*space.width,y:-f.depth,z:(f.y-.5)*space.length});
  function floorAt(x,z){
    const r=clamp(Math.hypot(x/space.radiusX,z/space.radiusZ),0,1);
    return -space.depth+.24*r*r+1.54*Math.pow(r,8);
  }
  function safeBottom(f){
    // Reserve room for the whole curved/pitched hull, not just its centre.
    const r=radius(f.x,f.y)+.18*f.size;
    return Math.max(.10,-floorAt(r*space.radiusX,0)-.18*f.size);
  }
  function swimRadius(f){
    let low=0,high=1;
    for(let i=0;i<12;i++){
      const mid=(low+high)*.5;
      if(-floorAt(mid*space.radiusX,0)>f.depth+.18*f.size)low=mid;else high=mid;
    }
    return Math.max(.12,low-.18*f.size);
  }
  function bodyPoint(f,s,lateral=0,up=0){
    s=clamp(s,0,1);
    const i=Math.min(BONES-1,Math.max(0,f.spine.findIndex(p=>p.s>=s)-1));
    const t=(s-f.spine[i].s)/segments[i].length;
    const a=f.spine[i],b=f.spine[i+1],angle=a.angle+(b.angle-a.angle)*t;
    const along=a.x+(b.x-a.x)*t-f.center.x,side=a.y+(b.y-a.y)*t-f.center.y;
    const roll=f.bank*(.25+.75*s),pitch=f.pitch || 0,length=.108*f.size*space.width;
    const transverse=lateral*Math.cos(roll)-up*Math.sin(roll);
    const vertical=lateral*Math.sin(roll)+up*Math.cos(roll);
    const x=(along-Math.sin(angle)*transverse)*Math.cos(pitch)+vertical*Math.sin(pitch);
    const z=side+Math.cos(angle)*transverse;
    const y=vertical*Math.cos(pitch)-along*Math.sin(pitch);
    const heading=f.heading+Math.PI,origin=world(f);
    return {x:origin.x+length*(x*Math.cos(heading)-z*Math.sin(heading)),
      y:origin.y+length*y,z:origin.z+length*(x*Math.sin(heading)+z*Math.cos(heading))};
  }
  function bodyRadius(s){
    const trunk=.024+.056*Math.exp(-Math.pow((s-.29)/.24,2));
    return s<.53 ? trunk : Math.max(.012,trunk*(1-(s-.53)/.34));
  }
  function volumeMesh(f,rings=25,sides=18){
    const positions=[],uv=[];
    for(let i=0;i<rings;i++){
      const s=i/(rings-1)*.83,radius=bodyRadius(s);
      for(let j=0;j<=sides;j++){
        const a=j/sides*TAU,p=bodyPoint(f,s,Math.cos(a)*radius,Math.sin(a)*radius*.82);
        positions.push(p.x,p.y,p.z);
        // The photographed dorsal scale pattern wraps onto the solid flank.
        uv.push(.085+s*.94,.5+Math.cos(a)*.115);
      }
    }
    return {positions,uv,rings,sides};
  }
  function skeletalMesh(f){
    const positions=[],colors=[],indices=[];
    const vertex=(s,l,u,tissue=false)=>{
      const p=bodyPoint(f,s,l,u),i=positions.length/3;positions.push(p.x,p.y,p.z);
      colors.push(...(tissue?[.16,.78,.87]:[.92,.85,.68]));return i;
    };
    const beam=(a,b,r=.0018,tissue=false)=>{
      const start=positions.length/3;
      for(const p of [a,b])for(let j=0;j<4;j++){
        const angle=j*Math.PI/2;vertex(p[0],p[1]+Math.cos(angle)*r,p[2]+Math.sin(angle)*r,tissue);
      }
      for(let j=0;j<4;j++){const k=(j+1)%4;indices.push(start+j,start+4+j,start+k,start+k,start+4+j,start+4+k);}
    };
    for(const segment of segments.filter(s=>s.vertebra)){
      const center=segment.s+segment.length*.5,taper=1-center*.48;
      const start=positions.length/3;
      // Biconcave centra, rather than mammalian vertebrae or ball joints.
      const rings=[[-.43,.010],[-.20,.007],[0,.004],[.20,.007],[.43,.010]];
      for(const [along,radius] of rings)for(let j=0;j<8;j++){
        const a=j/8*TAU;vertex(center+along*segment.length,Math.cos(a)*radius*taper,Math.sin(a)*radius*taper);
      }
      for(let i=0;i<4;i++)for(let j=0;j<8;j++){
        const a=start+i*8+j,b=start+i*8+(j+1)%8;indices.push(a,a+8,b,b,a+8,b+8);
      }
      // Flexible intervertebral connective tissue is separate from bone.
      beam([segment.s+.88*segment.length,0,0],[segment.s+segment.length,0,0],.005*taper,true);
      const arch=.024*taper,spine=.052*taper;
      if(segment.vertebra<31){
        beam([center,-.008,.004],[center+.003,-.006,arch]);
        beam([center+.003,-.006,arch],[center+.003,.006,arch]);
        beam([center+.003,.006,arch],[center,.008,.004]);
        beam([center+.003,0,arch],[center+.012,0,spine]);
        if(segment.region==="caudal"){
          beam([center,-.007,-.004],[center+.004,0,-arch]);
          beam([center+.004,0,-arch],[center,.007,-.004]);
          beam([center+.004,0,-arch],[center+.015,0,-spine]);
        }else if(segment.region==="abdominal" && segment.vertebra>4){
          const r=bodyRadius(center);
          for(const side of [-1,1]){
            beam([center,side*.009,0],[center+.007,side*r*.75,-r*.30]);
            beam([center+.007,side*r*.75,-r*.30],[center+.014,side*r*.55,-r*.70]);
          }
        }
      }
    }
    // Terminal hypural supports spread vertically into the caudal fin.
    for(let i=0;i<6;i++)beam([.81,0,0],[.91,0,(i-2.5)*.026],.0024);
    // A rigid skull outline attached to the first centrum.
    for(const side of [-1,1]){
      beam([.025,0,0],[.09,side*.033,.02],.004);
      beam([.09,side*.033,.02],[.19,side*.046,0],.004);
      beam([.19,side*.046,0],[.20,0,0],.004);
    }
    return {positions,colors,indices};
  }
  // Snell: apparent vertical depth at the centre of this air/water view.
  const refractedSin=lens.cos/1.333;
  const apparentDepth=(refractedSin/Math.sqrt(1-refractedSin*refractedSin))/(lens.cos/lens.sin);
  const clamp = (x, low, high) => Math.max(low, Math.min(high, x));
  const angleDiff = (a, b) => Math.atan2(Math.sin(a - b), Math.cos(a - b));
  const radius = (x, y) => Math.hypot((x - .5) / .46, (y - .5) / .44);
  const inWater = (x, y) => Number.isFinite(x) && Number.isFinite(y) && radius(x, y) < .94;
  // Far water compresses in both axes. The inverse is also used for feeding,
  // so a clicked ripple and a fish's destination remain aligned when cropped.
  const project = (x, y, depth=0) => {
    const X=x-.5, Y=(y-.5)/WATER_Y, Z=-depth/lens.width*apparentDepth;
    const q=lens.distance-Y*lens.cos-Z*lens.sin;
    const scale=lens.scale*lens.distance/q;
    return { x:.5+X*scale, y:lens.centerY+(Y*lens.sin-Z*lens.cos)*scale*(16/9) };
  };
  const unproject = (u, v) => {
    const t=(v-lens.centerY)/((16/9)*lens.scale);
    const Y=t*lens.distance/(lens.distance*lens.sin+t*lens.cos);
    return { x:.5+(u-.5)*(lens.distance-Y*lens.cos)/(lens.scale*lens.distance),
      y:.5+Y*WATER_Y };
  };
  const camera = (x,y,photoRatio=16/9,depth=0) => {
    const X=x-.5,Y=(y-.5)/WATER_Y,Z=-depth/lens.width*apparentDepth;
    const q=lens.distance-Y*lens.cos-Z*lens.sin,k=lens.scale*lens.distance;
    return { xx:k/q, yx:0, xy:k*X*lens.cos/(q*q),
      yy:k*(lens.sin*lens.distance-Z)/(q*q)*(16/9)/photoRatio };
  };
  function optics(depth) {
    const path=Math.max(0,depth)/Math.sqrt(1-refractedSin*refractedSin);
    // Pond turbidity and illumination are fitted, not measured. Exponential
    // extinction reduces contrast; wavelength-dependent absorption changes hue.
    return { path, clarity:.96*Math.exp(-.95*path),
      transmission:[Math.exp(-.75*path),Math.exp(-.28*path),Math.exp(-.43*path)],
      blur:.0003+.012*(1-Math.exp(-1.2*path)) };
  }
  const separation = (x,y) => Math.hypot(x,y/WATER_Y);
  // Salinas et al.'s representative common-carp column has 34 centra, with
  // abdominal V1-11, transition V12-17 and caudal V18-34. The skull and fin
  // are separate rigid/passive structures, not extra vertebrae. Dimensions,
  // spring constants and individual bone contours remain visual estimates.
  const anatomy={vertebrae:34,abdominal:11,transition:6,caudal:17};
  const segments=[{s:0,length:.20,region:"skull"},
    ...Array.from({length:34},(_,i)=>({s:.20+i*.63/34,length:.63/34,
      region:i<11?"abdominal":i<17?"transition":"caudal",vertebra:i+1})),
    {s:.83,length:.17,region:"caudal-fin"}];
  const BONES=segments.length;
  function buildSpine(f) {
    let x = 0, y = 0, angle = 0;
    f.spine = [{ x, y, angle,s:0 }];
    for (let i = 0; i < BONES; i++) {
      angle += f.joints[i];
      const segment=segments[i];
      x += Math.cos(angle)*segment.length; y += Math.sin(angle)*segment.length;
      f.spine.push({ x, y, angle,s:segment.s+segment.length });
    }
    // Mass is concentrated in the trunk. This keeps bending about the body,
    // rather than making the entire fish orbit its nose as the tail curls.
    let mass = 0, cx = 0, cy = 0;
    f.spine.forEach((p,i) => {
      const s = p.s, weight = .12+Math.exp(-Math.pow((s-.38)/.25,2));
      mass += weight; cx += p.x*weight; cy += p.y*weight;
    });
    f.center = { x: cx/mass, y: cy/mass };
  }
  function flex(f, dt) {
    const effort = Math.sqrt(clamp(f.drive/.19,0,1));
    f.tail += dt*TAU*(.18+2.35*effort)/Math.sqrt(f.size);
    const amplitude = .04+.96*effort;
    for (let i = 0; i < BONES; i++) {
      const segment=segments[i],s=segment.s+segment.length*.5;
      // Skull stays rigid. Flexion spreads through the trunk and becomes
      // strongest at the narrow peduncle. The fin trails its attachment.
      const mobile = clamp((s-.20)/.18,0,1);
      const wave = Math.sin(f.tail-TAU*.92*s);
      const muscle = (.22+3.1*s*s)*amplitude*wave;
      const steering = -f.bend*2.7*Math.sin(Math.PI*clamp((s-.2)/.8,0,1));
      const target = mobile*(muscle+steering)*segment.length;
      const stiffness = segment.region==="caudal-fin"?55:170-75*s, damping = 19-7*s;
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
      const s = p.s;
      const next = f.spine[Math.min(BONES,i+1)];
      const angle = i === BONES ? p.angle : (p.angle+next.angle)/2;
      const roll = f.bank*(.25+.75*s);
      const fins = Math.exp(-Math.pow((s-.26)/.13,2));
      return [0,.5,1].map(v => {
        const fin = v < .5 ? f.finLeft : f.finRight;
        const offset = (v-.5)*aspect*Math.cos(roll)*(1+fins*(fin-1));
        const dorsal=.12*Math.sin(Math.PI*s)*Math.max(0,1-Math.pow((v-.5)*2,2));
        return { u:s, v, x:p.x-f.center.x-Math.sin(angle)*offset,
          y:p.y-f.center.y+Math.cos(angle)*offset,
          z:dorsal*Math.cos(roll)+(v-.5)*aspect*Math.sin(roll) };
      });
    });
  }
  function projectedMesh(f,aspect=.5) {
    const length=.108*f.size, cosine=Math.cos(f.heading+Math.PI),sine=Math.sin(f.heading+Math.PI);
    const pitch=f.pitch || 0;
    return skinMesh(f,aspect).map(column=>column.map(p=>{
      const along=p.x*Math.cos(pitch), up=p.z*Math.cos(pitch)-p.x*Math.sin(pitch);
      const x=f.x+length*(cosine*along-sine*p.y);
      const y=f.y+length*(sine*along+cosine*p.y)*WATER_Y;
      const depth=Math.max(.025,f.depth-up*length*lens.width);
      const screen=project(x,y,depth);
      return { u:p.u,v:p.v,x:screen.x,y:screen.y,depth };
    }));
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
      f.targetDepth = between(.22,1.5);
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
        size: between(.82,1.15), depth: between(.18,1.2), depthVelocity:0, pitch:0, pitchVelocity:0,target: point(),
        eating: 0, attention: 0, mode: "cruise", pace: .025, timer: 0, targetDepth: .5 };
      change(f); f.timer = between(.5,3.5);f.depth=Math.min(f.depth,safeBottom(f)-.03);buildSpine(f);fish.push(f);
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
        let px = x + Math.cos(a) * r, py = y + Math.sin(a) * r;
        const edge = radius(px,py);
        if (edge > .92) { px=.5+(px-.5)*.92/edge; py=.5+(py-.5)*.92/edge; }
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
          depth = .10;
          const nose = { x: f.x+Math.cos(f.heading)*.035*f.size,
            y: f.y+Math.sin(f.heading)*.035*f.size*WATER_Y };
          if (f.depth < .22 && (separation(nose.x-pellet.x,nose.y-pellet.y) < .022 || distance < .016)) {
            food.splice(food.indexOf(pellet),1);
            f.eating = between(.45,.95); pace = .001;
            ripple(pellet.x,pellet.y,.22);
          }
        } else if (f.eating) {
          depth = .10;
        } else if (separation(target.x-f.x,target.y-f.y) < .055 && f.mode !== "hover") {
          f.target = point(); target = f.target;
        }
        const bottom=safeBottom(f);
        depth=Math.min(depth,Math.max(.10,bottom-.15));
        const depthError=f.depth-depth;
        // A deep fish makes an ascending approach around food, rather than
        // stopping underneath it and being lifted vertically by a depth spring.
        if(pellet && distance<.075 && f.depth>.25){
          const a=Math.atan2((f.y-pellet.y)/WATER_Y,f.x-pellet.x)+.8;
          target={x:pellet.x+Math.cos(a)*.055,y:pellet.y+Math.sin(a)*.055*WATER_Y};
          pace=Math.max(pace,.055);
        }else if(!f.eating && f.mode!=="hover" && Math.abs(depthError)>.18)pace=Math.max(pace,.035);
        const desiredPitch=f.eating?0:clamp(Math.atan2(depthError*.85,Math.max(.35,f.speed*space.width)),-.56,.56);
        f.pitchVelocity+=((desiredPitch-f.pitch)*9-f.pitchVelocity*5)*dt;
        f.pitch=clamp(f.pitch+f.pitchVelocity*dt,-.60,.60);
        let dx = target.x-f.x, dy = (target.y-f.y)/WATER_Y;
        // A soft shoreline turns fish inward before their bodies meet the bank.
        const shore=swimRadius(f);
        if (radius(f.x,f.y) > shore-.12) {
          const strength=pellet ? 1 : 3;
          dx += (.5-f.x)*strength; dy += (.5-f.y)*strength/WATER_Y;
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
        f.angularVelocity += (torque-f.angularVelocity)*3.5*dt/(f.size*f.size);
        f.heading += f.angularVelocity*dt;
        f.turn = f.angularVelocity;
        f.bank += (clamp(f.turn*.34,-.65,.65)-f.bank)*(1-Math.exp(-dt*3));
        // Muscle effort responds before velocity. Tail strokes supply pulsed
        // thrust; a fish with relaxed muscles still coasts on its momentum.
        const power = pace < .004 ? 0 : pace*(.55+8*pace);
        f.drive += (power-f.drive)*(1-Math.exp(-dt*(power > f.drive ? 4 : 7)));
        flex(f,dt);
        const tailRate = f.jointVelocity.slice(-8,-1).reduce((sum,v) => sum+v,0)*2.25;
        const pulse = .25+1.5*clamp(tailRate*tailRate/1.4,0,1);
        f.thrust = f.drive*pulse*(loading ? .15 : stroke ? 2 : 1);
        const hx = Math.cos(f.heading), hy = Math.sin(f.heading);
        const forward = f.vx*hx+f.vy*hy, lateral = -f.vx*hy+f.vy*hx;
        // Sideways resistance exceeds forward drag: the water grips the
        // flank, while longitudinal momentum outlasts a stopped tail stroke.
        const drag = forward*(.55+8*Math.abs(forward));
        const sideDrag = lateral*(4.5+18*Math.abs(lateral));
        // Force scales roughly with area and effective mass with volume;
        // larger swimmers therefore accelerate more gradually (including
        // entrained water in the tuned effective mass).
        const forwardThrust=f.thrust*Math.cos(f.pitch);
        f.vx += ((forwardThrust-drag)*hx+sideDrag*hy)*dt/f.size;
        f.vy += ((forwardThrust-drag)*hy-sideDrag*hx)*dt/f.size;
        f.speed = Math.hypot(f.vx,f.vy);
        // The tuned burst envelope tops out at 0.9 m/s in this six-metre scene.
        if(f.speed>.15){f.vx*=.15/f.speed;f.vy*=.15/f.speed;f.speed=.15;}
        f.x += f.vx*dt; f.y += f.vy*WATER_Y*dt;
        const r = radius(f.x,f.y);
        if (r > shore) {
          f.x = .5+(f.x-.5)*shore/r; f.y = .5+(f.y-.5)*shore/r;
          // Remove only outward momentum at the bank; retain tangential glide.
          const nx = (f.x-.5)/(.46*.46), ny = (f.y-.5)*WATER_Y/(.44*.44);
          const outward = Math.max(0,(f.vx*nx+f.vy*ny)/(nx*nx+ny*ny));
          f.vx -= outward*nx; f.vy -= outward*ny;
          f.speed = Math.hypot(f.vx,f.vy);
        }
        // Tail thrust acts along the pitched body in three dimensions. Neutral
        // buoyancy adds no automatic elevator force when the tail relaxes.
        const verticalDrag=f.depthVelocity*(1.2+2*Math.abs(f.depthVelocity));
        f.depthVelocity=clamp(f.depthVelocity+(-f.thrust*space.width*Math.sin(f.pitch)-verticalDrag)*dt/f.size,-.42,.32);
        const nextBottom=safeBottom(f);
        f.depth=clamp(f.depth+f.depthVelocity*dt,.085,Math.max(.10,nextBottom));
        if(f.depth>=nextBottom && f.depthVelocity>0)f.depthVelocity=0;
        if(f.depth<=.085 && f.depthVelocity<0)f.depthVelocity=0;
      }
    }
    function step(delta) {
      // Bounded 120 Hz substeps keep springs stable at both 30 and 60 fps.
      // A resumed tab advances from its last state, never by hidden minutes.
      const dt = clamp(delta,0,.25), count = Math.ceil(dt*120);
      for (let i=0;i<count;i++) advance(dt/count);
    }
    return { fish, food, drops, feed, step, get time() { return time; } };
  }
  const api = { create, project, unproject, inWater, skinMesh, projectedMesh, camera, optics, lens,
    space, world, floorAt, bodyPoint, bodyRadius, volumeMesh,skeletalMesh,anatomy,segments };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ViraKoiPond = api;
})(typeof window !== "undefined" ? window : {});
