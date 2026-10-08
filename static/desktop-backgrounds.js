/* Photographic plates + local animation. No remote assets or libraries. */
(() => {
  "use strict";
  const KEY = "vira-background";
  const SCENES = [
    { id: "koi", name: "Koi pond", image: "pond-perspective.jpg",
      detail: "Living koi below rippling water. Click to feed." },
    { id: "redwoods", name: "Redwood grove", image: "redwoods.jpg",
      detail: "A canopy breeze, sunbeams and drifting motes." },
    { id: "aurora", name: "Aurora fjord", image: "aurora.jpg",
      detail: "Dancing polar light reflected in midnight water." },
  ];
  const ids = new Set(["constellation", "none", ...SCENES.map(s => s.id)]);
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let prefs, io, host, panel, stop = () => {}, generation = 0;
  let message = "", initialized = false;
  const images = new Map();
  const asset = name => "/backgrounds/" + name;
  function load(name) {
    if (!images.has(name)) images.set(name, new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => { images.delete(name); reject(new Error("Could not load " + name)); };
      image.src = asset(name);
    }));
    return images.get(name);
  }
  function normalize(value) {
    const p = value && typeof value === "object" ? value : {};
    return { scene: ids.has(p.scene) ? p.scene : "constellation",
      paused: p.paused === true,
      dim: typeof p.dim === "number" && Number.isFinite(p.dim)
        ? Math.max(0, Math.min(.65, p.dim)) : .2 };
  }
  const moving = () => !prefs.paused && !reduced.matches;
  function save() { io.write(KEY, prefs); }
  function select(scene) {
    if (!ids.has(scene)) return;
    prefs.scene = scene;
    save();
    apply();
  }
  function status() {
    if (!panel) return;
    panel.querySelectorAll("[data-scene]").forEach(b => {
      b.setAttribute("aria-pressed", String(b.dataset.scene === prefs.scene));
    });
    const button = panel.querySelector(".background-motion");
    button.textContent = prefs.paused ? "Resume motion" : "Pause motion";
    button.disabled = reduced.matches || prefs.scene === "none";
    button.setAttribute("aria-pressed", String(prefs.paused));
    panel.querySelector("input").disabled = !SCENES.some(s => s.id === prefs.scene);
    const feed = panel.querySelector(".background-feed");
    feed.hidden = prefs.scene !== "koi";
    feed.disabled = !moving() || !stop.feed;
    panel.querySelector(".background-status").textContent = message ||
      (reduced.matches ? "Still scene: your system prefers reduced motion."
        : prefs.paused ? "Motion paused. Your choice is saved."
        : prefs.scene === "koi" ? "Click open water to drop food. The koi will swim over."
        : "Your choice is saved. Motion rests when this tab is hidden.");
  }
  async function apply() {
    const token = ++generation;
    stop();
    stop = () => {};
    host?.remove();
    host = null;
    message = "";
    status();
    if (prefs.scene === "none") return;
    if (prefs.scene === "constellation") {
      stop = io.constellation();
      stop.motion?.(moving());
      return;
    }
    const scene = SCENES.find(s => s.id === prefs.scene);
    const node = document.createElement("div");
    node.id = "desktop-background";
    node.setAttribute("aria-hidden", "true");
    node.style.setProperty("--scene-dim", prefs.dim);
    // A static plate is also the fallback if WebGL is unavailable or lost.
    node.style.backgroundImage = `url("${asset(scene.image)}")`;
    document.body.prepend(node);
    host = node;
    message = "Loading " + scene.name.toLowerCase() + "...";
    status();
    try {
      const names = scene.id === "koi"
        ? [scene.image, "kohaku.webp", "ogon.webp", "showa.webp", "shusui.webp"] : [scene.image];
      const loaded = await Promise.all(names.map(load));
      if (token !== generation) return;
      stop = animate(node, scene.id, loaded);
      message = stop.message || "";
      status();
    } catch (error) {
      if (token !== generation) return;
      message = error.message + ". Choose another background or try again.";
      status();
    }
  }

  const VERTEX = `attribute vec2 a; void main() { gl_Position=vec4(a,0.,1.); }`;
  const FRAGMENT = `precision mediump float;
    uniform sampler2D photo, underwater;
    uniform vec2 resolution, imageSize;
    uniform float time, scene;
    // Match the simulation's twelve live impacts; fixed loops support WebGL 1.
    uniform vec4 ripples[12];
    vec3 aurora(vec2 p) {
      float x=p.x, t=time*.13;
      float base=.37+.065*sin(x*6.+t)+.045*sin(x*13.-t*.8);
      float height=base-p.y;
      float curtain=exp(-pow(height/.14,2.))*smoothstep(-.016,.028,height);
      float folds=.5+.5*sin(x*82.+sin(x*17.+t)*3.+t*2.);
      float fine=.65+.35*sin(x*231.+t*3.);
      float fade=smoothstep(.0,.12,x)*(1.-smoothstep(.87,1.,x));
      vec3 color=mix(vec3(.1,.88,.52),vec3(.43,.22,.82),smoothstep(.03,.19,height));
      return color*curtain*(.25+folds*.55)*fine*fade;
    }
    void main() {
      vec2 p=vec2(gl_FragCoord.x/resolution.x,1.-gl_FragCoord.y/resolution.y);
      float screenRatio=resolution.x/resolution.y;
      float photoRatio=imageSize.x/imageSize.y;
      vec2 scale=vec2(min(1.,screenRatio/photoRatio),min(1.,photoRatio/screenRatio));
      vec2 uv=(p-.5)*scale+.5;
      vec3 original=texture2D(photo,uv).rgb;
      vec2 offset=vec2(0.);
      float rippleLight=0.;
      if(scene<.5) {
        float edge=smoothstep(.02,.18,uv.x)*(1.-smoothstep(.82,.98,uv.x));
        offset=vec2(sin(uv.y*65.+time*.7)+sin(uv.x*37.-time*.4),
          cos(uv.x*54.+time*.6)+sin(uv.y*42.-time*.5))*.0013*edge;
        for(int i=0;i<12;i++) {
          vec4 r=ripples[i];
          if(r.w>0.) {
            vec2 d=(uv-r.xy)*vec2(photoRatio,1.35);
            float distance=length(d);
            float front=r.z*.06;
            float wave=sin((distance-front)*190.)*exp(-abs(distance-front)*65.)
              *exp(-r.z*.9)*r.w;
            offset+=d/max(distance,.001)*wave*.002;
            rippleLight+=wave*.06;
          }
        }
      } else if(scene<1.5) {
        // Green foliage bends; red trunks remain anchored.
        float foliage=smoothstep(.015,.12,original.g-original.r)*
          smoothstep(.005,.09,original.g-original.b);
        offset.x=foliage*(sin(uv.y*19.+time*.65)+sin(uv.x*24.-time*.4))*.002;
        offset.y=foliage*sin(uv.x*31.+time*.5)*.0007;
      } else {
        offset.x=smoothstep(.71,.95,uv.y)*sin(uv.y*310.+time*.7)*.001;
      }
      vec3 color=texture2D(photo,clamp(uv+offset,.001,.999)).rgb;
      if(scene<.5) {
        // Fish are composited before surface light and share the water's
        // refraction. Their photographic reflections remain ABOVE the fish.
        vec2 fishUV=clamp(p+offset/scale,.001,.999);
        vec4 fish=texture2D(underwater,vec2(fishUV.x,1.-fishUV.y));
        vec3 submerged=fish.rgb/max(fish.a,.001)*vec3(.70,.88,.79);
        float reflection=(.14+.16*(1.-uv.y))*(.6+.4*original.b);
        color=mix(color,mix(submerged,original,reflection),fish.a);
        float light=sin(uv.x*71.+uv.y*34.+time*.55)*sin(uv.y*62.-time*.43);
        color+=vec3(.04,.07,.055)*max(0.,light)*.45+vec3(.65,.85,.78)*rippleLight;
      } else if(scene<1.5) {
        float rays=pow(.5+.5*sin(uv.x*38.+uv.y*21.+sin(time*.25)*.3),8.);
        float sun=exp(-length((uv-vec2(.79,.15))*vec2(1.,.7))*2.);
        color+=vec3(1.,.82,.48)*rays*sun*(.025+.015*sin(time*.6+uv.x*18.));
      } else {
        vec3 glow=aurora(uv);
        // Reflections use the same curtain, mirrored about the photographed shoreline.
        if(uv.y>.713) {
          vec2 reflected=vec2(uv.x+sin(uv.y*260.+time)*.005,.713-(uv.y-.713)*2.1);
          glow=aurora(reflected)*.28*(.7+.3*sin(uv.y*480.+time*.5));
        } else {
          float skyline=.70-.44*exp(-uv.x*9.)-.33*exp(-(1.-uv.x)*9.);
          glow*=(1.-smoothstep(.51,.65,uv.y))*(1.-smoothstep(skyline-.035,skyline,uv.y));
        }
        color+=glow;
      }
      gl_FragColor=vec4(color,1.);
    }`;

  const SKIN_VERTEX = `attribute vec4 vertex; varying vec2 uv;
    uniform vec2 offset;
    void main(){uv=vertex.zw;gl_Position=vec4(vertex.xy+offset,0.,1.);}`;
  const SKIN_FRAGMENT = `precision mediump float;
    varying vec2 uv; uniform sampler2D sprite; uniform float depth, shadow;
    void main(){
      float softness=mix(depth*.0012,.008,shadow);
      vec4 fish=texture2D(sprite,uv)*.4;
      fish+=texture2D(sprite,uv+vec2(softness,0.))*.15;
      fish+=texture2D(sprite,uv-vec2(softness,0.))*.15;
      fish+=texture2D(sprite,uv+vec2(0.,softness*2.))*.15;
      fish+=texture2D(sprite,uv-vec2(0.,softness*2.))*.15;
      fish.rgb*=1.-depth*.13;
      fish.a*=.94-depth*.24;
      gl_FragColor=mix(fish,vec4(0.,.07,.05,fish.a*.12),shadow);
    }`;

  function renderer(node, loaded, scene, onLost, pond) {
    const image = loaded[0];
    const canvas = document.createElement("canvas");
    const gl = canvas.getContext("webgl", { alpha: false, antialias: false,
      depth: false, powerPreference: "low-power" });
    if (!gl) return null;
    const shaders = [], resources = [];
    try {
      const shader = (type, source) => {
        const s = gl.createShader(type);
        shaders.push(s);
        gl.shaderSource(s, source); gl.compileShader(s);
        if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
        return s;
      };
      const program = gl.createProgram();
      resources.push(() => gl.deleteProgram(program));
      gl.attachShader(program, shader(gl.VERTEX_SHADER, VERTEX));
      gl.attachShader(program, shader(gl.FRAGMENT_SHADER, FRAGMENT));
      gl.linkProgram(program);
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(program));
      gl.useProgram(program);
      const buffer = gl.createBuffer();
      resources.push(() => gl.deleteBuffer(buffer));
      gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,-1,1,1,-1,1,1]), gl.STATIC_DRAW);
      const a = gl.getAttribLocation(program, "a");
      gl.enableVertexAttribArray(a); gl.vertexAttribPointer(a, 2, gl.FLOAT, false, 0, 0);
      const texture = gl.createTexture();
      resources.push(() => gl.deleteTexture(texture));
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, image);
      const loc = name => gl.getUniformLocation(program, name);
      const size = loc("resolution"), time = loc("time"), ripples = loc("ripples[0]");
      const fishTexture = gl.createTexture();
      resources.push(() => gl.deleteTexture(fishTexture));
      gl.activeTexture(gl.TEXTURE1);
      gl.bindTexture(gl.TEXTURE_2D, fishTexture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,1,1,0,gl.RGBA,gl.UNSIGNED_BYTE,new Uint8Array(4));
      gl.uniform1i(loc("underwater"),1);
      gl.uniform2f(loc("imageSize"), image.width, image.height);
      gl.uniform1f(loc("scene"), ["koi", "redwoods", "aurora"].indexOf(scene));
      let skin = null, frameW = 0, frameH = 0;
      if (pond) {
        const skinProgram = gl.createProgram();
        resources.push(() => gl.deleteProgram(skinProgram));
        gl.attachShader(skinProgram,shader(gl.VERTEX_SHADER,SKIN_VERTEX));
        gl.attachShader(skinProgram,shader(gl.FRAGMENT_SHADER,SKIN_FRAGMENT));
        gl.linkProgram(skinProgram);
        if (!gl.getProgramParameter(skinProgram,gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(skinProgram));
        const framebuffer = gl.createFramebuffer(), skinBuffer = gl.createBuffer();
        resources.push(() => gl.deleteFramebuffer(framebuffer),() => gl.deleteBuffer(skinBuffer));
        // Immutable sprite textures are uploaded once; only the small vertex
        // buffer changes each frame. No CPU skin raster or canvas readback.
        const sprites = loaded.slice(1).map(sprite => {
          const texture = gl.createTexture();
          resources.push(() => gl.deleteTexture(texture));
          gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D,texture);
          gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
          gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
          gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
          gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,sprite);
          return texture;
        });
        gl.useProgram(skinProgram);
        gl.uniform1i(gl.getUniformLocation(skinProgram,"sprite"),2);
        skin = { program:skinProgram, framebuffer, buffer:skinBuffer, sprites,
          attribute:gl.getAttribLocation(skinProgram,"vertex"),
          depth:gl.getUniformLocation(skinProgram,"depth"),
          shadow:gl.getUniformLocation(skinProgram,"shadow"),
          offset:gl.getUniformLocation(skinProgram,"offset") };
      }
      function drawFish(w,h) {
        gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D,fishTexture);
        gl.bindFramebuffer(gl.FRAMEBUFFER,skin.framebuffer);
        if (frameW!==w || frameH!==h) {
          frameW=w;frameH=h;
          gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,w,h,0,gl.RGBA,gl.UNSIGNED_BYTE,null);
          gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,fishTexture,0);
          if (gl.checkFramebufferStatus(gl.FRAMEBUFFER)!==gl.FRAMEBUFFER_COMPLETE)
            throw new Error("Fish render target unavailable");
        }
        gl.viewport(0,0,w,h); gl.clearColor(0,0,0,0); gl.clear(gl.COLOR_BUFFER_BIT);
        gl.useProgram(skin.program); gl.bindBuffer(gl.ARRAY_BUFFER,skin.buffer);
        if (skin.attribute!==a) gl.disableVertexAttribArray(a);
        gl.enableVertexAttribArray(skin.attribute);
        gl.vertexAttribPointer(skin.attribute,4,gl.FLOAT,false,16,0);
        gl.enable(gl.BLEND);
        // Store premultiplied color in the fish target, including overlaps;
        // the water shader converts back before applying surface reflections.
        gl.blendFuncSeparate(gl.SRC_ALPHA,gl.ONE_MINUS_SRC_ALPHA,gl.ONE,gl.ONE_MINUS_SRC_ALPHA);
        const cover=Math.max(w/image.width,h/image.height), pw=image.width*cover, ph=image.height*cover;
        for (const f of [...pond.fish].sort((a,b)=>b.depth-a.depth)) {
          const uv=window.ViraKoiPond.project(f.x,f.y);
          const px=(w-pw)/2+uv.x*pw, py=(h-ph)/2+uv.y*ph+f.depth*3;
          const c=window.ViraKoiPond.camera(f.x,f.y,image.width/image.height);
          const length=pw*.108*f.size*(1-f.depth*.07);
          const cosine=Math.cos(f.heading+Math.PI), sine=Math.sin(f.heading+Math.PI);
          const mesh=window.ViraKoiPond.skinMesh(f,loaded[1+f.type].height/loaded[1+f.type].width);
          const vertices=new Float32Array((mesh.length-1)*4*3*4);
          let cursor=0;
          const vertex=p=>{
            const x=cosine*p.x-sine*p.y,y=sine*p.x+cosine*p.y;
            vertices[cursor++]=2*(px+length*(c.xx*x+c.xy*y))/w-1;
            vertices[cursor++]=1-2*(py+length*(c.yx*x+c.yy*y))/h;
            vertices[cursor++]=p.u; vertices[cursor++]=p.v;
          };
          for(let j=0;j<mesh.length-1;j++)for(let k=0;k<2;k++){
            const a=mesh[j][k],b=mesh[j+1][k],c=mesh[j+1][k+1],d=mesh[j][k+1];
            [a,b,c,a,c,d].forEach(vertex);
          }
          gl.bufferData(gl.ARRAY_BUFFER,vertices,gl.DYNAMIC_DRAW);
          gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D,skin.sprites[f.type]);
          gl.uniform1f(skin.depth,f.depth);
          gl.uniform1f(skin.shadow,1);
          gl.uniform2f(skin.offset,f.depth*14/w,-f.depth*36/h);
          gl.drawArrays(gl.TRIANGLES,0,vertices.length/4);
          gl.uniform1f(skin.shadow,0); gl.uniform2f(skin.offset,0,0);
          gl.drawArrays(gl.TRIANGLES,0,vertices.length/4);
        }
        gl.disable(gl.BLEND); gl.bindFramebuffer(gl.FRAMEBUFFER,null);
        if (skin.attribute!==a) gl.disableVertexAttribArray(skin.attribute);
        gl.useProgram(program); gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
        gl.enableVertexAttribArray(a);gl.vertexAttribPointer(a,2,gl.FLOAT,false,0,0);
      }
      node.appendChild(canvas);
      // A lost context reveals the static photograph immediately.
      let failed = false;
      const lost = () => { failed = true; canvas.style.display = "none"; onLost(); };
      canvas.addEventListener("webglcontextlost", lost);
      return {
        draw(t, w, h, impacts) {
          if (failed || gl.isContextLost()) return;
          if (canvas.width !== w || canvas.height !== h) {
            canvas.width = w; canvas.height = h;
          }
          if (skin) {
            try {
              drawFish(Math.round(w/Math.max(1,w/1280)),Math.round(h/Math.max(1,w/1280)));
            } catch (error) {
              failed = true; gl.bindFramebuffer(gl.FRAMEBUFFER,null);
              canvas.style.display = "none"; onLost(error.message); return;
            }
          }
          gl.useProgram(program); gl.viewport(0,0,w,h);
          gl.uniform2f(size, w, h); gl.uniform1f(time, t);
          if (skin) {
            gl.uniform4fv(ripples,impacts);
          }
          gl.drawArrays(gl.TRIANGLES, 0, 6);
        },
        close() {
          canvas.removeEventListener("webglcontextlost", lost);
          resources.forEach(dispose => dispose());
          shaders.forEach(s => gl.deleteShader(s));
          gl.getExtension("WEBGL_lose_context")?.loseContext();
          canvas.remove();
        },
      };
    } catch (error) {
      resources.forEach(dispose => dispose());
      shaders.forEach(s => gl.deleteShader(s));
      gl.getExtension("WEBGL_lose_context")?.loseContext();
      console.warn("Desktop background uses its static photo:", error.message);
      return null;
    }
  }

  function skinStrip(ctx,image,a,b,side) {
    // A light affine fallback for devices without WebGL. Each short column
    // follows its bone tangent; fins still have independent cross sections.
    const sw=b[1].sx-a[1].sx, edge=side ? 2 : 0, sign=side ? 1 : -1;
    const xx=(b[1].x-a[1].x)/sw, yx=(b[1].y-a[1].y)/sw;
    const xy=(a[edge].x-a[1].x+b[edge].x-b[1].x)/(sign*image.height);
    const yy=(a[edge].y-a[1].y+b[edge].y-b[1].y)/(sign*image.height);
    ctx.save();
    ctx.transform(xx,yx,xy,yy,a[1].x-xx*a[1].sx-xy*image.height/2,
      a[1].y-yx*a[1].sx-yy*image.height/2);
    const left=Math.max(0,a[1].sx-.5), width=Math.min(image.width,b[1].sx+.5)-left;
    const top=side ? image.height/2 : 0;
    ctx.drawImage(image,left,top,width,image.height/2,left,top,width,image.height/2);
    ctx.restore();
  }

  function animate(node, scene, loaded) {
    const pond = scene === "koi" ? window.ViraKoiPond.create() : null;
    const below = pond ? document.createElement("canvas") : null;
    const fishCtx = below?.getContext("2d");
    const body = pond ? document.createElement("canvas") : null;
    const bodyCtx = body?.getContext("2d");
    if (body) { body.width = 640; body.height = 640; }
    let graphicsLost = false;
    const plate = renderer(node, loaded, scene, reason => {
      graphicsLost = true;
      message = reason ? "Water and light effects unavailable: " + reason + ". Choose the scene again to retry."
        : "Water and light effects stopped: graphics context lost. Choose the scene again to retry.";
      status();
    }, pond);
    const canvas = document.createElement("canvas");
    node.appendChild(canvas);
    const ctx = canvas.getContext("2d");
    let raf = 0, elapsed = 0, last = 0, painted = 0, closed = false;
    const impacts = new Float32Array(48); // twelve shader ripple vectors
    const motes = Array.from({ length: 28 }, (_, i) => ({
      x: ((i * .6180339) % 1), y: ((i * .4142135) % 1), phase: i * 2.3,
    }));
    function view(w, h) {
      const cover = Math.max(w / loaded[0].width, h / loaded[0].height);
      const pw = loaded[0].width * cover, ph = loaded[0].height * cover;
      return { pw, ph, left: (w-pw)/2, top: (h-ph)/2 };
    }
    function position(p, v) {
      const uv = window.ViraKoiPond.project(p.x,p.y);
      return { x: v.left+uv.x*v.pw, y: v.top+uv.y*v.ph };
    }
    function fish(w, h) {
      if (below.width !== w || below.height !== h) { below.width = w; below.height = h; }
      fishCtx.clearRect(0,0,w,h);
      const v = view(w,h);
      // Paint deeper fish first so a near-surface swimmer passes above them.
      for (const f of [...pond.fish].sort((a,b) => b.depth-a.depth)) {
        const p = position(f,v), image = loaded[1+f.type];
        const projection = window.ViraKoiPond.camera(f.x,f.y,loaded[0].width/loaded[0].height);
        const length = v.pw*.108*f.size*(1-f.depth*.07);
        bodyCtx.clearRect(0,0,640,640);
        // The CPU fallback follows the same backbone using short rotated
        // columns; the normal GPU path renders a connected triangle mesh.
        const mesh = window.ViraKoiPond.skinMesh(f,image.height/image.width).map(column =>
          column.map(p => ({ sx:p.u*image.width, sy:p.v*image.height,
            x:320+p.x*384, y:320+p.y*384 })));
        for (let j=0;j<mesh.length-1;j++) for (let k=0;k<2;k++) {
          skinStrip(bodyCtx,image,mesh[j],mesh[j+1],k);
        }
        fishCtx.save();
        fishCtx.translate(p.x+f.depth*7,p.y+f.depth*18);
        fishCtx.transform(projection.xx,projection.yx,projection.xy,projection.yy,0,0);
        fishCtx.rotate(f.heading+Math.PI);
        fishCtx.filter = `blur(${3+f.depth*5}px)`;
        fishCtx.fillStyle = `rgba(0,18,15,${.15-f.depth*.065})`;
        fishCtx.beginPath(); fishCtx.ellipse(0,0,length*.39,length*.085,0,0,Math.PI*2); fishCtx.fill();
        fishCtx.restore();
        fishCtx.save(); fishCtx.translate(p.x,p.y+f.depth*3);
        fishCtx.transform(projection.xx,projection.yx,projection.xy,projection.yy,0,0);
        fishCtx.rotate(f.heading+Math.PI);
        // Roll varies along the spine in the mesh, rather than flattening
        // the entire fish into a uniformly squashed photograph.
        fishCtx.globalAlpha = .94-f.depth*.24;
        fishCtx.filter = `blur(${f.depth*.55}px) brightness(${1-f.depth*.13})`;
        fishCtx.drawImage(body,-length*320/384,-length*320/384,
          length*640/384,length*640/384);
        fishCtx.restore();
      }
    }
    function surface(w,h) {
      const v = view(w,h);
      for (const drop of pond.drops) {
        const p = position(drop,v), r = Math.max(1,drop.age*.06*v.ph);
        const a = Math.exp(-drop.age*.9)*drop.strength*.28;
        ctx.lineWidth = .8;
        for (let k = 0; k < 3; k++) {
          const ring = Math.max(1,r-k*7);
          ctx.strokeStyle = `rgba(194,229,219,${a*(1-k*.2)})`;
          ctx.beginPath(); ctx.ellipse(p.x,p.y,ring,ring/1.35,0,0,Math.PI*2); ctx.stroke();
        }
        if (drop.age < .18) {
          ctx.fillStyle = "rgba(231,248,242,.45)";
          ctx.beginPath(); ctx.ellipse(p.x,p.y,1.5,1,0,0,Math.PI*2); ctx.fill();
        }
      }
      for (const pellet of pond.food) {
        const p = position(pellet,v);
        ctx.globalAlpha = Math.min(1,pellet.life);
        ctx.fillStyle = "#b89962";
        ctx.beginPath(); ctx.ellipse(p.x,p.y,1.8,1.3,0,0,Math.PI*2); ctx.fill();
        ctx.globalAlpha = 1;
      }
    }
    function paint() {
      // Limit GPU pixels and frame rate: background detail need not compete with work.
      const ratio = Math.min(devicePixelRatio || 1, 1.5, 1920 / innerWidth);
      const gpuW = Math.round(innerWidth*ratio), gpuH = Math.round(innerHeight*ratio);
      // The uploaded fish texture needs only desktop resolution, not retina
      // resolution: water refraction and depth soften it again in the shader.
      const layerRatio = pond ? Math.min(1,1280/innerWidth) : ratio;
      const w = Math.round(innerWidth*layerRatio), h = Math.round(innerHeight*layerRatio);
      if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
      impacts.fill(0);
      const fallback = !plate || graphicsLost;
      if (pond) {
        if (fallback) fish(w,h);
        pond.drops.forEach((drop,i) => {
          const p = window.ViraKoiPond.project(drop.x,drop.y);
          impacts.set([p.x,p.y,drop.age,drop.strength],i*4);
        });
      }
      plate?.draw(elapsed,gpuW,gpuH,impacts);
      if (pond && graphicsLost && !fallback) fish(w,h);
      ctx.clearRect(0,0,w,h);
      if (pond) {
        if (!plate || graphicsLost) ctx.drawImage(below,0,0,w,h);
        surface(w,h);
      }
      if (scene === "redwoods") {
        for (const m of motes) {
          const x = ((m.x+elapsed*.0018)%1)*w;
          const y = ((m.y-elapsed*.003+100)%1)*h;
          const a = (.15+.12*Math.sin(elapsed*.7+m.phase))*(x/w);
          ctx.fillStyle = `rgba(255,228,168,${a})`;
          ctx.beginPath(); ctx.arc(x,y,1.2*ratio,0,Math.PI*2); ctx.fill();
        }
      }
    }
    function tick(now) {
      raf = 0;
      if (closed || document.hidden || !moving()) return;
      const dt = last ? Math.min((now-last)/1000,.1) : 0;
      elapsed += dt; pond?.step(dt); last = now;
      if (now-painted >= 1000/30) { paint(); painted = now; }
      raf = requestAnimationFrame(tick);
    }
    function visibility() {
      cancelAnimationFrame(raf); raf = 0; last = 0;
      if (!closed && !document.hidden && moving()) raf = requestAnimationFrame(tick);
    }
    function resize() { paint(); }
    function dropFood(x,y) {
      if (closed || !pond || !moving() || document.hidden) return false;
      if (!pond.feed(x,y)) return false;
      paint();
      return true;
    }
    function clickPoint(e) {
      if (e.button !== 0 || e.defaultPrevented || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey
          || !io.canFeed?.(e.target)
          || e.target.closest?.("button,a,input,textarea,select,[contenteditable],#background-picker,.ctx-menu,.ctx-pop,#reminder-stickies")) return null;
      const v = view(innerWidth,innerHeight);
      const p = window.ViraKoiPond.unproject((e.clientX-v.left)/v.pw,(e.clientY-v.top)/v.ph);
      return window.ViraKoiPond.inWater(p.x,p.y) ? p : null;
    }
    const feedClick = e => { const p = clickPoint(e); if (p && dropFood(p.x,p.y)) e.preventDefault(); };
    // Rapid feeding must not invoke the desktop's double-click "close all".
    const feedDouble = e => { if (moving() && clickPoint(e)) e.preventDefault(); };
    paint(); visibility();
    document.addEventListener("visibilitychange",visibility);
    addEventListener("resize",resize);
    if (pond) {
      document.addEventListener("click",feedClick);
      document.addEventListener("dblclick",feedDouble,true);
    }
    const dispose = () => {
      closed = true; cancelAnimationFrame(raf);
      document.removeEventListener("visibilitychange",visibility);
      removeEventListener("resize",resize);
      document.removeEventListener("click",feedClick);
      document.removeEventListener("dblclick",feedDouble,true);
      plate?.close(); canvas.remove();
    };
    dispose.motion = visibility;
    dispose.feed = pond ? () => dropFood(.5,.5) : null;
    dispose.message = plate ? "" : "Water and light effects unavailable: using the photograph.";
    return dispose;
  }

  function picker() {
    panel = document.createElement("section");
    panel.id = "background-picker";
    panel.hidden = true;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-labelledby", "background-title");
    panel.innerHTML = `<div class="background-head"><div>
      <h2 id="background-title">A different kind of desktop</h2>
      <p>Three living landscapes. Choose one to see it on your desk.</p></div>
      <button class="background-close" aria-label="Close background picker">&times;</button></div>
      <div class="background-options"></div><div class="background-simple"></div>
      <div class="background-controls"><button class="background-motion">Pause motion</button>
      <button class="background-feed" hidden>Feed koi</button>
      <label>Dim <input aria-label="Background dimming" type="range" min="0" max="65" step="1"></label></div>
      <p class="background-status" role="status"></p>`;
    for (const s of SCENES) {
      const b = document.createElement("button");
      b.className = "background-choice"; b.dataset.scene = s.id;
      const img = document.createElement("img"); img.src = asset(s.image); img.alt = "";
      img.loading = "lazy"; img.decoding = "async";
      const title = document.createElement("strong"); title.textContent = s.name;
      const detail = document.createElement("span"); detail.textContent = s.detail;
      b.append(img, title, detail);
      b.addEventListener("click", () => select(s.id));
      panel.querySelector(".background-options").appendChild(b);
    }
    for (const [id, label] of [["constellation", "Constellation"], ["none", "Plain background"]]) {
      const b = document.createElement("button"); b.dataset.scene = id; b.textContent = label;
      b.addEventListener("click", () => select(id));
      panel.querySelector(".background-simple").appendChild(b);
    }
    panel.querySelector(".background-motion").addEventListener("click", () => {
      prefs.paused = !prefs.paused; save(); motionChange();
    });
    panel.querySelector(".background-feed").addEventListener("click", () => stop.feed?.());
    const range = panel.querySelector("input"); range.value = Math.round(prefs.dim * 100);
    range.addEventListener("input", () => {
      prefs.dim = Number(range.value) / 100;
      host?.style.setProperty("--scene-dim", prefs.dim); save();
    });
    const trigger = document.getElementById("background-btn");
    function show() {
      panel.hidden = false; trigger.setAttribute("aria-expanded", "true");
      status(); panel.querySelector(`[data-scene="${prefs.scene}"]`).focus();
    }
    function hide(focus = true) {
      panel.hidden = true; trigger.setAttribute("aria-expanded", "false");
      if (focus) trigger.focus();
    }
    panel.querySelector(".background-close").addEventListener("click", () => hide());
    panel.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); hide(); } });
    document.addEventListener("pointerdown", e => {
      if (!panel.hidden && !panel.contains(e.target) && !trigger.contains(e.target)) hide(false);
    });
    trigger.hidden = false;
    trigger.addEventListener("click", () => panel.hidden ? show() : hide());
    document.getElementById("design-background-btn")?.addEventListener("click", show);
    document.body.appendChild(panel);
    status();
  }
  function motionChange() {
    if (stop.motion) { stop.motion(moving()); status(); }
    else apply();
  }
  window.ViraBackgrounds = {
    init(options) {
      if (initialized) return;
      initialized = true; io = options; prefs = normalize(io.read(KEY, {}));
      picker(); apply();
      reduced.addEventListener("change", motionChange);
    },
  };
})();
