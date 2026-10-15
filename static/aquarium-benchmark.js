import * as THREE from './vendor/three.webgpu.js';
import {GLTFLoader} from './vendor/GLTFLoader.js';
import {mergeVertices} from './vendor/BufferGeometryUtils.js';
import {RectAreaLightTexturesLib} from './vendor/RectAreaLightTexturesLib.js';
import {cameraPosition,positionWorld,positionLocal,positionGeometry,output,uniform,vec3,vec4,float,max,min,step,abs,exp,sin,pow,uv,smoothstep,mx_noise_float,bumpMap,texture,triplanarTexture} from './vendor/three.tsl.js';
import {createSwimmer,waterFlow} from './aquarium-motion.js?v=74';
import {createStudyRandom,createTurnStudy} from './aquarium-study.js?v=74';
import {createFinFrame,createFinController,bindFinGeometry,deformFinGeometry} from './aquarium-fins.js?v=74';
import {createFinNormalSmoother} from './aquarium-fin-normals.js?v=74';
import {finRayContrast} from './aquarium-fin-detail.js?v=74';
import {applySwimPose,createFacialRig} from './aquarium-rig.js?v=74';
import {waterOptics} from './aquarium-optics.js?v=74';
import {createContainment} from './aquarium-collision.js?v=74';
import {createWaterInterface,waterExtinction} from './aquarium-water.js?v=74';
import {waterMin,waterMax,tankCenterZ,tankDepth} from './aquarium-space.js?v=74';
import {createResearchLayer} from './aquarium-research.js?v=74';

const $=id=>document.getElementById(id),canvas=$('aquarium'),loading=$('loading');
const requestedStudySeed=new URLSearchParams(location.search).get('seed');
const studyRandom=createStudyRandom(requestedStudySeed===null?913:Number(requestedStudySeed));
let turnReplay=false,turnPoseKey=0;
const swimmer=createSwimmer(()=>turnReplay?studyRandom():Math.random()),fish=swimmer.fish,clock=uniform(0);
const rayDetail=uniform(1),rayDetailPosition=new THREE.Vector3();
const fullRayDetail=new URLSearchParams(location.search).get('ray-detail')==='full';
const state={paused:matchMedia('(prefers-reduced-motion: reduce)').matches,reference:false,wireframe:false,study:false,studyAxis:'combined',closeup:false,cameraAngle:'front',research:false};
let renderer,scene,camera,asset,environment,disposed=false,previous=0,metrics=0;
let liveEngine='Starting renderer',referenceMetadata=null;
let skeleton;
let optics;
let waterInterface;
let containment,facialRig;
let research;
let recording,recordingStream,recordingTimer,recordingUrl,recordingSurface,recordingContext;
let recordingExtent=null;
let studyPhase=0,visualFish=fish,navigationLeg=-1;
const ownedGeometries=new Set(),ownedMaterials=new Set(),ownedTextures=new Set(),joints=[],rest=[];
const frameTimes=[];
let seed=913;
const random=()=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed/4294967296;};
const ownGeometry=g=>{ownedGeometries.add(g);return g;},ownMaterial=m=>{ownedMaterials.add(m);return m;};
const ownTexture=t=>{ownedTextures.add(t);return t;};
let orbit=0,elevation=0,distance=4.1;
const target=new THREE.Vector3(0,.86,-.2);
const researchFocus=target.clone(),focusPoint=new THREE.Vector3();
const raycaster=new THREE.Raycaster(),pointer=new THREE.Vector2(),interactionPlane=new THREE.Plane(new THREE.Vector3(0,0,1),-.14),hit=new THREE.Vector3();
const deformable=[];
const fins=[];
const pairedFinControllers={
  pectoral:{'-1':createFinController(-1,true),'1':createFinController(1,true)},
  pelvic:{'-1':createFinController(-1,false),'1':createFinController(1,false)},
};
function observePairedFins(sample,key){
  for(const pair of Object.values(pairedFinControllers))for(const controller of Object.values(pair))controller.sample(sample,key);
}
const turnStudy=createTurnStudy(swimmer,{random:studyRandom,
  onReset:sample=>{turnPoseKey++;observePairedFins(sample,`turn:${turnPoseKey}`);},
  onTick:sample=>observePairedFins(sample,`turn:${turnPoseKey}`),
});
const barbels=[];
const finSupports=[];
const anatomicalMeshes=[];
const scaleMeshes=[],wireSettings=new Map();
let wireMaterial;
const particleData=[],particlePose=new THREE.Object3D();
let particles;
let finMap;

function finTexture(){
  const image=document.createElement('canvas');image.width=2048;image.height=512;
  const ctx=image.getContext('2d'),pixels=ctx.createImageData(image.width,image.height);
  for(let y=0;y<image.height;y++)for(let x=0;x<image.width;x++){
    const u=x/(image.width-1),s=1-y/(image.height-1),i=(y*image.width+x)*4;
    const ray=Math.pow(.5+.5*Math.cos(u*Math.PI*30),18);
    const branches=Math.pow(.5+.5*Math.cos((u+s*.004)*Math.PI*90),24)*Math.max(0,(s-.45)/.55);
    const tissue=ray*.22+branches*.14,grain=(random()-.5)*.05;
    pixels.data[i]=255*(.80+grain+tissue*.16-s*.08);
    pixels.data[i+1]=255*(.75+grain+tissue*.2+s*.02);
    pixels.data[i+2]=255*(.62+grain+tissue*.30+s*.12);
    pixels.data[i+3]=255*(.64-s*.33+tissue);
  }
  ctx.putImageData(pixels,0,0);
  const map=ownTexture(new THREE.CanvasTexture(image));map.colorSpace=THREE.SRGBColorSpace;map.flipY=false;
  return map;
}

function size(){
  const width=canvas.clientWidth,height=canvas.clientHeight;
  // Preserve native 5K while bounding sustained GPU work beyond large desktop displays.
  const nativeRatio=Math.min(devicePixelRatio,2,Math.sqrt(16777216/(width*height)));
  // The encoder receives 1080p. Avoid rendering millions of extra pixels only
  // to discard them during capture; restore native pixels when capture ends.
  const ratio=recordingExtent?Math.min(nativeRatio,recordingExtent.width/width,recordingExtent.height/height):nativeRatio;
  renderer.setPixelRatio(ratio);renderer.setSize(width,height,false);
  camera.aspect=width/height;
  camera.fov=state.closeup?10:width<650?64:THREE.MathUtils.radToDeg(2*Math.atan(18/(47*camera.aspect)));
  frameResearchCamera();
  $('pixels').textContent=`${canvas.width.toLocaleString()} × ${canvas.height.toLocaleString()}`;
  canvas.dataset.renderWidth=String(canvas.width);canvas.dataset.renderHeight=String(canvas.height);
}
function frameResearchCamera(){
  const width=canvas.clientWidth,height=canvas.clientHeight;
  if(state.research){
    // Reserve reading space with an off-axis camera, not fish translation or
    // a diagram-space offset. Reflections and extinction use this projection.
    const side=width>650?(width>1150?396:358)/2:0;
    camera.setViewOffset(width,height,side,width<=650?Math.min(100,height*.12):0,width,height);
  }else camera.clearViewOffset();
}
function setCamera(){
  if(state.closeup&&asset){
    const focus=asset.localToWorld(new THREE.Vector3(-.536,.004,.02));
    camera.position.set(focus.x-2.4+orbit*2,focus.y+.16+elevation,2.45);
    camera.lookAt(focus);camera.fov=10;camera.updateProjectionMatrix();camera.updateMatrixWorld();return;
  }
  camera.fov=canvas.clientWidth<650?64:THREE.MathUtils.radToDeg(2*Math.atan(18/(47*camera.aspect)));
  camera.updateProjectionMatrix();
  if(state.cameraAngle==='fish'&&asset){
    const focus=asset.localToWorld(new THREE.Vector3(-.15,.02,0));
    const orientation=asset.getWorldQuaternion(new THREE.Quaternion());
    camera.position.copy(focus).add(new THREE.Vector3(-distance*.7,.10+elevation,orbit*distance).applyQuaternion(orientation));
    camera.lookAt(focus);camera.updateMatrixWorld();return;
  }
  const bearing=orbit+(state.cameraAngle==='quarter'?-.55:0);
  const focus=state.research?researchFocus:target;
  // Give a full swimming turn room in the overhead view. Distance changes
  // elevation and range together, so zoom also works above the water.
  if(state.cameraAngle==='top')camera.position.set(focus.x+Math.sin(orbit)*distance*.55,focus.y+distance*1.08+elevation,focus.z+distance*.82);
  else camera.position.set(Math.sin(bearing)*distance,1.02+elevation,Math.cos(bearing)*distance);
  camera.lookAt(focus.x,state.research?focus.y:canvas.clientWidth<650?.20:focus.y,focus.z);camera.updateMatrixWorld();
}
function immerse(mat){
  const finTissue=mat.name.startsWith('Ivory fin membrane');
  const tissue=mat.name==='Pearl skin'||finTissue;
  const anatomical=mat.name.startsWith('Anatomy');
  const basic=anatomical||mat.isMeshBasicMaterial;
  const material=ownMaterial(basic?new THREE.MeshBasicNodeMaterial():tissue?new THREE.MeshSSSNodeMaterial():new THREE.MeshPhysicalNodeMaterial());
  // glTF supplies ordinary materials. NodeMaterial.copy also copies shader
  // hooks, which do not exist on those sources and must retain their defaults.
  if(basic){
    THREE.Material.prototype.copy.call(material,mat);
    material.color.copy(mat.color);material.map=mat.map??null;material.vertexColors=mat.vertexColors;
  }else{
    THREE.MeshStandardMaterial.prototype.copy.call(material,mat);
    if(mat.isMeshPhysicalMaterial)THREE.MeshPhysicalMaterial.prototype.copy.call(material,mat);
  }
  if(anatomical){
    material.color.setRGB(...(mat.name.includes('notochord')?[.9,.55,.20]:mat.name.includes('cartilage')?[.25,.60,.90]:[.28,.85,.70]));
    material.userData.anatomy=true;
  }
  if(finTissue){
    material.transmission=0;material.opacity=mat.name==='Ivory fin membrane'?.72:1;material.transparent=true;
    material.depthWrite=false;material.clearcoat=.02;material.side=THREE.DoubleSide;material.alphaTest=.01;
    material.color.setRGB(1,1,1);material.roughness=.38;material.ior=1.035;material.specularIntensity=.55;
    material.map=mat.map??(finMap??=finTexture());
    material.thicknessColorNode=vec3(.7,.48,.28);
    material.thicknessAttenuationNode=float(.05);material.thicknessScaleNode=float(.65);
    // Standard depth shadows treat thin tissue as opaque. Retain a soft,
    // tinted transmission approximation rather than black fin silhouettes.
    material.castShadowNode=vec4(.50,.56,.42,texture(material.map).a.mul(.18));
  }
  if(mat.name==='Fin rays'){
    material.transmission=0;material.transparent=true;material.opacity=1;material.depthWrite=false;
    material.opacityNode=rayDetail.mul(.32);
    material.ior=1.04;material.clearcoat=.02;material.roughness=.42;
    material.castShadowNode=vec4(.7,.72,.58,rayDetail.mul(.04));
  }
  if(mat.name==='Pearl skin'){
    material.normalNode=bumpMap(mx_noise_float(positionGeometry.mul(900)).mul(.00009),.65);
    material.roughnessNode=mx_noise_float(positionGeometry.mul(450)).mul(.055).add(.34);
    // The skin-water interface has far less Fresnel contrast than skin-air.
    material.ior=1.04;material.specularIntensity=.6;
    material.clearcoat=.08;material.clearcoatRoughness=.35;
    material.thicknessColorNode=texture(mat.map).rgb.mul(.22);
    material.thicknessAttenuationNode=float(.08);material.thicknessScaleNode=float(1.8);
  }
  if(mat.name==='Golden iris'&&!mat.map){
    material.colorNode=vec3(.26,.29,.19).mul(mx_noise_float(positionLocal.mul(2000)).mul(.45).add(.85));
    material.roughness=.32;material.metalness=0;
  }
  if(mat.name==='Corneal highlight'){
    material.color.setRGB(1,1,1);material.ior=1.035;material.transmission=1;
    material.thickness=.003;material.clearcoat=.02;material.roughness=.025;
    material.castShadowNode=vec4(1,1,1,.001);
  }
  // Clip the complete camera-to-fragment segment to the actual tank volume.
  // Front-pane distance is wrong when looking through the surface or side.
  const ray=positionWorld.sub(cameraPosition);
  const safeRay=max(abs(ray),vec3(.000001)).mul(step(vec3(0),ray).mul(2).sub(1));
  const a=vec3(...waterMin).sub(cameraPosition).div(safeRay);
  const b=vec3(...waterMax).sub(cameraPosition).div(safeRay);
  const entry=min(a,b),exit=max(a,b);
  const near=max(max(entry.x,entry.y),max(entry.z,0));
  const far=min(min(exit.x,exit.y),min(exit.z,1));
  const waterPath=max(far.sub(near),0).mul(ray.length());
  const transmission=exp(vec3(...waterExtinction).mul(waterPath).negate());
  const caustic=pow(sin(positionWorld.x.mul(12).add(sin(positionWorld.z.mul(9).add(clock)))).mul(.5).add(.5),12);
  material.outputNode=vec4(output.rgb.mul(transmission).mul(caustic.mul(.045).add(1)),output.a);
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
function habitat(stoneMaps){
  const ground=pebbleMaterial(),rock=immerse(new THREE.MeshPhysicalMaterial({color:0x253c40,roughness:.85,clearcoat:.04}));
  const projected=map=>triplanarTexture(texture(map),null,null,float(2));
  const arm=projected(stoneMaps.arm);
  rock.normalNode=bumpMap(projected(stoneMaps.height).r.mul(.007),.75);
  rock.colorNode=projected(stoneMaps.color).rgb;
  rock.roughnessNode=arm.g.mul(.55).add(.12);rock.aoNode=arm.r;
  rock.ior=1.12;
  mesh(new THREE.BoxGeometry(4,.09,tankDepth),ground,[0,-.025,tankCenterZ]).layers.enable(1);
  const pebbles=new THREE.InstancedMesh(ownGeometry(new THREE.IcosahedronGeometry(1,1)),ground,22000),dummy=new THREE.Object3D();
  for(let i=0;i<22000;i++){
    const radius=.007+random()*.015;dummy.position.set((random()-.5)*4,.0205+radius*.45,(random()-.5)*(tankDepth-.12)+tankCenterZ);
    dummy.scale.set(radius*(.8+random()*.5),radius*(.4+random()*.35),radius*(.7+random()*.5));
    dummy.rotation.set(random()*3,random()*3,random()*3);dummy.updateMatrix();pebbles.setMatrixAt(i,dummy.matrix);
    const shade=.30+random()*.45;pebbles.setColorAt(i,new THREE.Color(shade*.84,shade*.94,shade));
  }
  pebbles.receiveShadow=true;scene.add(pebbles);
  for(const [x,z,r] of [[-1.63,-.39,.25],[-1.27,-.64,.13],[1.64,-.68,.28],[1.23,-.78,.15]]){
    const raw=new THREE.IcosahedronGeometry(r,24);
    raw.deleteAttribute('normal');raw.deleteAttribute('uv');
    const geometry=mergeVertices(raw,1e-6);raw.dispose();
    const positions=geometry.attributes.position;
    // Coincident corners must move together; random offsets split triangles
    // and turn smooth stone into a pile of disconnected facets.
    for(let i=0;i<positions.count;i++){
      const px=positions.getX(i),py=positions.getY(i),pz=positions.getZ(i);
      const scale=1+.13*Math.sin(px*17+py*11)*Math.cos(pz*19)+.04*Math.sin(px*77-pz*39);
      positions.setXYZ(i,px*scale,py*scale,pz*scale);
    }
    geometry.computeVertexNormals();
    const stone=mesh(geometry,rock,[x,r*.4,z]);stone.scale.set(1.2,.73,.8);stone.castShadow=true;stone.layers.enable(1);
  }
  mesh(new THREE.PlaneGeometry(4,1.95),immerse(new THREE.MeshBasicMaterial({color:0x030f18})),[0,.9,waterMin[2]-.01]).layers.enable(1);
  waterInterface=createWaterInterface(scene,camera);
  const surface=ownMaterial(waterInterface.material);
  const water=mesh(new THREE.PlaneGeometry(4,tankDepth,64,48),surface,[0,waterMax[1],tankCenterZ]);water.rotation.x=-Math.PI/2;
  const points=water.geometry.attributes.position,base=Float32Array.from(points.array);deformable.push({points,base,geometry:water.geometry});
  // At this near-normal viewing angle a 12 mm parallel pane displaces the
  // image by millimetres. A faint reflective pane preserves that appearance
  // without a nested transmission pass that omits the translucent fin tissue.
  const glass=ownMaterial(new THREE.MeshPhysicalNodeMaterial({color:0x101c23,roughness:.035,metalness:0,ior:1.46,transparent:true,opacity:.035,depthWrite:false,clearcoat:0,side:THREE.DoubleSide}));
  const pane=mesh(new THREE.PlaneGeometry(4,1.9),glass,[0,.9,waterMax[2]]);pane.receiveShadow=false;
  const edge=ownMaterial(new THREE.MeshPhysicalNodeMaterial({color:0x446b74,roughness:.18,metalness:.25,transparent:true,opacity:.35}));
  for(const x of [-1.98,1.98])mesh(new THREE.BoxGeometry(.014,1.86,.025),edge,[x,.92,waterMax[2]-.01]);
  const particulate=ownMaterial(new THREE.MeshBasicNodeMaterial({color:0x57747d,transparent:true,opacity:.19}));
  particles=new THREE.InstancedMesh(ownGeometry(new THREE.IcosahedronGeometry(.0016,0)),particulate,180);
  for(let i=0;i<180;i++){
    const point={x:(random()-.5)*3.7,y:.1+random()*1.6,z:(random()-.5)*(tankDepth-.3)+tankCenterZ,scale:.5+random()};particleData.push(point);
    dummy.position.set(point.x,point.y,point.z);dummy.scale.setScalar(point.scale);dummy.updateMatrix();particles.setMatrixAt(i,dummy.matrix);
  }
  scene.add(particles);
  const ambient=new THREE.HemisphereLight(0xe0e5df,0x0b252e,.25);ambient.layers.enable(1);scene.add(ambient);
  const key=new THREE.RectAreaLight(0xfff0db,7,1.3,.10);
  key.position.set(.1,1.70,.4);key.lookAt(.1,.65,-.15);key.layers.enable(1);scene.add(key);
  for(const [position,targetPosition,power,color,angle] of [
    [[.1,1.70,.4],[.2,.4,-.1],6,0xfff0db,.85],
    [[-1.75,1.25,0],[.2,.7,-.1],3,0xaccad6,.8],
    [[1,1.60,-.8],[.2,.7,-.1],5,0x8fcfe6,.7],
  ]){
    const light=new THREE.SpotLight(color,power,8,angle,.7,2);light.position.set(...position);light.target.position.set(...targetPosition);
    light.castShadow=true;light.shadow.mapSize.set(1024,1024);light.shadow.normalBias=.001;light.shadow.bias=-.0002;light.layers.enable(1);
    scene.add(light,light.target);
  }
  const diffuser=ownMaterial(new THREE.MeshBasicNodeMaterial({color:new THREE.Color(.72,.67,.59),transparent:true,depthWrite:false}));
  const lampUV=uv().sub(.5).abs();
  diffuser.opacityNode=smoothstep(.5,.38,lampUV.x).mul(smoothstep(.5,.18,lampUV.y));
  const lamp=mesh(new THREE.PlaneGeometry(1.3,.10),diffuser,[.1,1.70,.4]);
  lamp.lookAt(.1,.65,-.15);lamp.layers.set(1);lamp.receiveShadow=false;
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
  }else if(!turnReplay)swimmer.follow(pointerPoint(event));
}
function pointerUp(event){if(drag&&event.pointerId===drag.id){if(!drag.moved&&!turnReplay)swimmer.flick(pointerPoint(event));drag=null;}}
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
  const held={...fish,x:.3,y:.77,z:-.15,yaw:-.40,yawRate:0,pitch:0,pitchRate:0,bank:0,
    drive:.2,speed:.2,forwardSpeed:.2,stroke:1,brake:0,maneuver:0,finSteer:0,finPhase:studyPhase*.55,phase:studyPhase,poseHistory:false};
  if(state.study){
    const axis=state.studyAxis;
    held.lateralGain=axis==='vertical'||axis==='twist'?0:1;
    held.verticalGain=axis==='lateral'||axis==='twist'?0:1;
    held.twistGain=axis==='lateral'||axis==='vertical'?0:1;
    if(axis==='vertical'||axis==='combined')held.pitchRate=.38*Math.sin(studyPhase*.32);
    if(axis==='twist'||axis==='combined')held.torsionDrive=.28*Math.sin(studyPhase*.27);
    if(axis==='fins')Object.assign(held,{lateralGain:0,verticalGain:0,twistGain:0,stroke:0,
      speed:.10,forwardSpeed:.10,brake:.75,finPhase:fish.age*Math.PI*2*(.55+.35*.75),muscle1:0,muscle2:0,muscle3:0});
  }
  const sample=state.study?held:fish;
  visualFish=sample;
  applySwimPose(asset,joints,rest,sample);
  facialRig?.pose(sample);
  const p=joints.at(-1).getWorldPosition(hit);
  canvas.dataset.tailPosition=[p.x,p.y,p.z].map(v=>v.toFixed(4)).join(',');
  canvas.dataset.phase=sample.phase.toFixed(4);
  canvas.dataset.age=fish.age.toFixed(2);canvas.dataset.pitch=sample.pitch.toFixed(3);
  canvas.dataset.studyTick=turnReplay?String(turnStudy.tick):'';
  canvas.dataset.rootPosition=[sample.x,sample.y,sample.z].map(v=>v.toFixed(3)).join(',');
  const behavior=swimmer.behavior;
  canvas.dataset.behavior=state.study?'held study':behavior.intent;
  canvas.dataset.gait=behavior.gait;canvas.dataset.speed=sample.speed.toFixed(3);
  canvas.dataset.stroke=sample.stroke.toFixed(3);canvas.dataset.yawRate=sample.yawRate.toFixed(3);
  canvas.dataset.wallContacts=String(behavior.contacts);
}
function draw(now){
  const motionStarted=performance.now();
  if(disposed)return;
  const elapsed=previous?(now-previous)/1000:0;previous=now;
  if(document.hidden||state.reference)return;
  if(!state.paused){
    if(state.studyAxis==='swim'&&!state.study){
      const leg=Math.floor(fish.age/12)%2;
      if(leg!==navigationLeg){swimmer.navigate({x:leg?.85:-.85,y:leg?.50:1.17,z:-.10});navigationLeg=leg;}
    }
    if(turnReplay)turnStudy.step(elapsed);else swimmer.step(elapsed);
    studyPhase+=Math.PI*2*1.19*Math.min(elapsed,.25);clock.value=fish.age;pose();}
  const finPoseKey=turnReplay?`turn:${turnPoseKey}`:state.study?state.studyAxis:'live';
  const pairedMotion={
    pectoral:{'-1':pairedFinControllers.pectoral[-1].sample(visualFish,finPoseKey),'1':pairedFinControllers.pectoral[1].sample(visualFish,finPoseKey)},
    pelvic:{'-1':pairedFinControllers.pelvic[-1].sample(visualFish,finPoseKey),'1':pairedFinControllers.pelvic[1].sample(visualFish,finPoseKey)},
  };
  for(const fin of fins){
    const {points,base,paired,pectoral,geometry,pivots}=fin;
    if(paired){
      // Tissue and the combined bilateral ray mesh use the same membrane
      // frames and Cartesian field, including distal cup and feathering lag.
      pivots.frames??=Object.fromEntries(fins.filter(item=>item.paired&&item.pectoral===pectoral&&item.name.includes('_membrane'))
        .map(item=>[item.sign,createFinFrame(item.base,item.geometry.attributes.uv.array)]));
      fin.binding??=bindFinGeometry(base,pivots.frames);
      deformFinGeometry(fin.binding,pairedMotion[pectoral?'pectoral':'pelvic'],points.array);
    }else for(let i=0;i<points.count;i++){
      const x=base[i*3],y=base[i*3+1],z=base[i*3+2];
      points.setXYZ(i,x,y,z+.035*(visualFish.stroke??1)*Math.sin(x*12-visualFish.phase-1.1)*Math.max(0,x-.48));
    }
    points.needsUpdate=true;
    geometry.computeVertexNormals();
    fin.reconcileNormals?.();
  }
  for(const {points,base} of barbels){
    const rootX=base[0],extent=Math.max(.001,Math.abs(base[(points.count-16)*3]-rootX));
    for(let i=0;i<points.count;i++){
      const x=base[i*3],y=base[i*3+1],z=base[i*3+2],s=THREE.MathUtils.clamp(Math.abs(x-rootX)/extent,0,1);
      // Anchored base, a tapering drag response and delayed tip compliance.
      points.setXYZ(i,x+.001*s*s*Math.sin(fish.age*2.1-s),y+.0018*s*s*Math.sin(fish.age*3.2-s*1.7),z+.0022*s*s*Math.sin(fish.age*2.7-s*2));
    }
    points.needsUpdate=true;
  }
  for(const {points,base,geometry} of deformable){
    for(let i=0;i<points.count;i++)points.setZ(i,base[i*3+2]+.0015*Math.sin(base[i*3]*5+fish.age*.8)*Math.cos(base[i*3+1]*6-fish.age*.3));
    points.needsUpdate=true;geometry?.computeVertexNormals();
  }
  if(!state.paused){
    const dt=Math.min(elapsed,.25);
    particleData.forEach((point,i)=>{
      const flow=waterFlow(point,fish.age);point.x+=flow.x*dt;point.z+=flow.z*dt;point.y-=.00025*dt;
      if(point.y<.04)point.y=1.7;
      particlePose.position.set(point.x,point.y,point.z);particlePose.scale.setScalar(point.scale);
      particlePose.updateMatrix();particles.setMatrixAt(i,particlePose.matrix);
    });particles.instanceMatrix.needsUpdate=true;
  }
  if(!state.study)for(let i=0;i<3;i++){
    if(!swimmer.contain(containment.correction()))break;pose();
  }
  if(state.research)researchFocus.lerp(asset.getWorldPosition(focusPoint),1-Math.exp(-4*Math.min(elapsed,.25)));
  if(state.closeup||state.cameraAngle==='fish'||state.research)setCamera();
  // The combined ray sets include 0.26 mm branches. Hand their contrast to
  // the filtered membrane map when unresolved; retain topology and full
  // inspection materials. This is authored detail LOD, not exact coverage AA.
  const cameraDepth=-asset.getWorldPosition(rayDetailPosition).applyMatrix4(camera.matrixWorldInverse).z;
  rayDetail.value=fullRayDetail?1:finRayContrast({renderHeight:canvas.height,projectionScaleY:camera.projectionMatrix.elements[5],cameraDepth});
  const renderStarted=performance.now();
  waterInterface.update(renderer);optics.render();
  research?.update(now);
  canvas.dataset.motionMs=(renderStarted-motionStarted).toFixed(1);
  canvas.dataset.submitMs=(performance.now()-renderStarted).toFixed(1);
  canvas.dataset.cameraPosition=camera.position.toArray().map(v=>v.toFixed(3)).join(',');
  canvas.dataset.cameraFov=camera.fov.toFixed(1);
  canvas.dataset.renderCalls=String(renderer.info.render.frameCalls);
  if(recordingContext&&recording?.state==='recording'){
    const scale=Math.min(recordingSurface.width/canvas.width,recordingSurface.height/canvas.height);
    const width=canvas.width*scale,height=canvas.height*scale;
    recordingContext.fillStyle='#030b12';recordingContext.fillRect(0,0,recordingSurface.width,recordingSurface.height);
    recordingContext.drawImage(canvas,(recordingSurface.width-width)/2,(recordingSurface.height-height)/2,width,height);
  }
  if(elapsed>0&&elapsed<.25){frameTimes.push(elapsed*1000);if(frameTimes.length>180)frameTimes.shift();}
  if(now-metrics>1000&&frameTimes.length>20){
    const average=frameTimes.reduce((a,b)=>a+b,0)/frameTimes.length;
    $('performance').textContent=`${Math.round(1000/average)} fps / ${average.toFixed(1)} ms`;
    canvas.dataset.fps=String(Math.round(1000/average));metrics=now;
  }
}
function setPause(paused){state.paused=paused;$('motion').textContent=paused?'Resume motion':'Pause motion';$('motion').setAttribute('aria-pressed',String(paused));}
function restartTurnStudy(){
  turnReplay=true;state.study=false;state.closeup=false;studyPhase=0;navigationLeg=-1;
  turnStudy.reset();previous=0;clock.value=fish.age;frameTimes.length=0;
  $('study').setAttribute('aria-pressed','false');$('closeup').setAttribute('aria-pressed','false');
  $('hint').textContent='The same swimming turn replays on restart and recording. Drag to inspect it from another angle.';
  pose();if(state.research)asset.getWorldPosition(researchFocus);setCamera();setPause(false);
}
function setView(reference){
  state.reference=reference;$('reference').hidden=!reference;$('live-view').setAttribute('aria-pressed',String(!reference));$('reference-view').setAttribute('aria-pressed',String(reference));
  research?.setReference(reference);
  $('motion').disabled=$('reset').disabled=$('wireframe').disabled=$('light').disabled=$('study').disabled=$('study-axis').disabled=$('closeup').disabled=$('camera-angle').disabled=$('record-length').disabled=reference;
  $('record').disabled=reference||recording?.state==='recording';
  $('hint').textContent=reference?'Cycles reference: the same authored fish, rendered offline through glass and volumetric water.':turnReplay?'The same swimming turn replays on restart and recording. Drag to inspect it from another angle.':'Move to invite the koi closer. Tap to startle or interest it. Drag to look around.';
  $('engine').textContent=reference?'Cycles / offline':liveEngine;
  $('performance').textContent=reference&&referenceMetadata?`${referenceMetadata.samples} samples / ${referenceMetadata.resolution.join(' × ')} still`:reference?'Offline still':'Measuring';
  previous=0;frameTimes.length=0;
}
function cleanup(){
  disposed=true;renderer?.setAnimationLoop(null);window.removeEventListener('resize',size);
  research?.dispose();
  if(recording?.state==='recording')recording.stop();releaseCapture();if(recordingUrl)URL.revokeObjectURL(recordingUrl);
  canvas.removeEventListener('pointerdown',pointerDown);canvas.removeEventListener('pointermove',pointerMove);canvas.removeEventListener('pointerup',pointerUp);canvas.removeEventListener('pointercancel',pointerCancel);canvas.removeEventListener('pointerleave',pointerLeave);canvas.removeEventListener('wheel',wheel);canvas.removeEventListener('keydown',key);
  optics?.dispose();waterInterface?.dispose();ownedGeometries.forEach(g=>g.dispose());ownedMaterials.forEach(m=>m.dispose());ownedTextures.forEach(t=>t.dispose());environment?.dispose();renderer?.dispose();
}

function releaseCapture(){
  clearTimeout(recordingTimer);recordingStream?.getTracks().forEach(track=>track.stop());recordingStream=null;
  recordingContext=recordingSurface=recordingExtent=null;
  frameTimes.length=0;
  if(!disposed)size();
}

function record(){
  if(state.reference||recording?.state==='recording')return;
  const button=$('record'),link=$('download-recording'),seconds=$('record-length').value==='30'?30:8;
  try{
    if(!canvas.captureStream||!window.MediaRecorder)throw new Error('Canvas recording unavailable');
    const mimeType=['video/webm;codecs=vp8','video/webm;codecs=vp9','video/webm'].find(type=>MediaRecorder.isTypeSupported(type));
    if(!mimeType)throw new Error('No supported video codec');
    if(turnReplay){restartTurnStudy();draw(performance.now());}
    // Retain a fixed capture extent if the window changes size mid-recording.
    const exportRatio=Math.min(1,1920/canvas.width,1080/canvas.height);
    recordingSurface=document.createElement('canvas');recordingSurface.width=Math.round(canvas.width*exportRatio);
    recordingSurface.height=Math.round(canvas.height*exportRatio);
    recordingContext=recordingSurface.getContext('2d',{alpha:false});
    recordingContext.drawImage(canvas,0,0,recordingSurface.width,recordingSurface.height);
    recordingExtent={width:recordingSurface.width,height:recordingSurface.height};size();
    recordingStream=recordingSurface.captureStream(30);
    recording=new MediaRecorder(recordingStream,{mimeType,videoBitsPerSecond:15000000});
    let captureFailed=false;const chunks=[];
    recording.ondataavailable=event=>{if(event.data.size)chunks.push(event.data);};
    recording.onstop=async()=>{
      releaseCapture();if(disposed)return;
      if(captureFailed){button.disabled=state.reference;return;}
      if(recordingUrl)URL.revokeObjectURL(recordingUrl);
      const blob=new Blob(chunks,{type:mimeType});
      recordingUrl=URL.createObjectURL(blob);link.href=recordingUrl;link.hidden=false;
      try{
        const response=await fetch('__recording',{method:'POST',headers:{'Content-Type':mimeType},body:blob});
        if(response.ok){const saved=await response.json();link.href=saved.url;link.removeAttribute('download');link.textContent='Review recording';}
      }catch{/* Ordinary static servers retain the browser-download fallback. */}
      button.disabled=state.reference;button.textContent=`Record ${$('record-length').value} seconds (1080p)`;
    };
    recording.onerror=event=>{captureFailed=true;releaseCapture();button.textContent=`Recording failed: ${event.error?.message??'encoder error'}`;button.disabled=true;};
    link.hidden=true;button.disabled=true;button.textContent='Recording live frames';frameTimes.length=0;recording.start();
    if(turnReplay)previous=performance.now();
    recordingTimer=setTimeout(()=>{if(recording.state==='recording')recording.stop();},seconds*1000);
  }catch(error){releaseCapture();button.textContent=error.message;button.disabled=true;}
}

async function start(){
  const forceWebGL=new URLSearchParams(location.search).get('renderer')==='webgl';
  renderer=new THREE.WebGPURenderer({canvas,antialias:true,alpha:false,powerPreference:'high-performance',forceWebGL});
  await renderer.init();
  RectAreaLightTexturesLib.init();THREE.RectAreaLightNode.setLTC(RectAreaLightTexturesLib);
  renderer.outputColorSpace=THREE.SRGBColorSpace;
  renderer.toneMapping=new URLSearchParams(location.search).get('tone')==='agx'?THREE.AgXToneMapping:THREE.NeutralToneMapping;
  renderer.toneMappingExposure=1;
  renderer.shadowMap.enabled=true;renderer.shadowMap.type=THREE.PCFSoftShadowMap;
  scene=new THREE.Scene();scene.background=new THREE.Color(0x030b12);
  camera=new THREE.PerspectiveCamera(26,1,.06,25);setCamera();size();softboxEnvironment();
  const mapLoader=new THREE.TextureLoader();
  const mapFiles={color:'stone-color.jpg',height:'stone-height.jpg',arm:'stone-arm.jpg'},stoneMaps={};
  await Promise.all(Object.entries(mapFiles).map(async([key,file])=>{
    const map=ownTexture(await mapLoader.loadAsync(`aquarium/${file}`));
    map.wrapS=map.wrapT=THREE.RepeatWrapping;map.anisotropy=8;
    if(key==='color')map.colorSpace=THREE.SRGBColorSpace;stoneMaps[key]=map;
  }));
  habitat(stoneMaps);
  const gltf=await new GLTFLoader().loadAsync('aquarium/koi.glb?v=74');asset=gltf.scene;scene.add(asset);
  const converted=new Map();
  asset.traverse(object=>{
    if(object.isBone&&object.name.startsWith('spine_'))joints.push(object);
    if(object.isMesh){
      // The reflection needs the moving silhouette and tissue,
      // while overlapping scale relief stays in the native-resolution view.
      if(!object.name.startsWith('Overlapping_scales')&&!object.name.startsWith('Anatomy'))object.layers.enable(1);
      if(object.name.startsWith('Overlapping_scales'))scaleMeshes.push(object);
      if(object.name.startsWith('Anatomy')){anatomicalMeshes.push(object);object.visible=false;}
      if(object.name.includes('branched_rays')){finSupports.push(object);object.visible=true;}
      ownedGeometries.add(object.geometry);object.castShadow=object.receiveShadow=true;object.frustumCulled=false;
      if(/Caudal|Pectoral|Pelvic/.test(object.name)){
        const points=object.geometry.attributes.position,base=Float32Array.from(points.array);
        const paired=!object.name.startsWith('Caudal');
        let z=0;for(let i=0;i<points.count;i++)z+=points.getZ(i);
        const sign=Math.sign(z)||1;
        fins.push({name:object.name,points,base,paired,pectoral:object.name.startsWith('Pectoral'),sign,geometry:object.geometry,
          reconcileNormals:object.name.includes('branched_rays')?createFinNormalSmoother(object.geometry):null,
          root:{x:points.getX(0),y:points.getY(0),z:points.getZ(0)}});
      }
      if(object.name.includes('sensory_barbel')){const points=object.geometry.attributes.position;barbels.push({points,base:Float32Array.from(points.array)});}
      const replace=mat=>{if(!converted.has(mat)){converted.set(mat,immerse(mat));for(const value of Object.values(mat))if(value?.isTexture)ownedTextures.add(value);mat.dispose();}return converted.get(mat);};
      object.material=Array.isArray(object.material)?object.material.map(replace):replace(object.material);
    }
  });
  // Rays from both paired fins share an exported draw call. Each side must
  // use its membrane's basal pivot and paddle phase, not the first ray's tip.
  const pivots={pectoral:{},pelvic:{}};
  fins.filter(fin=>fin.paired&&fin.name.includes('_membrane')).forEach(fin=>{pivots[fin.pectoral?'pectoral':'pelvic'][fin.sign]=fin.root;});
  fins.filter(fin=>fin.paired).forEach(fin=>{fin.pivots=pivots[fin.pectoral?'pectoral':'pelvic'];});
  joints.sort((a,b)=>a.name.localeCompare(b.name));joints.forEach(b=>rest.push(b.quaternion.clone()));pose();facialRig=createFacialRig(asset);pose();
  containment=createContainment(asset);
  skeleton=new THREE.SkeletonHelper(asset);
  skeleton.material.dispose();skeleton.material=ownMaterial(new THREE.LineBasicNodeMaterial({color:0x80ffdc,transparent:true,opacity:.85,depthTest:false}));
  ownedGeometries.add(skeleton.geometry);skeleton.visible=false;skeleton.renderOrder=10;scene.add(skeleton);
  liveEngine=renderer.backend.isWebGPUBackend?'WebGPU / local GPU':'WebGL 2 fallback';
  $('engine').textContent=liveEngine;
  canvas.dataset.engine=renderer.backend.isWebGPUBackend?'webgpu':'webgl2';canvas.dataset.joints=String(joints.length);canvas.dataset.facialJoints=String(facialRig.jointCount);
  canvas.dataset.optics='single-scattering-camera-volume';canvas.dataset.rig='three-axis-arched-centra';
  canvas.dataset.collisionProbes=String(containment.probeCount);canvas.dataset.anatomyMeshes=String(anatomicalMeshes.length);
  optics=waterOptics(renderer,scene,camera);
  await renderer.compileAsync(scene,camera);await optics.prepare();loading.hidden=true;
  canvas.dataset.ready='true';
  fetch('aquarium/manifest.json?v=74').then(r=>{if(!r.ok)throw new Error(`HTTP ${r.status}`);return r.json();}).then(manifest=>{referenceMetadata=manifest.reference;$('reference-view').disabled=manifest.reference_matches_asset===false;}).catch(error=>console.warn('Reference metadata unavailable:',error));
  setPause(state.paused);
  canvas.addEventListener('pointerdown',pointerDown);canvas.addEventListener('pointermove',pointerMove);canvas.addEventListener('pointerup',pointerUp);canvas.addEventListener('pointercancel',pointerCancel);canvas.addEventListener('pointerleave',pointerLeave);canvas.addEventListener('wheel',wheel,{passive:false});canvas.addEventListener('keydown',key);
  window.addEventListener('resize',size);window.addEventListener('pagehide',cleanup,{once:true});
  $('motion').addEventListener('click',()=>setPause(!state.paused));
  $('reset').addEventListener('click',()=>{if(turnReplay){restartTurnStudy();return;}swimmer.reset();state.study=state.closeup=false;studyPhase=0;state.studyAxis='combined';$('study-axis').value='combined';$('hint').textContent='Move to invite the koi closer. Tap to startle or interest it. Drag to look around.';navigationLeg=-1;$('study').setAttribute('aria-pressed','false');$('closeup').setAttribute('aria-pressed','false');orbit=elevation=0;distance=4.1;setCamera();pose();setPause(false);});
  $('wireframe').addEventListener('click',()=>{
    state.wireframe=!state.wireframe;skeleton.visible=state.wireframe;
    [...finSupports,...anatomicalMeshes].forEach(mesh=>{mesh.visible=state.wireframe;});scaleMeshes.forEach(mesh=>{mesh.visible=!state.wireframe;});
    $('wireframe').setAttribute('aria-pressed',String(state.wireframe));
    wireMaterial??=ownMaterial(new THREE.MeshBasicNodeMaterial({color:0x32d4cf,wireframe:true,transparent:true,opacity:.022,depthWrite:false,side:THREE.DoubleSide}));
    asset.traverse(mesh=>{
      if(!mesh.isMesh||mesh.name.startsWith('Anatomy'))return;
      if(state.wireframe){
        wireSettings.set(mesh,{material:mesh.material,visible:mesh.visible});
        mesh.visible=/^(Koi_skin|Caudal|Dorsal|Pectoral|Pelvic|Anal)/.test(mesh.name);
        mesh.material=wireMaterial;
      }else{
        const saved=wireSettings.get(mesh);
        if(saved){mesh.material=saved.material;mesh.visible=saved.visible;}
      }
    });
    scaleMeshes.forEach(mesh=>{mesh.visible=!state.wireframe;});
    finSupports.forEach(mesh=>{mesh.visible=true;});
  });
  $('study').addEventListener('click',()=>{if(state.studyAxis==='swim'||state.studyAxis==='turn'){state.studyAxis='combined';$('study-axis').value='combined';}turnReplay=false;state.study=!state.study;studyPhase=fish.phase;$('study').setAttribute('aria-pressed',String(state.study));$('hint').textContent=state.study?'Held position reveals the spine. Choose an axis to isolate it; vertical flexion and twist are amplified for inspection.':'Move to invite the koi closer. Tap to startle or interest it. Drag to look around.';pose();});
  $('study-axis').addEventListener('change',()=>{
    state.studyAxis=$('study-axis').value;turnReplay=state.studyAxis==='turn';
    if(turnReplay){state.cameraAngle='quarter';$('camera-angle').value='quarter';orbit=elevation=0;distance=4.1;restartTurnStudy();return;}
    state.study=state.studyAxis!=='swim';
    if(!state.study){swimmer.reset();Object.assign(fish,{x:.65,y:.58,z:.12,yaw:0});navigationLeg=-1;}
    $('hint').textContent=state.studyAxis==='fins'?'Held fin-control study: inspect cupping, feathered recovery and independent sides. Timing is authored for inspection.':state.study?'Held orientation: vertical flexion and twist are amplified for inspection. Change camera angle to inspect in 3D.':'Ascent and descent come from nose-up and nose-down propulsion through the water.';
    $('study').setAttribute('aria-pressed',String(state.study));pose();setPause(false);
  });
  $('camera-angle').addEventListener('change',()=>{state.cameraAngle=$('camera-angle').value;state.closeup=false;$('closeup').setAttribute('aria-pressed','false');setCamera();});
  $('closeup').addEventListener('click',()=>{state.closeup=!state.closeup;if(state.closeup){if(state.studyAxis==='swim'||state.studyAxis==='turn'){state.studyAxis='combined';$('study-axis').value='combined';}turnReplay=false;state.study=true;$('study').setAttribute('aria-pressed','true');pose();}$('closeup').setAttribute('aria-pressed',String(state.closeup));setCamera();});
  $('record').addEventListener('click',record);
  $('record-length').addEventListener('change',()=>{if(recording?.state!=='recording')$('record').textContent=`Record ${$('record-length').value} seconds (1080p)`;});
  $('reference-view').addEventListener('click',()=>setView(true));$('live-view').addEventListener('click',()=>setView(false));
  $('light').addEventListener('input',()=>{renderer.toneMappingExposure=Number($('light').value);$('light-value').value=`${Math.round(Number($('light').value)*100)}%`;});
  research=createResearchLayer({root:$('tank'),canvas,toggle:$('research-toggle'),storyButton:$('build-story'),asset,joints,camera,
    getState:()=>state,getFish:()=>visualFish,onAction:action=>$(action==='pause'?'motion':'wireframe').click(),
    onVisibility:open=>{state.research=open;if(open)asset.getWorldPosition(researchFocus);frameResearchCamera();setCamera();}});
  await renderer.setAnimationLoop(draw);
}
start().catch(error=>{console.error('Aquarium benchmark:',error);loading.textContent=`Aquarium unavailable: ${error.message}`;canvas.dataset.ready='error';cleanup();});
window.addEventListener('pageshow',event=>{if(event.persisted)location.reload();});
