import * as THREE from './vendor/three.webgpu.js';
import {GLTFLoader} from './vendor/GLTFLoader.js';
import {createKoiInstance} from './aquarium-fish-instance.js?v=91';
import {createAxialFrame} from './aquarium-axial-frame.js?v=91';
import {headAngles} from './aquarium-attitude.js?v=91';
import {createRapierRenderBridge} from './aquarium-articulated-render.js?v=91';
import {createReferenceAnimationClip} from './aquarium-reference-motion.js?v=91';

// The bridge only copies P/Q to the real rig and refreshes visual tissue here.
// This page never instantiates Rapier and never claims a physical solution.
const $=id=>document.getElementById(id),degrees=180/Math.PI;
const state={time:0,playing:false,speed:.25,camera:'side',anatomy:false,labels:true,follow:true,last:0};
const restingVisual=Object.fromEntries(['phase','finPhase','drive','stroke','muscle1','muscle2','muscle3','speed','forwardSpeed',
  'brake','finSteer','pitchFin','rollFin','turnAnterior','turnTrunk','turnPosterior','turnRecruitment1','turnRecruitment2',
  'turnRecruitment3','torsionDrive',...['yawLag','pitchLag','bankLag'].flatMap(prefix=>[1,2,3].map(i=>prefix+i)),
  ...['pectoral','pelvic'].flatMap(family=>['Paddle','ForeAft'].flatMap(channel=>['Plus','Minus'].map(side=>family+channel+side)))]
  .map(key=>[key,0]));
const originals=new Map(),currentCenter=new THREE.Vector3(),initialCenter=new THREE.Vector3();
let renderer,scene,instance,sampler,bridge,clip,packet,end=0;
const views=['left-pane','right-pane'].map(id=>({pane:$(id),camera:new THREE.PerspectiveCamera(35,1,.02,30),markers:[]}));
const svgElement=name=>document.createElementNS('http://www.w3.org/2000/svg',name);
for(const view of views){
  const svg=view.pane.querySelector('svg');
  for(const [bone,label,anchor] of [[0,'HEAD',[.07,.62]],[16,'TRUNK',[.48,.25]],[33,'TAIL',[.79,.63]]]){
    const group=svgElement('g'),line=svgElement('path'),dot=svgElement('circle'),text=svgElement('text');
    text.textContent=label;dot.setAttribute('r','3');group.append(line,dot,text);svg.append(group);
    view.markers.push({bone,anchor,group,line,dot,text});
  }
}
function diagram(view,width,height){
  for(const marker of view.markers){
    const position=instance.joints[marker.bone].getWorldPosition(new THREE.Vector3()).project(view.camera);
    const visible=state.labels&&Math.abs(position.x)<1&&Math.abs(position.y)<1&&Math.abs(position.z)<1;
    marker.group.style.display=visible?'':'none';if(!visible)continue;
    const x=(position.x+1)*width/2,y=(1-position.y)*height/2,lx=marker.anchor[0]*width,ly=marker.anchor[1]*height;
    marker.line.setAttribute('d',`M ${lx} ${ly+5} L ${lx+22} ${ly+5} L ${x} ${y}`);
    marker.dot.setAttribute('cx',x);marker.dot.setAttribute('cy',y);marker.text.setAttribute('x',lx);marker.text.setAttribute('y',ly);
  }
}
function render(){
  if(!renderer||!clip)return;
  const field=$('field').getBoundingClientRect(),center=state.follow?currentCenter:initialCenter;
  const modes=state.camera==='side-quarter'?['side','quarter']:state.camera==='quarter'?['quarter','above']:['side','above'];
  const names={side:'Side view',quarter:'Quarter view',above:'Above'};
  $('left-title').textContent=names[modes[0]];$('right-title').textContent=names[modes[1]];
  renderer.setScissorTest(true);
  for(let i=0;i<views.length;i++){
    const view=views[i],{camera,pane}=view,r=pane.getBoundingClientRect(),mode=modes[i];
    camera.aspect=r.width/r.height;camera.up.set(0,1,0);
    const offset=mode==='above'?new THREE.Vector3(0,3.5,.001):mode==='side'?new THREE.Vector3(0,.12,3.5):new THREE.Vector3(-1.9,1.5,2.2);
    camera.position.copy(center).add(offset);if(mode==='above')camera.up.set(0,0,-1);
    camera.lookAt(center);camera.updateProjectionMatrix();camera.updateMatrixWorld();
    // WebGL viewport coordinates start at the bottom, including stacked mobile panes.
    const bottom=field.bottom-r.bottom;
    renderer.setViewport(r.left-field.left,bottom,r.width,r.height);renderer.setScissor(r.left-field.left,bottom,r.width,r.height);
    renderer.render(scene,camera);diagram(view,r.width,r.height);
  }
  renderer.setScissorTest(false);
}
const phaseCopy={
  'held-before':['Authored initial rest','The prescribed root and neutral body are held before the entry blend.'],
  'root-entry':['Authored entry','A quintic C2 root blend joins rest to the first retained head and COM state. This portion is not observed footage.'],
  'shape-pre-margin':['Observed root / authored body entry','The head and modeled-COM path follow retained source observations. The body cue is closing from its authored neutral boundary to the marked turn.'],
  'none':['Marked observed turn / fitted body','Head and modeled-COM channels interpolate the retained observations. The body is a three-control fit retargeted to the koi, not a measured koi pose.'],
  'shape-post-margin':['Observed post-turn root / authored body recovery','The source root continues beyond the marked turn, while the fitted body cue is authored to return to neutral.'],
  'root-exit':['Authored deceleration','A quintic C2 root blend continues the retained endpoint rates and settles to rest. Its final heading is authored.'],
  'held-after':['Authored final rest','The animation holds its settled root and neutral body. The final heading is not an additional source observation.'],
};
function show(time){
  if(!clip)return;
  packet=clip.sample(time,packet);currentCenter.copy(packet.rootReference.comWorld).multiplyScalar(instance.asset.scale.x);
  const angles=headAngles(packet.headQuaternion);
  bridge.pose(packet,{age:time,visualFish:{...restingVisual,...angles,age:time,gait:1,pectoral:0},
    headAngularVelocity:packet.rootReference.omegaWorld,headLinearVelocity:packet.rootReference.originVelocityWorld});
  const head=packet.rotations[0].clone().multiply(sampler.rest[0].clone().invert());
  const tail=packet.rotations[33].clone().multiply(sampler.rest[33].clone().invert());
  let largest=0;
  for(let i=5;i<34;i++){
    const current=packet.rotations[i].clone().multiply(sampler.rest[i].clone().invert());
    const previous=packet.rotations[i-1].clone().multiply(sampler.rest[i-1].clone().invert());largest=Math.max(largest,current.angleTo(previous));
  }
  $('bend').textContent=(head.angleTo(tail)*degrees).toFixed(1)+'°';$('joint').textContent=(largest*degrees).toFixed(1)+'°';
  $('yaw').textContent=(2*Math.atan2(packet.headQuaternion.y,packet.headQuaternion.w)*degrees).toFixed(1)+'°';
  $('source-time').textContent=packet.source.sourceTime.toFixed(3)+' s';
  $('join').textContent=(bridge.measure().maximumPositionError*1e6).toFixed(3)+' µm';
  $('time').textContent=time.toFixed(3)+' / '+end.toFixed(3)+' s';$('timeline').value=time;
  const [title,note]=phaseCopy[packet.source.authoredBoundaryBlend];$('phase').textContent=title;$('phase-note').textContent=note;
  $('field').dataset.time=time;$('field').dataset.phase=packet.source.authoredBoundaryBlend;render();
}
function resize(){if(!renderer)return;const r=$('field').getBoundingClientRect();renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));renderer.setSize(r.width,r.height,false);render();}
function anatomy(){
  if(!instance)return;instance.setInspection(state.anatomy);
  instance.asset.traverse(mesh=>{
    if(!mesh.isMesh||mesh.name.startsWith('Anatomy')||mesh.name.startsWith('Overlapping_scales'))return;
    for(const material of Array.isArray(mesh.material)?mesh.material:[mesh.material]){
      const original=originals.get(material);if(!original)continue;
      material.transparent=state.anatomy||original.transparent;
      material.opacity=state.anatomy?(/^(Pectoral|Pelvic|Dorsal|Anal|Caudal)_/.test(mesh.name)?.35:.1):original.opacity;
      material.depthWrite=state.anatomy?false:original.depthWrite;material.needsUpdate=true;
    }
  });render();
}
function animate(now){
  const dt=state.last?Math.min((now-state.last)/1000,.1):0;state.last=now;
  if(state.playing&&clip){state.time=Math.min(end,state.time+dt*state.speed);show(state.time);if(state.time>=end){state.playing=false;$('play').textContent='Play';}}
  requestAnimationFrame(animate);
}
$('play').onclick=()=>{if(!clip)return;if(state.time>=end){state.time=0;show(0);}state.playing=!state.playing;$('play').textContent=state.playing?'Pause':'Play';};
$('reset').onclick=()=>{state.playing=false;state.time=0;$('play').textContent='Play';show(0);};
$('camera').onchange=e=>{state.camera=e.target.value;render();};$('speed').onchange=e=>state.speed=Number(e.target.value);
$('anatomy').onchange=e=>{state.anatomy=e.target.checked;anatomy();};$('labels').onchange=e=>{state.labels=e.target.checked;render();};
$('follow').onchange=e=>{state.follow=e.target.checked;render();};
$('timeline').oninput=e=>{state.playing=false;$('play').textContent='Play';state.time=Number(e.target.value);show(state.time);};
window.addEventListener('resize',resize);document.addEventListener('visibilitychange',()=>state.last=0);
async function json(url){const response=await fetch(url);if(!response.ok)throw new Error(`${url}: HTTP ${response.status}`);return response.json();}
try{
  renderer=new THREE.WebGPURenderer({canvas:$('canvas'),antialias:true,forceWebGL:true});await renderer.init();
  renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.NeutralToneMapping;
  const [gltf,data,profile]=await Promise.all([new GLTFLoader().loadAsync('./aquarium/koi.glb'),json('./aquarium/reference-turn.json'),json('./aquarium/body-mass.json')]);
  sampler=createAxialFrame(gltf.scene);clip=createReferenceAnimationClip({data,sampler,profile});packet=clip.createPacket();end=clip.metadata.end+.3;
  scene=new THREE.Scene();scene.background=new THREE.Color(0x061019);scene.add(new THREE.HemisphereLight(0xdce8ea,0x10212a,1.35));
  for(const [color,power,position] of [[0xfff0db,3.7,[-1,3,2]],[0x88bac8,1.6,[1,1,-2]]]){const light=new THREE.DirectionalLight(color,power);light.position.set(...position);scene.add(light);}
  instance=createKoiInstance(gltf.scene,{random:()=>.5,materialFor:material=>{const cloned=material.clone();originals.set(cloned,{opacity:cloned.opacity,transparent:cloned.transparent,depthWrite:cloned.depthWrite});cloned.side=THREE.DoubleSide;return cloned;}});
  scene.add(instance.asset);bridge=createRapierRenderBridge(instance,{sampler});
  const grid=new THREE.GridHelper(4,40,0x31505c,0x16303b);grid.position.set(0,-.32,0);grid.material.transparent=true;grid.material.opacity=.42;scene.add(grid);
  initialCenter.copy(clip.sample(0).rootReference.comWorld).multiplyScalar(instance.asset.scale.x);
  $('marked-heading').textContent=clip.metadata.markedHeadingDegrees.toFixed(2)+'°';$('post-heading').textContent=clip.metadata.sourcePostHeadingDegrees.toFixed(2)+'°';$('exit-heading').textContent=clip.metadata.authoredFinalHeadingDegrees.toFixed(2)+'°';
  $('timeline').max=end;for(const id of ['play','reset','timeline'])$(id).disabled=false;
  $('status').textContent='BG63_14_8 · bluegill observations → koi geometry · full-fin target length 1.496383 m · prescribed animation, no force simulation';
  show(0);$('loading').hidden=true;resize();requestAnimationFrame(animate);
}catch(error){console.error(error);$('loading').textContent='Reference animation unavailable: '+error.message;}
