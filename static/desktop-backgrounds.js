/* Local living backgrounds: a 3D pond and animated photographic landscapes. */
(() => {
  "use strict";
  const KEY = "vira-background";
  const PONDS = [
    { id:"garden", name:"Open water", image:"open-water.svg" },
    { id:"courtyard", name:"Open water", image:"open-water.svg" },
  ];
  const SCENES = [
    { id: "koi", name: "Koi pond", image: "open-water-thumbnail.svg",
      detail: "Koi in dark, open water. Move closer; click for ripples." },
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
      // Selected scene assets should not wait behind the desktop polling traffic.
      image.fetchPriority = "high";
      image.decoding = "async";
      image.onload = () => resolve(image);
      image.onerror = () => { images.delete(name); reject(new Error("Could not load " + name)); };
      image.src = asset(name);
    }));
    return images.get(name);
  }
  function normalize(value) {
    const p = value && typeof value === "object" ? value : {};
    return { scene: ids.has(p.scene) ? p.scene : "constellation",
      pond: PONDS.some(s => s.id === p.pond) ? p.pond : "garden",
      look:p.look==="wireframe"?"wireframe":"natural",
      paused: p.paused === true,
      dim: typeof p.dim === "number" && Number.isFinite(p.dim)
        ? Math.max(0, Math.min(.65, p.dim)) : .2 };
  }
  const moving = () => !prefs.paused && !reduced.matches;
  const pondSetting = () => PONDS.find(p => p.id === prefs.pond);
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
    panel.querySelector(".background-look-setting").hidden=prefs.scene!=="koi";
    panel.querySelector(".background-look-select").value=prefs.look;
    panel.querySelector(".background-look-select").disabled=prefs.scene==="koi" && !stop.look;
    const studioLook=document.getElementById("design-pond-look");if(studioLook){studioLook.value=prefs.look;studioLook.disabled=prefs.scene==="koi" && !stop.look;}
    const explore=panel.querySelector(".background-explore");explore.hidden=prefs.scene!=="koi";
    explore.disabled=!stop.explore;
    panel.querySelector('[data-scene="koi"]').querySelector("img").src = asset("open-water-thumbnail.svg");
    panel.querySelector(".background-status").textContent = message ||
      (reduced.matches ? "Still scene: your system prefers reduced motion."
        : prefs.paused ? "Motion paused. Your choice is saved."
        : prefs.scene === "koi" ? "Move over open water to draw koi closer. Click to make ripples."
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
    const base = SCENES.find(s => s.id === prefs.scene);
    const scene = prefs.scene === "koi" ? { ...base, image:pondSetting().image } : base;
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
      if(scene.id==="koi"){
        let engine=null;
        try{
          const factory=io.pond3D || (()=>import("./koi-pond-3d.js"));
          const module=await factory();if(token!==generation)return;
          engine=module?.create({node,loaded,canInteract:io.canInteract,
            onMessage:value=>{if(token===generation){message=value;status();}},onLook:setLook,look:prefs.look,
            onPause:()=>{prefs.paused=!prefs.paused;save();motionChange();}});
        }catch(error){console.warn("3D pond unavailable:",error.message);}
        if(token!==generation){engine?.();return;}
        stop=engine || animate(node,scene.id,loaded);
        if(engine){stop.look(prefs.look);stop.motion(moving());}
        else stop.message="3D pond unavailable: using the flat water approximation. "+(stop.message || "");
      }else stop = animate(node, scene.id, loaded);
      message = stop.message || "";
      status();
    } catch (error) {
      if (token !== generation) return;
      message = error.message + ". Choose another background or try again.";
      status();
    }
  }

  function setLook(look){
    if(!["natural","wireframe"].includes(look))return;
    prefs.look=look;save();
    if(prefs.scene!=="koi")select("koi");
    else {stop.look?.(look);status();}
  }

  const VERTEX = `attribute vec2 a; void main() { gl_Position=vec4(a,0.,1.); }`;
  const FRAGMENT = `precision mediump float;
    uniform sampler2D photo, underwater;
    uniform vec2 resolution, imageSize;
    uniform float time, scene;
    uniform vec4 pondCamera;
    uniform float waterMetric;
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
        float planeY=(uv.y-.51)/(photoRatio*pondCamera.x);
        float edge=1.;
        offset=vec2(sin(uv.y*65.+time*.7)+sin(uv.x*37.-time*.4),
          cos(uv.x*54.+time*.6)+sin(uv.y*42.-time*.5))*.00065*edge;
        for(int i=0;i<12;i++) {
          vec4 r=ripples[i];
          if(r.w>0.) {
            vec2 d=(uv-r.xy)*vec2(photoRatio,1.35);
            float distance=length(d);
            float front=r.z*.06;
            float wave=sin((distance-front)*190.)*exp(-abs(distance-front)*65.)
              *exp(-r.z*.9)*r.w;
            offset+=d/max(distance,.001)*wave*.0015*edge;
            rippleLight+=wave*.025*edge;
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
        // refraction. Surface reflections remain above the fish.
        vec2 fishUV=clamp(p+offset/scale,.001,.999);
        vec4 fish=texture2D(underwater,vec2(fishUV.x,1.-fishUV.y));
        vec3 submerged=fish.rgb/max(fish.a,.001);
        float planeY=(uv.y-.51)/(photoRatio*pondCamera.x);
        float worldY=planeY*pondCamera.y/(pondCamera.y*pondCamera.z+planeY*pondCamera.w);
        float worldX=(uv.x-.5)*(pondCamera.y-worldY*pondCamera.w)/(pondCamera.x*pondCamera.y);
        float cosine=pondCamera.y*pondCamera.z/length(vec3(worldX,
          pondCamera.y*pondCamera.w-worldY,pondCamera.y*pondCamera.z));
        float fresnel=.0204+.9796*pow(1.-cosine,5.);
        // The dark plate supplies quiet reflected sky radiance. Estimate its
        // prominence from brightness, keeping it above even a shallow fish.
        float brightness=dot(original,vec3(.2126,.7152,.0722));
        float reflection=clamp(fresnel*(4.+brightness*24.),.07,.52);
        color=mix(color,mix(submerged,color,reflection),fish.a);
        color+=vec3(.65,.85,.78)*rippleLight;
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
    varying vec2 uv; uniform sampler2D sprite; uniform float depth, shadow, clarity, softness;
    uniform vec3 transmission;
    vec4 sampleFish(vec2 p){vec4 f=texture2D(sprite,p);return vec4(f.rgb*f.a,f.a);}
    void main(){
      float blur=mix(softness,.012,shadow);
      vec4 fish=sampleFish(uv)*.4;
      fish+=sampleFish(uv+vec2(blur,0.))*.15;
      fish+=sampleFish(uv-vec2(blur,0.))*.15;
      fish+=sampleFish(uv+vec2(0.,blur*2.))*.15;
      fish+=sampleFish(uv-vec2(0.,blur*2.))*.15;
      fish.rgb=fish.rgb/max(fish.a,.001)*transmission;
      fish.a*=clarity;
      gl_FragColor=mix(fish,vec4(0.,.07,.05,fish.a*.035*exp(-depth)),shadow);
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
      const lens=window.ViraKoiPond.lens;
      gl.uniform4fv(loc("pondCamera"),new Float32Array([lens.scale,lens.distance,lens.sin,lens.cos]));
      gl.uniform1f(loc("waterMetric"),lens.waterY);
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
          clarity:gl.getUniformLocation(skinProgram,"clarity"),
          softness:gl.getUniformLocation(skinProgram,"softness"),
          transmission:gl.getUniformLocation(skinProgram,"transmission"),
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
          const mesh=window.ViraKoiPond.projectedMesh(f,loaded[1+f.type].height/loaded[1+f.type].width);
          const vertices=new Float32Array((mesh.length-1)*4*3*4);
          let cursor=0;
          const vertex=p=>{
            vertices[cursor++]=2*((w-pw)/2+p.x*pw)/w-1;
            vertices[cursor++]=1-2*((h-ph)/2+p.y*ph)/h;
            vertices[cursor++]=p.u; vertices[cursor++]=p.v;
          };
          for(let j=0;j<mesh.length-1;j++)for(let k=0;k<2;k++){
            const a=mesh[j][k],b=mesh[j+1][k],c=mesh[j+1][k+1],d=mesh[j][k+1];
            [a,b,c,a,c,d].forEach(vertex);
          }
          gl.bufferData(gl.ARRAY_BUFFER,vertices,gl.DYNAMIC_DRAW);
          gl.activeTexture(gl.TEXTURE2); gl.bindTexture(gl.TEXTURE_2D,skin.sprites[f.type]);
          const light=window.ViraKoiPond.optics(f.depth);
          gl.uniform1f(skin.depth,f.depth);
          gl.uniform1f(skin.clarity,light.clarity); gl.uniform1f(skin.softness,light.blur);
          gl.uniform3fv(skin.transmission,new Float32Array(light.transmission));
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
      const uv = window.ViraKoiPond.project(p.x,p.y,p.depth || 0);
      return { x: v.left+uv.x*v.pw, y: v.top+uv.y*v.ph };
    }
    function fish(w, h) {
      if (below.width !== w || below.height !== h) { below.width = w; below.height = h; }
      fishCtx.clearRect(0,0,w,h);
      const v = view(w,h);
      // Paint deeper fish first so a near-surface swimmer passes above them.
      for (const f of [...pond.fish].sort((a,b) => b.depth-a.depth)) {
        const p = position(f,v), image = loaded[1+f.type];
        const length = v.pw*.108*f.size;
        const light=window.ViraKoiPond.optics(f.depth);
        bodyCtx.clearRect(0,0,640,640);
        // The CPU fallback follows the same backbone using short rotated
        // columns; the normal GPU path renders a connected triangle mesh.
        const mesh = window.ViraKoiPond.projectedMesh(f,image.height/image.width).map(column =>
          column.map(vertex => ({ sx:vertex.u*image.width, sy:vertex.v*image.height,
            x:320+(v.left+vertex.x*v.pw-p.x)*384/length,
            y:320+(v.top+vertex.y*v.ph-p.y)*384/length })));
        for (let j=0;j<mesh.length-1;j++) for (let k=0;k<2;k++) {
          skinStrip(bodyCtx,image,mesh[j],mesh[j+1],k);
        }
        fishCtx.save();
        fishCtx.translate(p.x+f.depth*7,p.y+f.depth*18);
        fishCtx.rotate(f.heading+Math.PI);
        fishCtx.filter = `blur(${3+f.depth*5}px)`;
        fishCtx.fillStyle = `rgba(0,18,15,${.025*Math.exp(-f.depth)})`;
        fishCtx.beginPath(); fishCtx.ellipse(0,0,length*.39,length*.085,0,0,Math.PI*2); fishCtx.fill();
        fishCtx.restore();
        bodyCtx.save(); bodyCtx.globalCompositeOperation="source-atop";
        bodyCtx.fillStyle=`rgba(20,62,44,${1-Math.exp(-light.path*.38)})`;
        bodyCtx.fillRect(0,0,640,640); bodyCtx.restore();
        fishCtx.save(); fishCtx.translate(p.x,p.y);
        // Roll varies along the spine in the mesh, rather than flattening
        // the entire fish into a uniformly squashed photograph.
        fishCtx.globalAlpha = light.clarity;
        fishCtx.filter = `blur(${light.blur*length}px) brightness(${light.transmission[1]})`;
        fishCtx.drawImage(body,-length*320/384,-length*320/384,
          length*640/384,length*640/384);
        fishCtx.restore();
      }
    }
    function surface(w,h) {
      const v = view(w,h);
      for (const drop of pond.drops) {
        const p = position(drop,v), r = Math.max(1,drop.age*.06*v.pw);
        const c=window.ViraKoiPond.camera(drop.x,drop.y,loaded[0].width/loaded[0].height);
        const a = Math.exp(-drop.age*.9)*drop.strength*.28;
        ctx.save(); ctx.translate(p.x,p.y); ctx.transform(c.xx,c.yx,c.xy,c.yy,0,0);
        ctx.lineWidth = .8;
        for (let k = 0; k < 3; k++) {
          const ring = Math.max(1,r-k*7);
          ctx.strokeStyle = `rgba(194,229,219,${a*(1-k*.2)})`;
          ctx.beginPath(); ctx.ellipse(0,0,ring,ring,0,0,Math.PI*2); ctx.stroke();
        }
        if (drop.age < .18) {
          ctx.fillStyle = "rgba(231,248,242,.45)";
          ctx.beginPath(); ctx.ellipse(0,0,1.5,1,0,0,Math.PI*2); ctx.fill();
        }
        ctx.restore();
      }
    }
    function paint() {
      // Preserve native 4K detail; retain a bounded pixel budget on retina displays.
      const ratio = Math.min(devicePixelRatio || 1,2,Math.sqrt(8388608/(innerWidth*innerHeight)),4096/Math.max(innerWidth,innerHeight));
      const gpuW = Math.round(innerWidth*ratio), gpuH = Math.round(innerHeight*ratio);
      const layerRatio = ratio;
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
      const dt = last ? Math.min((now-last)/1000,.25) : 0;
      elapsed += dt; pond?.step(dt); last = now;
      if (now-painted >= 1000/30) { paint(); painted = now; }
      raf = requestAnimationFrame(tick);
    }
    function visibility() {
      cancelAnimationFrame(raf); raf = 0; last = 0;
      if (!closed && !document.hidden && moving()) raf = requestAnimationFrame(tick);
    }
    function resize() { paint(); }
    function flickWater(x,y) {
      if (closed || !pond || !moving() || document.hidden) return false;
      if (!pond.flick(x,y)) return false;
      paint();
      return true;
    }
    function clickPoint(e) {
      if (e.button !== 0 || e.defaultPrevented || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey
          || !io.canInteract?.(e.target)
          || e.target.closest?.("button,a,input,textarea,select,[contenteditable],#background-picker,.ctx-menu,.ctx-pop,#reminder-stickies")) return null;
      const v = view(innerWidth,innerHeight);
      const p = window.ViraKoiPond.unproject((e.clientX-v.left)/v.pw,(e.clientY-v.top)/v.ph);
      return p;
    }
    const waterClick = e => { const p = clickPoint(e); if (p && flickWater(p.x,p.y)) e.preventDefault(); };
    // Rapid water flicks must not invoke the desktop's double-click "close all".
    const waterDouble = e => { if (moving() && clickPoint(e)) e.preventDefault(); };
    const pointerMove=e=>{
      if(!pond)return;
      const v=view(innerWidth,innerHeight),p=io.canInteract?.(e.target)
        ?window.ViraKoiPond.unproject((e.clientX-v.left)/v.pw,(e.clientY-v.top)/v.ph):null;
      pond.follow(p?.x,p?.y);
    };
    const clearPointer=()=>pond?.follow(null);
    paint(); visibility();
    document.addEventListener("visibilitychange",visibility);
    addEventListener("resize",resize);
    if (pond) {
      document.addEventListener("pointermove",pointerMove);document.addEventListener("pointerleave",clearPointer);
      addEventListener("blur",clearPointer);
      document.addEventListener("click",waterClick);
      document.addEventListener("dblclick",waterDouble,true);
    }
    const dispose = () => {
      closed = true; cancelAnimationFrame(raf);
      document.removeEventListener("visibilitychange",visibility);
      removeEventListener("resize",resize);
      document.removeEventListener("pointermove",pointerMove);document.removeEventListener("pointerleave",clearPointer);
      removeEventListener("blur",clearPointer);
      document.removeEventListener("click",waterClick);
      document.removeEventListener("dblclick",waterDouble,true);
      plate?.close(); canvas.remove();
    };
    dispose.motion = visibility;
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
      <p>Three living backgrounds. Choose one to see it on your desk.</p></div>
      <button class="background-close" aria-label="Close background picker">&times;</button></div>
      <div class="background-options"></div><div class="background-simple"></div>
      <div class="background-controls"><button class="background-motion">Pause motion</button>
      <button class="background-explore" hidden>Look around</button>
      <label>Dim <input aria-label="Background dimming" type="range" min="0" max="65" step="1"></label></div>
      <label class="background-look-setting" hidden>Pond look <select class="background-look-select" aria-label="Pond look">
        <option value="natural">Natural 3D</option><option value="wireframe">Cyberpunk wireframe</option></select></label>
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
    panel.querySelector(".background-explore").addEventListener("click",()=>{panel.hidden=true;trigger.setAttribute("aria-expanded","false");stop.explore?.();});
    panel.querySelector(".background-look-select").addEventListener("change",e=>setLook(e.target.value));
    document.getElementById("design-pond-look")?.addEventListener("change",e=>setLook(e.target.value));
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
