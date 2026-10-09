// Run the actual scene controller against a small DOM and deterministic frame clock.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Target {
  constructor() { this.listeners = new Map(); }
  addEventListener(type, fn) { if (!this.listeners.has(type)) this.listeners.set(type, new Set()); this.listeners.get(type).add(fn); }
  removeEventListener(type, fn) { this.listeners.get(type)?.delete(fn); }
  emit(type, extra = {}) { for (const fn of this.listeners.get(type) || []) fn({ target: this, ...extra }); }
}
let draws = 0, poses = [];
let gpuEnabled = false, gpuDraws = [], uploads = 0, disposed = 0;
const gl = { program:null, buffer:null, enabled:new Set(), pointers:new Map() };
["VERTEX_SHADER","FRAGMENT_SHADER","COMPILE_STATUS","LINK_STATUS","ARRAY_BUFFER","STATIC_DRAW",
 "DYNAMIC_DRAW","FLOAT","TEXTURE_2D","TEXTURE_MIN_FILTER","TEXTURE_MAG_FILTER","LINEAR",
 "TEXTURE_WRAP_S","TEXTURE_WRAP_T","CLAMP_TO_EDGE","RGB","RGBA","UNSIGNED_BYTE",
 "TEXTURE1","TEXTURE2","FRAMEBUFFER","COLOR_ATTACHMENT0","FRAMEBUFFER_COMPLETE","COLOR_BUFFER_BIT",
 "BLEND","SRC_ALPHA","ONE_MINUS_SRC_ALPHA","ONE","TRIANGLES"].forEach((name,i)=>{gl[name]=i+1;});
Object.assign(gl,{
  createShader:()=>({}),shaderSource:(s,source)=>{s.source=source;},compileShader(){},getShaderParameter:()=>true,
  createProgram:()=>({shaders:[]}),attachShader:(p,s)=>p.shaders.push(s),linkProgram(){},getProgramParameter:()=>true,
  useProgram:p=>{gl.program=p;},createBuffer:()=>({}),bindBuffer:(_,b)=>{gl.buffer=b;},
  bufferData:(_,data)=>{gl.buffer.data=data;},getAttribLocation:(_,name)=>name==="a"?0:1,
  enableVertexAttribArray:i=>gl.enabled.add(i),disableVertexAttribArray:i=>gl.enabled.delete(i),
  vertexAttribPointer:(i,size)=>gl.pointers.set(i,{buffer:gl.buffer,size}),
  createTexture:()=>({}),bindTexture(){},texParameteri(){},texImage2D(){uploads++;},activeTexture(){},
  getUniformLocation:(program,name)=>({program,name}),
  uniform1i(location){assert.equal(gl.program,location.program);},
  uniform1f(location){assert.equal(gl.program,location.program);},
  uniform2f(location){assert.equal(gl.program,location.program);},
  uniform3fv(location){assert.equal(gl.program,location.program);},
  uniform4fv(location){assert.equal(gl.program,location.program);},
  createFramebuffer:()=>({}),bindFramebuffer(){},framebufferTexture2D(){},checkFramebufferStatus:()=>gl.FRAMEBUFFER_COMPLETE,
  viewport(){},clearColor(){},clear(){},enable(){},disable(){},blendFuncSeparate(){},isContextLost:()=>gl.lost===true,
  drawArrays(_,first,count){
    const skin=gl.program.shaders.some(s=>s.source.includes("attribute vec4 vertex"));
    const attribute=skin?1:0, pointer=gl.pointers.get(attribute);
    assert.deepEqual([...gl.enabled],[attribute],"passes must restore their vertex attribute state");
    assert.equal(pointer.size,skin?4:2);
    assert.ok(pointer.buffer.data.length>=(first+count)*pointer.size,"the draw uses its own complete buffer");
    assert.ok([...pointer.buffer.data].every(Number.isFinite),"mesh vertices must stay finite on the GPU path");
    gpuDraws.push(skin?"skin":"water");
  },
  getExtension:()=>({loseContext(){}}),deleteProgram(){disposed++;},deleteBuffer(){},deleteTexture(){},deleteShader(){},deleteFramebuffer(){},
});
const context = { clearRect() {}, save() {}, restore() {}, rotate() {},
  transform() {}, scale() {},
  translate(x, y) { poses.push([x, y]); }, beginPath() {}, ellipse() {}, fill() {},
  moveTo() {}, lineTo() {}, closePath() {}, clip() {},
  stroke() {}, arc() {}, fillRect() {}, drawImage() { draws++; } };
class Element extends Target {
  constructor(tag) { super(); this.tagName = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.style = { setProperty() {} }; }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  append(...children) { children.forEach(c => this.appendChild(c)); }
  prepend(c) { c.parent = this; this.children.unshift(c); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(c => c !== this); }
  setAttribute(k, v) { this.attrs[k] = v; }
  contains(c) { return c === this || this.children.some(n => n.contains(c)); }
  closest() { return this.tagName === "button" || this.control ? this : null; }
  focus() {}
  matches(s) {
    if (s.startsWith(".")) return this.className === s.slice(1);
    if (s === "[data-scene]") return !!this.dataset.scene;
    if (s.startsWith("[data-scene=")) return this.dataset.scene === s.split('"')[1];
    return this.tagName === s;
  }
  querySelectorAll(s) { return this.children.flatMap(c => [...(c.matches(s) ? [c] : []), ...c.querySelectorAll(s)]); }
  querySelector(s) { return this.querySelectorAll(s)[0]; }
  set innerHTML(html) {
    // Only the fixed picker skeleton needs parsing in this harness.
    for (const cls of ["background-options", "background-simple", "background-motion", "background-explore", "background-look-setting", "background-status", "background-close"]) {
      const c = new Element(cls.includes("motion") || cls.includes("close") ? "button" : "div");
      c.className = cls; this.appendChild(c);
    }
    this.appendChild(new Element("input"));
    const look=new Element("select");look.className="background-look-select";this.appendChild(look);
  }
  getContext(kind) { return kind === "2d" ? context : gpuEnabled ? gl : null; }
}
const document = new Target(); document.body = new Element("body"); document.hidden = false;
document.createElement = tag => new Element(tag);
const trigger = new Element("button"); trigger.id = "background-btn"; document.body.appendChild(trigger);
document.getElementById = id => id === trigger.id ? trigger : undefined;
const reduced = new Target(); reduced.matches = false;
const windowEvents = new Target(), frames = new Map(), pendingImages = new Map();
let nextFrame = 0, clock = 0, stored, stops = 0, constellations = 0;
let pondFactory=async()=>null;
class Image {
  constructor() { this.width = 1600; this.height = 900; }
  set src(src) { pendingImages.set(src.split("/").pop(), this); }
}
const sandbox = { window: { ViraKoiPond: require("../static/koi-pond.js") }, document, matchMedia: () => reduced, Image,
  innerWidth: 1280, innerHeight: 720, devicePixelRatio: 1,
  requestAnimationFrame: fn => { frames.set(++nextFrame, fn); return nextFrame; },
  cancelAnimationFrame: id => frames.delete(id),
  addEventListener: (...args) => windowEvents.addEventListener(...args),
  removeEventListener: (...args) => windowEvents.removeEventListener(...args), console };
vm.runInNewContext(fs.readFileSync("static/desktop-backgrounds.js", "utf8"), sandbox);
const flush = () => new Promise(resolve => setImmediate(resolve));
const resolve = name => { const image = pendingImages.get(name); assert.ok(image, name); image.onload(); };
const panel = () => document.body.children.find(c => c.id === "background-picker");
const scene = id => panel().querySelector(`[data-scene="${id}"]`).emit("click");
const host = () => document.body.children.find(c => c.id === "desktop-background");
const frame = () => { clock += 40; const work = [...frames.values()]; frames.clear(); work.forEach(fn => fn(clock)); };
(async () => {
  sandbox.window.ViraBackgrounds.init({ read: () => ({ scene: "unknown", dim: 100 }),
    pond3D:()=>pondFactory(),
    canInteract: target => target === document.body,
    write: (key, value) => { assert.equal(key, "vira-background"); stored = { ...value }; },
    constellation: () => { constellations++; return () => { stops++; }; } });
  assert.equal(constellations, 1); assert.equal(pendingImages.size, 0);
  scene("koi"); scene("redwoods");
  assert.equal(stops, 1);
  resolve("open-water.svg"); resolve("kohaku.webp"); resolve("ogon.webp");
  resolve("showa.webp"); resolve("shusui.webp"); await flush();
  assert.equal(host().children.length, 0, "a stale load must not install its animation");
  resolve("redwoods.jpg"); await flush();
  assert.equal(host().children.length, 1); assert.equal(frames.size, 1);
  assert.match(panel().querySelector(".background-status").textContent, /effects unavailable/);
  scene("koi"); await flush();
  assert.equal(frames.size, 1, "switching must dispose the old loop");
  assert.equal(document.listeners.get("visibilitychange").size, 1);
  assert.equal(windowEvents.listeners.get("resize").size, 1);
  frame(); frame(); assert.ok(draws > 0);
  const rendered = draws, motion = panel().querySelector(".background-motion");
  const before = poses.at(-1);
  motion.emit("click"); assert.equal(frames.size, 0); assert.equal(stored.paused, true);
  frame(); assert.equal(draws, rendered, "pause must stop painting");
  motion.emit("click"); frame();
  assert.deepEqual(poses.at(-1), before, "resume must retain the paused simulation time");
  assert.equal(frames.size, 1);
  assert.equal(document.listeners.get("click").size,1,"the pond installs one ripple handler");
  assert.equal(document.listeners.get("pointermove").size,1,"cursor attention has a single listener");
  document.emit("pointermove",{target:document.body,clientX:720,clientY:390});frame();
  document.emit("pointerleave");
  let prevented = false;
  const click = { button: 0, clientX: 640, clientY: 360,
    preventDefault() { prevented = true; }, defaultPrevented: false };
  const prior = draws;
  document.emit("click", { ...click, target: trigger });
  assert.equal(draws, prior, "window controls must not disturb or repaint the pond");
  document.emit("click", { ...click, target: document.body });
  assert.equal(prevented, true); assert.ok(draws > prior, "ripples paint immediately");
  prevented = false;
  document.emit("dblclick", { ...click, target: document.body });
  assert.equal(prevented, true, "rapid water flicks must protect the desktop's double-click gesture");
  motion.emit("click");
  const pausedDraws = draws;
  document.emit("click", { ...click, target: document.body });
  assert.equal(draws, pausedDraws, "ripples must respect paused motion");
  motion.emit("click");
  document.hidden = true; document.emit("visibilitychange"); assert.equal(frames.size, 0);
  document.hidden = false; document.emit("visibilitychange"); assert.equal(frames.size, 1);
  reduced.matches = true; reduced.emit("change"); assert.equal(frames.size, 0);
  assert.equal(motion.disabled, true); assert.ok(host(), "reduced motion retains the scene");
  reduced.matches = false; reduced.emit("change"); assert.equal(frames.size, 1);
  const range = panel().querySelector("input"); range.value = "35"; range.emit("input");
  assert.equal(stored.dim, .35);
  scene("none"); assert.equal(host(), undefined); assert.equal(frames.size, 0);
  assert.equal(document.listeners.get("pointermove").size,0,"leaving the pond retires cursor attraction");
  assert.equal(document.listeners.get("visibilitychange").size, 0);
  assert.equal(windowEvents.listeners.get("resize").size, 0);
  assert.equal(document.listeners.get("click").size, 0);
  assert.equal(document.listeners.get("dblclick").size, 0);
  scene("aurora"); pendingImages.get("aurora.jpg").onerror(); await flush();
  assert.match(panel().querySelector(".background-status").textContent, /Could not load/);
  scene("aurora"); resolve("aurora.jpg"); await flush(); assert.equal(frames.size, 1);
  scene("none");
  gpuEnabled=true;
  const cpuDraws=draws;
  scene("koi");await flush();
  assert.equal(host().children.length,2,"the GPU plane and surface overlay are installed together");
  assert.equal(gpuDraws.filter(kind=>kind==="skin").length,14,"seven connected skins and shadows draw into the water target");
  assert.equal(gpuDraws.at(-1),"water","surface refraction composites after the submerged fish");
  const loadedUploads=uploads;
  frame();frame();
  assert.equal(uploads,loadedUploads,"moving fish update vertices without reuploading sprite pixels");
  assert.equal(draws,cpuDraws,"the GPU path must not rasterize the fish on the CPU");
  gl.lost=true;host().children[0].emit("webglcontextlost");frame();
  assert.match(panel().querySelector(".background-status").textContent,/context lost/);
  assert.ok(draws>cpuDraws,"context loss retains animated fish above the photograph");
  scene("none");
  assert.equal(disposed,2,"both water and skin programs are released");
  assert.equal(frames.size,0);
  let release, allocations=0, retired=0, callbacks, engine;
  pondFactory=()=>new Promise(resolve=>{release=resolve;});
  scene("koi");await flush();scene("none");
  release({create:()=>{allocations++;}});await flush();
  assert.equal(allocations,0,"a delayed module must not allocate into an abandoned scene");
  pondFactory=async()=>({create:options=>{
    callbacks=options;allocations++;
    engine=()=>{retired++;};engine.styles=[];engine.moves=[];
    engine.look=value=>engine.styles.push(value);engine.motion=value=>engine.moves.push(value);
    engine.explore=()=>{engine.explored=true;};return engine;
  }});
  scene("koi");await flush();
  const look=panel().querySelector(".background-look-select");
  look.value="wireframe";look.emit("change");
  assert.equal(stored.look,"wireframe");assert.equal(engine.styles.at(-1),"wireframe");
  assert.equal(allocations,1);assert.equal(retired,0,"changing look must retain the simulation");
  callbacks.onLook("natural");assert.equal(look.value,"natural");assert.equal(stored.look,"natural");
  motion.emit("click");assert.equal(engine.moves.at(-1),false);
  motion.emit("click");assert.equal(engine.moves.at(-1),true);
  reduced.matches=true;reduced.emit("change");assert.equal(engine.moves.at(-1),false);
  reduced.matches=false;reduced.emit("change");
  panel().querySelector(".background-explore").emit("click");assert.equal(engine.explored,true);
  assert.equal(panel().hidden,true,"exploration closes the picker without moving desktop windows");
  scene("none");assert.equal(retired,1,"leaving the scene disposes the 3D engine once");
  console.log("Scene races, cleanup, pause/resume, hidden tabs, reduced motion and 3D style continuity pass.");
})().catch(error => { console.error(error); process.exitCode = 1; });
