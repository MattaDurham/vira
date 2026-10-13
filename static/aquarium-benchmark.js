import * as THREE from './vendor/three.webgpu.js';
import {GLTFLoader} from './vendor/GLTFLoader.js';
import {cameraPosition,positionWorld,output,uniform,vec3,vec4,max,exp,sin,pow} from './vendor/three.tsl.js';
import {createSwimmer} from './aquarium-motion.js';
import {applySwimPose} from './aquarium-rig.js';

const $=id=>document.getElementById(id),canvas=$('aquarium'),loading=$('loading');
const swimmer=createSwimmer(),fish=swimmer.fish,clock=uniform(0);
const state={paused:matchMedia('(prefers-reduced-motion: reduce)').matches,reference:false,wireframe:false};
let renderer,scene,camera,asset,environment,disposed=false,frame=0,previous=0,metrics=0;
let liveEngine='Starting renderer',referenceMetadata=null;
const ownedGeometries=new Set(),ownedMaterials=new Set(),ownedTextures=new Set(),joints=[],rest=[];
const frameTimes=[];
let seed=913;
const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
const ownGeometry=g=>{ownedGeometries.add(g);return g;},ownMaterial=m=>{ownedMaterials.add(m);return m;};
const ownTexture=t=>{ownedTextures.add(t);return t;};
let orbit=0,elevation=0,distance=4.1;
const target=new THREE.Vector3(0,.86,-.2);
const raycaster=new THREE.Raycaster(),pointer=new THREE.Vector2(),interactionPlane=new THREE.Plane(new THREE.Vector3(0,0,1),-.14),hit=new THREE.Vector3();
const deformable=[];
const fins=[];

function size(){
  const width=canvas.clientWidth,height=canvas.clientHeight;
  // Preserve native 4K while bounding sustained GPU work on extreme displays.
  const ratio=Math.min(devicePixelRatio,2,Math.sqrt(8388608/(width*height)));
  renderer.setPixelRatio(ratio);renderer.setSize(width,height,false);
  camera.aspect=width/height;
  camera.fov=width<650?64:THREE.MathUtils.radToDeg(2*Math.atan(18/(47*camera.aspect)));
  camera.updateProjectionMatrix();
  $('pixels').textContent=`${canvas.width.toLocaleString()} × ${canvas.height.toLocaleString()}`;
  canvas.dataset.renderWidth=String(canvas.width);canvas.dataset.renderHeight=String(canvas.height);
}
function setCamera(){
  camera.position.set(Math.sin(orbit)*distance,1.02+elevation,Math.cos(orbit)*distance);
  camera.lookAt(target);camera.updateMatrixWorld();
}
function immerse(mat){
  const material=ownMaterial(new THREE.MeshPhysicalNodeMaterial());
  // glTF supplies ordinary materials. NodeMaterial.copy also copies shader
  // hooks, which do not exist on those sources and must retain their defaults.
  THREE.MeshStandardMaterial.prototype.copy.call(material,mat);
  if(mat.isMeshPhysicalMaterial)THREE.MeshPhysicalMaterial.prototype.copy.call(material,mat);
  if(mat.name==='Ivory fin membrane'){
    // The front glass samples the opaque transmission buffer, excluding
    // alpha-blended membranes. Keep fins in that buffer and approximate
    // their thin tissue with a pale diffuse material and faint backlighting.
    material.transmission=0;material.opacity=1;material.transparent=false;
    material.depthWrite=true;material.clearcoat=.12;material.color.setRGB(.40,.49,.43);
    material.emissive.setRGB(.013,.020,.019);
  }
  // The observer is outside the front pane. Only the path through water absorbs.
  const waterPath=max(cameraPosition.sub(positionWorld).length().mul(max(positionWorld.z.negate().add(.69),0)).div(cameraPosition.z.sub(positionWorld.z)),0);
  const transmission=exp(vec3(.26,.085,.065).mul(waterPath).negate());
  const waterColor=vec3(.006,.019,.027);
  const caustic=pow(sin(positionWorld.x.mul(12).add(sin(positionWorld.z.mul(9).add(clock)))).mul(.5).add(.5),12);
  material.outputNode=vec4(output.rgb.mul(transmission).mul(caustic.mul(.045).add(1)).add(waterColor.mul(transmission.oneMinus())),output.a);
  return material;
}
function pebbleMaterial(){
  const image=document.createElement('canvas');image.width=image.height=256;
  const ctx=image.getContext('2d'),pixels=ctx.createImageData(256,256);
  for(let i=0;i<pixels.data.length;i+=4){const value=21+random()*22;pixels.data[i]=value*.86;pixels.data[i+1]=value*.98;pixels.data[i+2]=value;pixels.data[i+3]=255;}
  ctx.putImageData(pixels,0,0);
  const map=ownTexture(new THREE.CanvasTexture(image));map.colorSpace=THREE.SRGBColorSpace;
  map.wrapS=map.wrapT=THREE.RepeatWrapping;map.repeat.set(4,4);
  return immerse(new THREE.MeshPhysicalMaterial({map,bumpMap:map,bumpScale:.015,roughness:.89,color:0x40565c}));
}
function mesh(geometry,material,position){
  const item=new THREE.Mesh(ownGeometry(geometry),material);
  if(position)item.position.set(...position);item.receiveShadow=true;scene.add(item);return item;
}
function softboxEnvironment(){
  const room=new THREE.Scene();room.background=new THREE.Color(.055,.082,.10);
  const boxes=[[[0,3,0],[3,1.2],new THREE.Color(3.6,4.0,4.7)],[[2,1.1,0],[1.6,.7],new THREE.Color(1.4,2.1,2.6)],[[-2,.8,1],[1.2,.6],new THREE.Color(.65,.95,1.2)]];
  for(const [position,dimensions,color] of boxes){
    const plane=new THREE.Mesh(new THREE.PlaneGeometry(...dimensions),new THREE.MeshBasicMaterial({color,side:THREE.DoubleSide}));plane.position.set(...position);plane.lookAt(0,0,0);room.add(plane);
  }
  const generator=new THREE.PMREMGenerator(renderer);
  environment=generator.fromScene(room,.035,.05,20,{size:256});scene.environment=environment.texture;scene.environmentIntensity=.72;
  generator.dispose();room.traverse(o=>{o.geometry?.dispose();o.material?.dispose();});
}
function habitat(){
  const ground=pebbleMaterial(),rock=immerse(new THREE.MeshPhysicalMaterial({color:0x253c40,roughness:.68,clearcoat:.12}));
  mesh(new THREE.BoxGeometry(4, .09,1.8),ground,[0,-.025,-.2]);
  const pebbles=new THREE.InstancedMesh(ownGeometry(new THREE.IcosahedronGeometry(1,1)),ground,950),dummy=new THREE.Object3D();
  for(let i=0;i<950;i++){
    const radius=.007+random()*.018;dummy.position.set((random()-.5)*4,radius*.45,(random()-.5)*1.68-.2);
    dummy.scale.set(radius*(.8+random()*.5),radius*(.4+random()*.35),radius*(.7+random()*.5));
    dummy.rotation.set(random()*3,random()*3,random()*3);dummy.updateMatrix();pebbles.setMatrixAt(i,dummy.matrix);
  }
  pebbles.receiveShadow=true;scene.add(pebbles);
  for(const [x,z,r] of [[-1.63,-.39,.25],[-1.27,-.64,.13],[1.64,-.68,.28],[1.23,-.78,.15]]){
    const geometry=new THREE.IcosahedronGeometry(r,4),positions=geometry.attributes.position;
    // Coincident corners must move together; random offsets split triangles
    // and turn smooth stone into a pile of disconnected facets.
    for(let i=0;i<positions.count;i++){
      const px=positions.getX(i),py=positions.getY(i),pz=positions.getZ(i);
      const scale=1+.025*Math.sin(px*31+py*17)*Math.cos(pz*23);
      positions.setXYZ(i,px*scale,py*scale,pz*scale);
    }
    const stone=mesh(geometry,rock,[x,r*.4,z]);stone.scale.set(1.2,.73,.8);stone.castShadow=true;
  }
  mesh(new THREE.PlaneGeometry(4,1.95),ownMaterial(new THREE.MeshBasicNodeMaterial({color:0x030f18})),[0,.9,-1.11]);
  const surface=ownMaterial(new THREE.MeshPhysicalNodeMaterial({color:0x6d929e,roughness:.065,metalness:.25,transmission:.25,thickness:.006,ior:1.333,side:THREE.DoubleSide}));
  const water=mesh(new THREE.PlaneGeometry(4,1.78,64,32),surface,[0,1.79,-.2]);water.rotation.x=-Math.PI/2;
  const points=water.geometry.attributes.position,base=Float32Array.from(points.array);deformable.push({points,base});
  const glass=ownMaterial(new THREE.MeshPhysicalNodeMaterial({color:0xd7f0ed,roughness:.022,transmission:1,thickness:.012,ior:1.46,clearcoat:1,clearcoatRoughness:.01,side:THREE.DoubleSide}));
  const pane=mesh(new THREE.PlaneGeometry(4,1.9),glass,[0,.9,.69]);pane.receiveShadow=false;
  const edge=ownMaterial(new THREE.MeshPhysicalNodeMaterial({color:0x446b74,roughness:.18,metalness:.25,transparent:true,opacity:.35}));
  for(const x of [-1.98,1.98])mesh(new THREE.BoxGeometry(.014,1.86,.025),edge,[x,.92,.68]);
  const particulate=ownMaterial(new THREE.MeshBasicNodeMaterial({color:0x57747d,transparent:true,opacity:.19}));
  const particles=new THREE.InstancedMesh(ownGeometry(new THREE.IcosahedronGeometry(.0016,0)),particulate,180);
  for(let i=0;i<180;i++){dummy.position.set((random()-.5)*3.7,.1+random()*1.6,(random()-.5)*1.5-.2);dummy.scale.setScalar(.5+random());dummy.updateMatrix();particles.setMatrixAt(i,dummy.matrix);}
  scene.add(particles);
  scene.add(new THREE.HemisphereLight(0xc6dce3,0x0b252e,.32));
  for(const [position,targetPosition,power,color,angle] of [
    [[.45,2.35,.15],[.2,.4,-.1],19,0xc2dbff,.85],
    [[-1.7,1.4,.3],[.2,.7,-.1],5,0x64a9cd,.8],
    [[1,1.8,-.9],[.2,.7,-.1],11,0x8fcfe6,.7],
  ]){
    const light=new THREE.SpotLight(color,power,8,angle,.7,2);light.position.set(...position);light.target.position.set(...targetPosition);
    light.castShadow=true;light.shadow.mapSize.set(1024,1024);light.shadow.normalBias=.001;light.shadow.bias=-.0002;
    scene.add(light,light.target);
  }
}

function pointerPoint(event){
  const bounds=canvas.getBoundingClientRect();pointer.set((event.clientX-bounds.left)/bounds.width*2-1,-(event.clientY-bounds.top)/bounds.height*2+1);
  raycaster.setFromCamera(pointer,camera);return raycaster.ray.intersectPlane(interactionPlane,hit)?{x:hit.x,y:hit.y,z:.14}:null;
}
let drag=null;
function pointerDown(event){if(state.reference)return;drag={id:event.pointerId,x:event.clientX,y:event.clientY,lastX:event.clientX,lastY:event.clientY,moved:false};canvas.setPointerCapture(event.pointerId);swimmer.follow(null);}
function pointerMove(event){
  if(state.reference)return;
  if(drag&&drag.id===event.pointerId){
    if(Math.hypot(event.clientX-drag.x,event.clientY-drag.y)>5)drag.moved=true;
    if(drag.moved){orbit=THREE.MathUtils.clamp(orbit+(event.clientX-drag.lastX)*.0015,-.28,.28);elevation=THREE.MathUtils.clamp(elevation+(event.clientY-drag.lastY)*.001,-.15,.20);setCamera();}
    drag.lastX=event.clientX;drag.lastY=event.clientY;
  }else swimmer.follow(pointerPoint(event));
}
function pointerUp(event){if(drag&&event.pointerId===drag.id){if(!drag.moved)swimmer.flick(pointerPoint(event));drag=null;}}
function pointerCancel(){drag=null;}
function pointerLeave(){if(!drag)swimmer.follow(null);}
function wheel(event){if(state.reference)return;event.preventDefault();distance=THREE.MathUtils.clamp(distance+event.deltaY*.002,3.3,4.7);setCamera();}
function key(event){
  if(event.key==='ArrowLeft')orbit=Math.max(-.28,orbit-.04);
  else if(event.key==='ArrowRight')orbit=Math.min(.28,orbit+.04);
  else if(event.key==='ArrowUp')elevation=Math.min(.2,elevation+.04);
  else if(event.key==='ArrowDown')elevation=Math.max(-.15,elevation-.04);
  else return;event.preventDefault();setCamera();
}
function pose(){
  applySwimPose(asset,joints,rest,fish);
}
function draw(now){
  if(disposed)return;frame=requestAnimationFrame(draw);
  const elapsed=previous?(now-previous)/1000:0;previous=now;
  if(document.hidden||state.reference)return;
  if(!state.paused){swimmer.step(elapsed);clock.value=fish.age;pose();}
  for(const {points,base,paired,root,sign} of fins){
    for(let i=0;i<points.count;i++){
      const x=base[i*3],y=base[i*3+1],z=base[i*3+2];
      if(paired){
        const a=(.10+.20*fish.speed)*Math.sin(fish.phase*.8)*sign,dy=y-root.y,dz=z-root.z;
        points.setXYZ(i,x,root.y+dy*Math.cos(a)-dz*Math.sin(a),root.z+dy*Math.sin(a)+dz*Math.cos(a));
      }else points.setXYZ(i,x,y,z+.004*Math.sin(x*16-fish.phase)*Math.max(0,x-.45));
    }
    points.needsUpdate=true;
  }
  for(const {points,base} of deformable){
    for(let i=0;i<points.count;i++)points.setZ(i,base[i*3+2]+.0015*Math.sin(base[i*3]*5+fish.age*.8)*Math.cos(base[i*3+1]*6-fish.age*.3));
    points.needsUpdate=true;
  }
  renderer.render(scene,camera);
  if(elapsed>0&&elapsed<.25){frameTimes.push(elapsed*1000);if(frameTimes.length>180)frameTimes.shift();}
  if(now-metrics>1000&&frameTimes.length>20){
    const average=frameTimes.reduce((a,b)=>a+b,0)/frameTimes.length;
    $('performance').textContent=`${Math.round(1000/average)} fps / ${average.toFixed(1)} ms`;
    canvas.dataset.fps=String(Math.round(1000/average));metrics=now;
  }
}
function setPause(paused){state.paused=paused;$('motion').textContent=paused?'Resume motion':'Pause motion';$('motion').setAttribute('aria-pressed',String(paused));}
function setView(reference){
  state.reference=reference;$('reference').hidden=!reference;$('live-view').setAttribute('aria-pressed',String(!reference));$('reference-view').setAttribute('aria-pressed',String(reference));
  $('motion').disabled=$('reset').disabled=$('wireframe').disabled=$('light').disabled=reference;
  $('hint').textContent=reference?'Cycles reference: the same authored fish, rendered offline through glass and volumetric water.':'Move to invite the koi closer. Tap to startle or interest it. Drag to look around.';
  $('engine').textContent=reference?'Cycles / offline':liveEngine;
  $('performance').textContent=reference&&referenceMetadata?`${referenceMetadata.samples} samples / 4K still`:reference?'Offline still':'Measuring';
  previous=0;frameTimes.length=0;
}
function cleanup(){
  disposed=true;cancelAnimationFrame(frame);window.removeEventListener('resize',size);
  canvas.removeEventListener('pointerdown',pointerDown);canvas.removeEventListener('pointermove',pointerMove);canvas.removeEventListener('pointerup',pointerUp);canvas.removeEventListener('pointercancel',pointerCancel);canvas.removeEventListener('pointerleave',pointerLeave);canvas.removeEventListener('wheel',wheel);canvas.removeEventListener('keydown',key);
  ownedGeometries.forEach(g=>g.dispose());ownedMaterials.forEach(m=>m.dispose());ownedTextures.forEach(t=>t.dispose());environment?.dispose();renderer?.dispose();
}

async function start(){
  const forceWebGL=new URLSearchParams(location.search).get('renderer')==='webgl';
  renderer=new THREE.WebGPURenderer({canvas,antialias:true,alpha:false,powerPreference:'high-performance',forceWebGL});
  await renderer.init();
  renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.AgXToneMapping;renderer.toneMappingExposure=1;
  renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
  scene=new THREE.Scene();scene.background=new THREE.Color(0x030b12);
  camera=new THREE.PerspectiveCamera(26,1,.06,25);setCamera();size();softboxEnvironment();habitat();
  const gltf=await new GLTFLoader().loadAsync('aquarium/koi.glb');asset=gltf.scene;scene.add(asset);
  const converted=new Map();
  asset.traverse(object=>{
    if(object.isBone&&object.name.startsWith('spine_'))joints.push(object);
    if(object.isMesh){
      ownedGeometries.add(object.geometry);object.castShadow=object.receiveShadow=true;object.frustumCulled=false;
      if(/Caudal|Pectoral|Pelvic/.test(object.name)){
        const points=object.geometry.attributes.position,base=Float32Array.from(points.array);
        const paired=!object.name.startsWith('Caudal'),pelvic=object.name.startsWith('Pelvic');
        let z=0;for(let i=0;i<points.count;i++)z+=points.getZ(i);
        const sign=Math.sign(z)||1;
        fins.push({points,base,paired,sign,root:{y:pelvic?-.11:-.036,z:sign*(pelvic?.075:.105)}});
      }
      const replace=mat=>{if(!converted.has(mat)){converted.set(mat,immerse(mat));for(const value of Object.values(mat))if(value?.isTexture)ownedTextures.add(value);mat.dispose();}return converted.get(mat);};
      object.material=Array.isArray(object.material)?object.material.map(replace):replace(object.material);
    }
  });
  joints.sort((a,b)=>a.name.localeCompare(b.name));joints.forEach(b=>rest.push(b.quaternion.clone()));pose();
  liveEngine=renderer.backend.isWebGPUBackend?'WebGPU / local GPU':'WebGL 2 fallback';
  $('engine').textContent=liveEngine;
  canvas.dataset.engine=renderer.backend.isWebGPUBackend?'webgpu':'webgl2';canvas.dataset.joints=String(joints.length);
  canvas.dataset.ready='true';
  await renderer.compileAsync(scene,camera);renderer.render(scene,camera);loading.hidden=true;
  fetch('aquarium/manifest.json').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();}).then(manifest=>{referenceMetadata=manifest.reference;}).catch(error=>console.warn('Reference metadata unavailable:',error));
  setPause(state.paused);
  canvas.addEventListener('pointerdown',pointerDown);canvas.addEventListener('pointermove',pointerMove);canvas.addEventListener('pointerup',pointerUp);canvas.addEventListener('pointercancel',pointerCancel);canvas.addEventListener('pointerleave',pointerLeave);canvas.addEventListener('wheel',wheel,{passive:false});canvas.addEventListener('keydown',key);
  window.addEventListener('resize',size);window.addEventListener('pagehide',cleanup,{once:true});
  $('motion').addEventListener('click',()=>setPause(!state.paused));
  $('reset').addEventListener('click',()=>{swimmer.reset();orbit=elevation=0;distance=4.1;setCamera();pose();setPause(true);});
  $('wireframe').addEventListener('click',()=>{state.wireframe=!state.wireframe;$('wireframe').setAttribute('aria-pressed',String(state.wireframe));converted.forEach(mat=>{mat.wireframe=state.wireframe;mat.needsUpdate=true;});});
  $('reference-view').addEventListener('click',()=>setView(true));$('live-view').addEventListener('click',()=>setView(false));
  $('light').addEventListener('input',()=>{renderer.toneMappingExposure=Number($('light').value);$('light-value').value=`${Math.round(Number($('light').value)*100)}%`;});
  frame=requestAnimationFrame(draw);
}
start().catch(error=>{console.error('Aquarium benchmark:',error);loading.textContent=`Aquarium unavailable: ${error.message}`;canvas.dataset.ready='error';cleanup();});
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
