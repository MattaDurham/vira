/* A metre-scaled, navigable pond. Three.js is bundled locally with Vira. */
import * as THREE from "./vendor/three.module.js";

const WATER_VERTEX = `varying vec3 world;
  void main(){vec4 p=modelMatrix*vec4(position,1.);world=p.xyz;
    gl_Position=projectionMatrix*viewMatrix*p;}`;
const WATER_WAVES = `uniform float time; uniform vec4 impacts[12];
  vec3 waves(vec2 p){
    vec2 slope=.005*cos(dot(p,vec2(2.7,1.4))+time*.65)*vec2(2.7,1.4)
      +.003*cos(dot(p,vec2(-1.6,3.9))-time*.48)*vec2(-1.6,3.9);
    float crest=0.;
    for(int i=0;i<12;i++){
      vec4 r=impacts[i];if(r.w>0.){
        vec2 delta=p-r.xy;float d=length(delta),q=d-r.z*.45;
        float fade=r.w*exp(-abs(q)*12.-r.z*.85);
        slope+=delta/max(d,.001)*.006*fade*(47.*cos(q*47.)-12.*sign(q)*sin(q*47.));
        crest+=pow(.5+.5*cos(q*47.),8.)*fade;
      }
    }return vec3(slope,crest);
  }`;
const RIPPLE_FRAGMENT = `precision highp float; varying vec3 world;
  ${WATER_WAVES}
  void main(){float glow=waves(world.xz).z;if(glow<.005)discard;
    gl_FragColor=vec4(.18,.75,.95,min(.65,glow*.45));
    #include <colorspace_fragment>
  }`;
const WATER_FRAGMENT = `precision highp float;
  varying vec3 world;
  uniform sampler2D below, depths;
  uniform mat4 inverseProjection, cameraWorld;
  uniform vec2 resolution;
  ${WATER_WAVES}
  vec3 pointAt(vec2 uv,float d){vec4 p=inverseProjection*vec4(uv*2.-1.,d*2.-1.,1.);
    p/=p.w;return (cameraWorld*p).xyz;}
  void main(){
    vec3 wave=waves(world.xz),normal=normalize(vec3(-wave.x,1.,-wave.y));
    vec3 view=normalize(cameraPosition-world);
    // Submerged geometry has already been projected through Snell's law.
    // Wave slopes add a small surface distortion without marching through a
    // single-view depth map (which produces stretched disocclusion trails).
    vec2 sampleUV=clamp(gl_FragCoord.xy/resolution+normal.xz*.005,.001,.999);
    float sceneDepth=texture2D(depths,sampleUV).r;
    vec3 hit=pointAt(sampleUV,sceneDepth);
    float path=max(0.,-hit.y)*1.333;
    vec2 blur=(.2+path*.9)/resolution;
    vec3 submerged=texture2D(below,sampleUV).rgb*.5;
    submerged+=texture2D(below,sampleUV+vec2(blur.x,0.)).rgb*.125;
    submerged+=texture2D(below,sampleUV-vec2(blur.x,0.)).rgb*.125;
    submerged+=texture2D(below,sampleUV+vec2(0.,blur.y)).rgb*.125;
    submerged+=texture2D(below,sampleUV-vec2(0.,blur.y)).rgb*.125;
    float fresnel=.0204+.9796*pow(1.-max(0.,dot(normal,view)),5.);
    // With no banks in view, reflect a quiet blue night sky. The wave
    // normals still vary Fresnel reflectance and the narrow moon highlights.
    vec3 reflectedRay=reflect(-view,normal);
    vec3 reflection=mix(vec3(.008,.016,.032),vec3(.045,.075,.12),max(0.,reflectedRay.y));
    vec3 color=mix(submerged,reflection,clamp(fresnel,.025,.92));
    vec3 sun=normalize(vec3(-3.,8.,4.));
    float glint=pow(max(0.,dot(reflect(-sun,normal),view)),180.);
    color+=vec3(.65,.8,1.)*glint*.12+vec3(.10,.22,.28)*wave.z*.15;
    gl_FragColor=vec4(color,1.);
    #include <tonemapping_fragment>
    #include <colorspace_fragment>
  }`;

export function renderSize(width,height,dpr=1,maxTexture=8192){
  // Native 4K, with retina sampling on smaller screens. Bound total pixels
  // rather than blurring every display to a 960-pixel-wide image.
  const ratio=Math.min(dpr,2,Math.sqrt(8388608/(width*height)),maxTexture/Math.max(width,height));
  return {ratio,width:Math.max(1,Math.floor(width*ratio)),height:Math.max(1,Math.floor(height*ratio))};
}
export function waterGeometry(depth=0){
  // Refraction is solved per vertex: subdivide the floor so distant grazing
  // rays cannot interpolate a false surface that occludes the fish.
  const subdivisions=depth?200:1;
  const g=new THREE.PlaneGeometry(200,200,subdivisions,subdivisions);g.rotateX(-Math.PI/2);g.translate(0,-depth,0);return g;
}

export function fishGeometry(model,f){
    const shape=model.volumeMesh(f),g=new THREE.BufferGeometry(),indices=[];
    g.setAttribute("position",new THREE.Float32BufferAttribute(shape.positions,3).setUsage(THREE.DynamicDrawUsage));
    g.setAttribute("uv",new THREE.Float32BufferAttribute(shape.uv,2));
    for(let i=0;i<shape.rings-1;i++)for(let j=0;j<shape.sides;j++){
      const a=i*(shape.sides+1)+j,b=a+shape.sides+1;indices.push(a,b,a+1,a+1,b,b+1);
    }
    // Close the snout and peduncle; they must also write correct scene depth.
    for(let j=1;j<shape.sides-1;j++){
      indices.push(0,j,j+1);
      const end=(shape.rings-1)*(shape.sides+1);indices.push(end,end+j+1,end+j);
    }g.setIndex(indices);g.computeVertexNormals();return g;
  }

export function create({node,loaded,canInteract,onMessage,onLook,onPause,look:initialLook="natural"}) {
  const model=window.ViraKoiPond,pond=model.create(),size=model.space;
  const canvas=document.createElement("canvas");
  let renderer;
  try{renderer=new THREE.WebGLRenderer({canvas,antialias:true,alpha:false,powerPreference:"low-power"});}
  catch(error){console.warn("3D pond unavailable:",error.message);return null;}
  renderer.debug.onShaderError=()=>{throw new Error("The 3D pond shader did not compile.");};
  const geometries=new Set(),materials=new Set(),textures=new Set(),targets=new Set();
  let cleanup=null;
  const ownGeometry=g=>{geometries.add(g);return g;};
  const ownMaterial=m=>{materials.add(m);return m;};
  const ownTexture=t=>{textures.add(t);return t;};
  try {
  const scene=new THREE.Scene(),submerged=new THREE.Group();
  scene.add(submerged);
  renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure=.95;renderer.shadowMap.enabled=true;
  renderer.shadowMap.type=THREE.PCFSoftShadowMap;
  const camera=new THREE.PerspectiveCamera(44,1,.08,60);
  const target=new THREE.Vector3(0,-.45,0);
  let azimuth=.08,elevation=.83,distance=7.4;
  const clock={value:0},underwaterPass={value:0};
  let seed=371;
  const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
  function canvasTexture(draw,w=256,h=w){
    const c=document.createElement("canvas");c.width=w;c.height=h;
    draw(c.getContext("2d"),w,h);const texture=ownTexture(new THREE.CanvasTexture(c));
    texture.colorSpace=THREE.SRGBColorSpace;return texture;
  }
  function grain(base,spread=28){
    return canvasTexture((ctx,w,h)=>{
      ctx.fillStyle=base;ctx.fillRect(0,0,w,h);
      const pixels=ctx.getImageData(0,0,w,h);
      const fields=[4,12,40].map(n=>({n,data:Array.from({length:n*n},()=>random()-.5)}));
      const noiseAt=(x,y,field)=>{
        const {n,data}=field,px=x/w*n,py=y/h*n,ix=Math.floor(px),iy=Math.floor(py);
        const fx=px-ix,fy=py-iy,tx=fx*fx*(3-2*fx),ty=fy*fy*(3-2*fy);
        const at=(x,y)=>data[(y%n)*n+(x%n)];
        return (at(ix,iy)*(1-tx)+at(ix+1,iy)*tx)*(1-ty)+(at(ix,iy+1)*(1-tx)+at(ix+1,iy+1)*tx)*ty;
      };
      for(let i=0;i<pixels.data.length;i+=4){
        const x=i/4%w,y=Math.floor(i/4/w);
        const noise=spread*(noiseAt(x,y,fields[0])*.65+noiseAt(x,y,fields[1])*.45+
          noiseAt(x,y,fields[2])*.25+(random()-.5)*.20);
        for(let k=0;k<3;k++)pixels.data[i+k]=Math.max(0,Math.min(255,pixels.data[i+k]+noise));
      }ctx.putImageData(pixels,0,0);
    });
  }
  function surfaceMaterial(base,roughness=1){
    const map=grain(base);map.wrapS=map.wrapT=THREE.RepeatWrapping;map.repeat.set(100,100);
    return ownMaterial(new THREE.MeshStandardMaterial({map,bumpMap:map,bumpScale:.065,roughness}));
  }
  function caustics(material){
    material.onBeforeCompile=shader=>{
      shader.uniforms.pondTime=clock;shader.uniforms.underwaterPass=underwaterPass;
      shader.vertexShader="varying vec3 pondWorld; varying float pondPath; uniform float underwaterPass;\n"+shader.vertexShader;
      shader.vertexShader=shader.vertexShader.replace("#include <project_vertex>",`
        vec4 pondPosition=vec4(transformed,1.);
        #ifdef USE_INSTANCING
          pondPosition=instanceMatrix*pondPosition;
        #endif
        pondPosition=modelMatrix*pondPosition;
        pondWorld=pondPosition.xyz;pondPath=0.;
        if(underwaterPass>.5 && pondPosition.y<0.){
          float d=-pondPosition.y,h=max(.05,cameraPosition.y);
          float range=length(cameraPosition.xz-pondPosition.xz),low=0.,high=range;
          // Solve the air/water exit point for this real 3D vertex. The
          // refracted projection and depth ordering share the same camera.
          for(int i=0;i<10;i++){
            float t=(low+high)*.5,a=range-t;
            if(1.333*t/sqrt(d*d+t*t)>a/sqrt(h*h+a*a))high=t;else low=t;
          }
          float t=(low+high)*.5;
          pondPath=sqrt(d*d+t*t);
          pondPosition.y=range<.0001?-d/1.333:-h*t/max(.0001,range-t);
        }
        vec4 mvPosition=viewMatrix*pondPosition;
        gl_Position=projectionMatrix*mvPosition;`);
      shader.fragmentShader="varying vec3 pondWorld; varying float pondPath; uniform float pondTime; uniform float underwaterPass;\n"+shader.fragmentShader;
      shader.fragmentShader=shader.fragmentShader.replace("#include <opaque_fragment>",`
        float caustic=pow(.5+.5*sin(pondWorld.x*17.+sin(pondWorld.z*11.+pondTime)*2.),7.);
        outgoingLight*=1.+caustic*.16*exp(min(0.,pondWorld.y)*.45);
        if(underwaterPass>.5){
          vec3 transmission=exp(-vec3(.68,.28,.43)*(pondPath+max(0.,-pondWorld.y)*.25));
          outgoingLight=outgoingLight*transmission+vec3(.006,.019,.025)*(1.-transmission);
        }
        #include <opaque_fragment>`);
    };return material;
  }
  // A procedural sky illuminates rough stone, wet scales and the water.
  const skyFaces=Array.from({length:6},(_,face)=>{
    const c=document.createElement("canvas");c.width=c.height=128;const ctx=c.getContext("2d");
    const gradient=ctx.createLinearGradient(0,0,0,128);
    gradient.addColorStop(0,face===3?"#0b1825":"#526a80");
    gradient.addColorStop(1,face===2?"#6e8391":"#344e61");ctx.fillStyle=gradient;ctx.fillRect(0,0,128,128);
    return c;
  });
  const sky=ownTexture(new THREE.CubeTexture(skyFaces));sky.needsUpdate=true;sky.colorSpace=THREE.SRGBColorSpace;
  scene.background=sky;
  const pmrem=new THREE.PMREMGenerator(renderer),environment=pmrem.fromCubemap(sky);
  targets.add(environment);
  scene.environment=environment.texture;pmrem.dispose();
  scene.add(new THREE.HemisphereLight(0xbcd5e8,0x112532,.65));
  const sun=new THREE.DirectionalLight(0xc7dfff,1.8);sun.position.set(-3,8,4);
  sun.castShadow=true;sun.shadow.mapSize.set(512,512);sun.shadow.camera.left=-5;sun.shadow.camera.right=5;
  sun.shadow.camera.top=5;sun.shadow.camera.bottom=-5;sun.shadow.camera.far=20;
  sun.shadow.normalBias=.025;sun.shadow.bias=-.0002;scene.add(sun);

  function mesh(geometry,material,group=submerged){
    const m=new THREE.Mesh(geometry,material);m.castShadow=true;m.receiveShadow=true;group.add(m);return m;
  }
  const bed=mesh(ownGeometry(waterGeometry(size.depth)),caustics(surfaceMaterial("#172c30",.95)),submerged);
  bed.castShadow=false;

  const baseColors=["#eee4d3","#c2a13b","#353b34","#acbac2"];
  const finColors=[0xd9d1b6,0xcbb457,0x949b89,0xb7c8cc];
  const bodyMaterials=loaded.slice(1).map((image,i)=>{
    const texture=canvasTexture((ctx,w,h)=>{ctx.fillStyle=baseColors[i];ctx.fillRect(0,0,w,h);ctx.drawImage(image,0,0,w,h);},1024,512);
    return caustics(ownMaterial(new THREE.MeshStandardMaterial({map:texture,roughness:.36,metalness:.035})));
  });
  const finMaterials=finColors.map(c=>caustics(ownMaterial(new THREE.MeshStandardMaterial({color:c,
    transparent:true,opacity:.48,side:THREE.DoubleSide,roughness:.45,depthWrite:false}))));
  const eyeMaterial=caustics(ownMaterial(new THREE.MeshPhysicalMaterial({color:0x151b16,roughness:.11,clearcoat:1})));
  const eyeGeometry=ownGeometry(new THREE.SphereGeometry(.006,10,8));
  const boneMaterial=caustics(ownMaterial(new THREE.MeshBasicMaterial({vertexColors:true,side:THREE.DoubleSide})));
  const bodyGeometry=f=>ownGeometry(fishGeometry(model,f));
  function finGeometry(points){
    const g=ownGeometry(new THREE.BufferGeometry());g.setAttribute("position",new THREE.Float32BufferAttribute(points.length*3,3).setUsage(THREE.DynamicDrawUsage));
    const indices=[];for(let i=1;i<points.length-1;i++)indices.push(0,i,i+1);g.setIndex(indices);return g;
  }
  const swimmers=pond.fish.map(f=>{
    const body=mesh(bodyGeometry(f),bodyMaterials[f.type],submerged);body.frustumCulled=false;
    const fins=[
      [[.24,-.060,0],[.27,-.14,-.008],[.31,-.20,-.016],[.35,-.21,-.022],[.40,-.17,-.025],[.43,-.10,-.012],[.39,-.055,0]],
      [[.24,.060,0],[.27,.14,-.008],[.31,.20,-.016],[.35,.21,-.022],[.40,.17,-.025],[.43,.10,-.012],[.39,.055,0]],
      [[.79,0,-.015],[.89,0,-.07],[.97,0,-.145],[1,0,-.15],[.94,0,0],[1,0,.15],[.97,0,.145],[.89,0,.07],[.79,0,.015]],
      [[.34,0,.06],[.39,0,.16],[.43,0,.17],[.49,0,.16],[.55,0,.13],[.61,0,.10],[.67,0,.025]],
      [[.52,-.052,-.03],[.58,-.12,-.05],[.64,-.075,-.04]],
      [[.52,.052,-.03],[.58,.12,-.05],[.64,.075,-.04]],
      [[.62,0,-.035],[.67,0,-.09],[.74,0,-.055]],
    ].map(points=>({points,mesh:mesh(finGeometry(points),finMaterials[f.type],submerged)}));
    fins.forEach(fin=>{fin.mesh.frustumCulled=false;fin.mesh.castShadow=false;});
    const eyes=[-1,1].map(()=>mesh(eyeGeometry,eyeMaterial,submerged));
    const skeleton=model.skeletalMesh(f),g=ownGeometry(new THREE.BufferGeometry());
    g.setAttribute("position",new THREE.Float32BufferAttribute(skeleton.positions,3).setUsage(THREE.DynamicDrawUsage));
    g.setAttribute("color",new THREE.Float32BufferAttribute(skeleton.colors,3));g.setIndex(skeleton.indices);
    const bones=new THREE.Mesh(g,boneMaterial);bones.userData.anatomy=true;bones.visible=false;
    bones.frustumCulled=false;submerged.add(bones);
    return {f,body,fins,eyes,bones};
  });
  const underTarget=new THREE.WebGLRenderTarget(1,1);targets.add(underTarget);
  underTarget.depthTexture=new THREE.DepthTexture(1,1,THREE.UnsignedIntType);
  const uniforms={below:{value:underTarget.texture},depths:{value:underTarget.depthTexture},
    resolution:{value:new THREE.Vector2(1,1)},time:clock,
    inverseProjection:{value:new THREE.Matrix4()},cameraWorld:{value:new THREE.Matrix4()},
    impacts:{value:Array.from({length:12},()=>new THREE.Vector4())}};
  const waterMaterial=ownMaterial(new THREE.ShaderMaterial({uniforms,vertexShader:WATER_VERTEX,fragmentShader:WATER_FRAGMENT,side:THREE.DoubleSide}));
  // The surface and floor share the camera but occupy separate elevations.
  const water=new THREE.Mesh(ownGeometry(waterGeometry()),waterMaterial);scene.add(water);
  // Style changes reuse the volume, skeletons and camera without restarting them.
  const naturalMaterials=new Map(),wireMaterials=new Map();
  scene.traverse(object=>{
    if(!object.isMesh || object.userData.anatomy)return;
    naturalMaterials.set(object,object.material);
    const fish=swimmers.find(s=>s.body===object || s.fins.some(f=>f.mesh===object));
    const tint=object===water?0x167f95:object===bed?0x714bba:
      fish?[0x7feef1,0xffad5b,0xf46ab4,0x7dabff][fish.f.type]:0x288889;
    const material=ownMaterial(object===water
      ?new THREE.ShaderMaterial({uniforms,vertexShader:WATER_VERTEX,fragmentShader:RIPPLE_FRAGMENT,
        transparent:true,depthWrite:false,side:THREE.DoubleSide})
      :new THREE.MeshBasicMaterial({color:tint,wireframe:true,
        transparent:!!fish,opacity:fish?.28:1,depthWrite:!fish}));
    wireMaterials.set(object,material);
  });
  const gridPoints=[];
  for(let i=-40;i<=40;i+=.5){
    gridPoints.push(i,-size.depth,-40,i,-size.depth,40,-40,-size.depth,i,40,-size.depth,i);
  }
  const bedWire=new THREE.LineSegments(ownGeometry(new THREE.BufferGeometry().setAttribute("position",new THREE.Float32BufferAttribute(gridPoints,3))),
    ownMaterial(new THREE.LineBasicMaterial({color:0x1d2845,transparent:true,opacity:.6})));bedWire.visible=false;submerged.add(bedWire);
  node.style.backgroundImage="";node.appendChild(canvas);
  canvas.dataset.pondDepth=String(size.depth);canvas.dataset.pondSpace="3d";
  let closed=false,running=true,exploring=false,failed=false,raf=0,last=0,painted=0,elapsed=0;
  let explorer=null,drag=null,ignoreClick=false,look="natural",lookButton=null,anatomyButton=null,motionButton=null,showBones=false;
  const raycaster=new THREE.Raycaster(),waterPlane=new THREE.Plane(new THREE.Vector3(0,1,0),0);
  function updateCamera(){
    camera.position.set(target.x+Math.sin(azimuth)*Math.cos(elevation)*distance,
      target.y+Math.sin(elevation)*distance,target.z+Math.cos(azimuth)*Math.cos(elevation)*distance);
    camera.lookAt(target);camera.updateMatrixWorld();
    uniforms.inverseProjection.value.copy(camera.projectionMatrixInverse);
    uniforms.cameraWorld.value.copy(camera.matrixWorld);
  }
  function updateFish(){
    for(const {f,body,fins,eyes,bones} of swimmers){
      const shape=model.volumeMesh(f);body.geometry.attributes.position.array.set(shape.positions);
      body.geometry.attributes.position.needsUpdate=true;body.geometry.computeVertexNormals();
      for(let i=0;i<fins.length;i++){
        const fin=fins[i],position=fin.mesh.geometry.attributes.position;
        fin.points.forEach(([s,lateral,up],j)=>{
          const flexible=Math.sin(Math.PI*j/(fin.points.length-1));
          const flap=i<2 ? (Math.sin(f.finPhase+i*.6)*.024+Math.sin(f.pitch)*.035)*flexible : 0;
          const p=model.bodyPoint(f,s,lateral*(i<2?(i?f.finRight:f.finLeft):1),up+flap);position.setXYZ(j,p.x,p.y,p.z);
        });position.needsUpdate=true;fin.mesh.geometry.computeVertexNormals();
      }
      eyes.forEach((eye,i)=>{const p=model.bodyPoint(f,.10,(i?1:-1)*.049,.025);eye.position.set(p.x,p.y,p.z);});
      if(bones.visible){bones.geometry.attributes.position.array.set(model.skeletalMesh(f).positions);bones.geometry.attributes.position.needsUpdate=true;}
    }
    uniforms.impacts.value.forEach((r,i)=>{
      const p=pond.drops[i];p?r.set((p.x-.5)*size.width,(p.y-.5)*size.length,p.age,p.strength):r.set(0,0,0,0);
    });
  }
  function paint(){
    if(closed || failed)return;
    updateCamera();updateFish();
    if(look==="wireframe"){
      submerged.visible=true;water.visible=true;
      renderer.setRenderTarget(null);renderer.render(scene,camera);return;
    }
    water.visible=false;submerged.visible=true;
    underwaterPass.value=1;renderer.setRenderTarget(underTarget);renderer.render(scene,camera);underwaterPass.value=0;
    water.visible=true;renderer.setRenderTarget(null);renderer.render(scene,camera);
  }
  function resize(){
    const {ratio,width:w,height:h}=renderSize(innerWidth,innerHeight,devicePixelRatio || 1,renderer.capabilities.maxTextureSize);
    renderer.setPixelRatio(ratio);renderer.setSize(innerWidth,innerHeight,false);
    underTarget.setSize(w,h);canvas.dataset.renderWidth=String(w);canvas.dataset.renderHeight=String(h);
    uniforms.resolution.value.set(w,h);camera.aspect=innerWidth/innerHeight;camera.updateProjectionMatrix();
    paint();
  }
  function tick(now){
    raf=0;if(closed || document.hidden || failed)return;
    const dt=last?Math.min((now-last)/1000,.25):0;last=now;
    if(running){elapsed+=dt;clock.value=elapsed;pond.step(dt);}
    if(now-painted>=1000/30){paint();painted=now;}
    if(running || drag)raf=requestAnimationFrame(tick);
  }
  function wake(){cancelAnimationFrame(raf);raf=0;last=0;if(!closed && !document.hidden && !failed && (running || drag))raf=requestAnimationFrame(tick);}
  function pointAt(clientX,clientY){
    const rect=canvas.getBoundingClientRect();raycaster.setFromCamera(new THREE.Vector2(
      (clientX-rect.left)/rect.width*2-1,1-(clientY-rect.top)/rect.height*2),camera);
    const point=raycaster.ray.intersectPlane(waterPlane,new THREE.Vector3());
    if(!point)return null;
    const p={x:.5+point.x/size.width,y:.5+point.z/size.length};return p;
  }
  function flickAt(clientX,clientY){
    if(!running || document.hidden || failed)return;
    const p=pointAt(clientX,clientY);if(p && pond.flick(p.x,p.y))paint();
  }
  function click(e){
    if(e.button!==0 || e.defaultPrevented || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey)return;
    if(exploring){if(e.target!==canvas || ignoreClick){ignoreClick=false;return;}}
    else if(!canInteract?.(e.target) || e.target.closest?.("button,a,input,textarea,select,[contenteditable],#background-picker,.ctx-menu,.ctx-pop,#reminder-stickies"))return;
    if(pointAt(e.clientX,e.clientY) && running){flickAt(e.clientX,e.clientY);e.preventDefault();}
  }
  function doubleClick(e){
    if(running && (exploring?e.target===canvas:canInteract?.(e.target)) && pointAt(e.clientX,e.clientY))e.preventDefault();
  }
  function pointerDown(e){
    if(!exploring || e.button!==0 || e.target!==canvas)return;
    drag={id:e.pointerId,x:e.clientX,y:e.clientY,startX:e.clientX,startY:e.clientY};
    canvas.setPointerCapture(e.pointerId);e.preventDefault();wake();
  }
  function pointerMove(e){
    if(!drag){
      const allowed=exploring?e.target===canvas:canInteract?.(e.target);
      const p=allowed && pointAt(e.clientX,e.clientY);pond.follow(p?.x,p?.y);return;
    }
    pond.follow(null);if(e.pointerId!==drag.id)return;
    const dx=e.clientX-drag.x,dy=e.clientY-drag.y;
    azimuth-=dx*.004;elevation=Math.max(.55,Math.min(1.38,elevation+dy*.003));
    drag.x=e.clientX;drag.y=e.clientY;
    if(Math.hypot(e.clientX-drag.startX,e.clientY-drag.startY)>4)ignoreClick=true;
    paint();
  }
  function pointerUp(e){if(drag?.id===e.pointerId){drag=null;if(canvas.hasPointerCapture(e.pointerId))canvas.releasePointerCapture(e.pointerId);wake();}}
  function wheel(e){if(!exploring)return;e.preventDefault();distance=Math.max(4.3,Math.min(12,distance*Math.exp(e.deltaY*.001)));paint();}
  function home(){azimuth=.08;elevation=.83;distance=7.4;paint();}
  function setLook(value){
    look=value==="wireframe"?"wireframe":"natural";
    const wire=look==="wireframe";
    for(const [object,material] of (wire?wireMaterials:naturalMaterials))object.material=material;
    scene.background=wire?new THREE.Color(0x050a16):sky;
    scene.environment=wire?null:environment.texture;bed.visible=!wire;bedWire.visible=wire;
    water.visible=true;
    renderer.shadowMap.enabled=!wire;canvas.dataset.pondLook=look;
    swimmers.forEach(s=>{s.bones.visible=wire || showBones;s.body.visible=!showBones;});
    if(lookButton)lookButton.textContent=wire?"Show natural pond":"Show cyberpunk wireframe";
    paint();
  }
  function leave(){
    if(!exploring)return;exploring=false;drag=null;ignoreClick=false;
    node.appendChild(canvas);explorer.remove();explorer=null;lookButton=null;anatomyButton=null;motionButton=null;wake();
    document.querySelector(".background-explore")?.focus();
  }
  function explore(){
    if(closed || failed || exploring)return;
    exploring=true;explorer=document.createElement("section");explorer.id="pond-explorer";
    explorer.setAttribute("role","dialog");explorer.setAttribute("aria-modal","true");explorer.setAttribute("aria-label","Explore koi pond");
    const bar=document.createElement("div");bar.className="pond-explorer-bar";
    const caption=document.createElement("div");caption.innerHTML=`<strong>Open water</strong>
      <span>Depth: 1.8 metres. Drag to orbit. Scroll to zoom. Move to draw koi closer. Click to ripple the water.</span>`;
    bar.appendChild(caption);
    lookButton=document.createElement("button");
    lookButton.textContent=look==="wireframe"?"Show natural pond":"Show cyberpunk wireframe";
    lookButton.addEventListener("click",()=>{
      const next=look==="wireframe"?"natural":"wireframe";
      if(onLook)onLook(next);else setLook(next);
    });bar.appendChild(lookButton);
    anatomyButton=document.createElement("button");anatomyButton.textContent=showBones?"Show skin":"Show anatomy";
    anatomyButton.addEventListener("click",()=>{
      showBones=!showBones;anatomyButton.textContent=showBones?"Show skin":"Show anatomy";setLook(look);
    });bar.appendChild(anatomyButton);
    motionButton=document.createElement("button");motionButton.textContent=running?"Pause motion":"Resume motion";
    motionButton.addEventListener("click",()=>{if(onPause)onPause();else dispose.motion(!running);});bar.appendChild(motionButton);
    for(const [label,action] of [["Reset view",home],["Return to desktop",leave]]){
      const button=document.createElement("button");button.textContent=label;button.addEventListener("click",action);bar.appendChild(button);
    }
    explorer.append(canvas,bar);document.body.appendChild(explorer);
    explorer.addEventListener("keydown",e=>{
      if(e.key==="Escape"){e.preventDefault();e.stopPropagation();leave();}
      else if(e.key==="ArrowLeft" || e.key==="ArrowRight"){azimuth+=(e.key==="ArrowLeft"?-.12:.12);e.preventDefault();e.stopPropagation();paint();}
      else if(e.key==="ArrowUp" || e.key==="ArrowDown"){elevation=Math.max(.55,Math.min(1.38,elevation+(e.key==="ArrowUp"?.08:-.08)));e.preventDefault();e.stopPropagation();paint();}
      else if(e.key==="Tab"){
        const controls=[canvas,...bar.querySelectorAll("button")],index=controls.indexOf(document.activeElement);
        e.preventDefault();e.stopPropagation();controls[(index+(e.shiftKey?-1:1)+controls.length)%controls.length].focus();
      }
    });
    canvas.tabIndex=0;canvas.setAttribute("aria-label","Orbit the pond with arrow keys, or drag with the pointer");canvas.focus();paint();
  }
  function contextLost(e){e.preventDefault();failed=true;cancelAnimationFrame(raf);raf=0;leave();
    node.style.backgroundImage="url('/backgrounds/open-water.svg')";canvas.style.display="none";
    dispose.explore=null;
    onMessage?.("3D pond stopped: graphics context lost. Choose the pond again to retry.");}
  document.addEventListener("click",click);document.addEventListener("dblclick",doubleClick,true);
  document.addEventListener("visibilitychange",wake);window.addEventListener("resize",resize);
  canvas.addEventListener("pointerdown",pointerDown);document.addEventListener("pointermove",pointerMove);
  const clearPointer=()=>pond.follow(null);window.addEventListener("blur",clearPointer);document.addEventListener("pointerleave",clearPointer);
  canvas.addEventListener("pointerup",pointerUp);canvas.addEventListener("pointercancel",pointerUp);
  canvas.addEventListener("wheel",wheel,{passive:false});canvas.addEventListener("webglcontextlost",contextLost);
  const dispose=()=>{
    if(closed)return;closed=true;leave();cancelAnimationFrame(raf);
    document.removeEventListener("click",click);document.removeEventListener("dblclick",doubleClick,true);
    document.removeEventListener("visibilitychange",wake);window.removeEventListener("resize",resize);
    canvas.removeEventListener("pointerdown",pointerDown);document.removeEventListener("pointermove",pointerMove);
    window.removeEventListener("blur",clearPointer);document.removeEventListener("pointerleave",clearPointer);
    canvas.removeEventListener("pointerup",pointerUp);canvas.removeEventListener("pointercancel",pointerUp);
    canvas.removeEventListener("wheel",wheel);canvas.removeEventListener("webglcontextlost",contextLost);
    targets.forEach(t=>t.dispose());sun.shadow.map?.dispose();
    geometries.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());textures.forEach(t=>t.dispose());
    renderer.dispose();renderer.forceContextLoss();canvas.remove();
  };
  dispose.motion=enabled=>{running=enabled;if(motionButton)motionButton.textContent=running?"Pause motion":"Resume motion";wake();};
  dispose.explore=explore;dispose.message="";
  dispose.look=setLook;cleanup=dispose;
  setLook(initialLook);resize();wake();
  return dispose;
  }catch(error){
    if(cleanup)cleanup();
    else{
      targets.forEach(t=>t.dispose());geometries.forEach(g=>g.dispose());
      materials.forEach(m=>m.dispose());textures.forEach(t=>t.dispose());
      renderer.dispose();renderer.forceContextLoss();canvas.remove();
    }
    console.warn("3D pond unavailable:",error.message);return null;
  }
}
