import * as THREE from './vendor/three.webgpu.js';
import {GLTFLoader} from './vendor/GLTFLoader.js';
import {createKoiInstance} from './aquarium-fish-instance.js?v=91';
import {createAxialFrame} from './aquarium-axial-frame.js?v=91';
import {neutralMechanicsFish} from './aquarium-mechanics-study.js?v=91';
import {headAngles} from './aquarium-attitude.js?v=91';
import {createRapierRenderBridge} from './aquarium-articulated-render.js?v=91';
import {loadWaterStudy} from './aquarium-water-study-data.js?v=91';

const $=id=>document.getElementById(id),degrees=180/Math.PI;
const state={time:0,playing:false,speed:.25,camera:'quarter',anatomy:false,labels:true,follow:true,last:0};
const caseNotes=[
  ['The original tail hinge','The separated water and motor steps concentrate the curve in a distal joint. Watch the tail fold while the front remains comparatively straight. This failed attempt is retained as the control.'],
  ['Spread the bend through the body','A shared active-motor and resistance solve, geometric lateral capacity and authored joint ranges distribute the C bend. Watch the front and rear rotate separately. The body reaches a deep curve, then largely returns to its original heading.'],
  ['Compare at the reference fish’s size','This 13 cm proxy uses the measured bluegill body-shape sequence and starting velocity. Head yaw increases, but the animal still fails to complete the observed reversal and develops unwanted pitch and roll. Its head orientation and centre of mass are free outcomes.'],
];
const originals=new Map(),currentCenter=new THREE.Vector3();
let renderer,scene,instance,sampler,bridge,runs,run,manifest,shown=-1,selection=0;
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
    const visible=state.labels&&position&&Math.abs(position.x)<1&&Math.abs(position.y)<1&&Math.abs(position.z)<1;
    marker.group.style.display=visible?'':'none';if(!visible)continue;
    const x=(position.x+1)*width/2,y=(1-position.y)*height/2,lx=marker.anchor[0]*width,ly=marker.anchor[1]*height;
    marker.line.setAttribute('d',`M ${lx} ${ly+5} L ${lx+22} ${ly+5} L ${x} ${y}`);
    marker.dot.setAttribute('cx',x);marker.dot.setAttribute('cy',y);marker.text.setAttribute('x',lx);marker.text.setAttribute('y',ly);
  }
}

function render(){
  if(!renderer||!run)return;
  const field=$('field').getBoundingClientRect(),first=run.frames[0];
  const center=state.follow?currentCenter:new THREE.Vector3(...(first.renderCOM??first.com));
  const modes=state.camera==='side-quarter'?['side','quarter']:state.camera==='side'?['side','above']:['quarter','above'];
  const names={side:'Side view',quarter:'Quarter view',above:'Above'};
  $('left-title').textContent=names[modes[0]];$('right-title').textContent=names[modes[1]];
  renderer.setScissorTest(true);
  for(let i=0;i<views.length;i++){
    const view=views[i],{camera,pane}=view,r=pane.getBoundingClientRect(),mode=modes[i];
    camera.aspect=r.width/r.height;camera.up.set(0,1,0);
    const offset=mode==='above'?new THREE.Vector3(0,3.5,.001):mode==='side'?new THREE.Vector3(0,.12,3.5):new THREE.Vector3(-1.9,1.5,2.2);
    camera.position.copy(center).add(offset);if(mode==='above')camera.up.set(0,0,-1);
    camera.lookAt(center);camera.updateProjectionMatrix();camera.updateMatrixWorld();
    renderer.setViewport(r.left-field.left,r.top-field.top,r.width,r.height);
    renderer.setScissor(r.left-field.left,r.top-field.top,r.width,r.height);renderer.render(scene,camera);
    diagram(view,r.width,r.height);
  }
  renderer.setScissorTest(false);
}

function show(time){
  if(!run)return;
  let low=0,high=run.frames.length-1;
  while(high-low>1){const middle=(low+high)>>1;if(run.frames[middle].time<=time)low=middle;else high=middle;}
  const index=Math.abs(run.frames[low].time-time)<=Math.abs(run.frames[high].time-time)?low:high;
  if(index===shown)return;shown=index;const frame=run.frames[index];
  const transforms={positions:frame.bonePositions.map(v=>new THREE.Vector3(...v)),rotations:frame.boneRotations.map(v=>new THREE.Quaternion(...v))};
  const angles=headAngles(new THREE.Quaternion(...frame.headQuaternion));currentCenter.set(...(frame.renderCOM??frame.com));
  bridge.pose(transforms,{age:frame.time,visualFish:{...neutralMechanicsFish(),...angles,age:frame.time},headAngularVelocity:new THREE.Vector3(...frame.headOmega)});
  const head=transforms.rotations[0].clone().multiply(sampler.rest[0].clone().invert());
  const tail=transforms.rotations[33].clone().multiply(sampler.rest[33].clone().invert());
  $('bend').textContent=(head.angleTo(tail)*degrees).toFixed(1)+'°';
  $('joint').textContent=(frame.largestJoint.angle*degrees).toFixed(1)+'°';
  for(const [id,key] of [['yaw','yaw'],['pitch','pitch'],['roll','bank']])$(id).textContent=(angles[key]*degrees).toFixed(1)+'°';
  $('anchor').textContent=(frame.anchorError*1e6).toFixed(3)+' µm';
  $('join').textContent=(bridge.measure().maximumPositionError*1e6).toFixed(3)+' µm';
  $('energy').textContent=frame.kineticEnergy<.01?(frame.kineticEnergy*1e6).toFixed(1)+' µJ':frame.kineticEnergy.toFixed(4)+' J';
  $('time').textContent=frame.time.toFixed(3)+' / '+run.frames.at(-1).time.toFixed(3)+' s';
  $('timeline').value=frame.time;$('field').dataset.time=frame.time;render();
}

function resize(){
  if(!renderer)return;const rect=$('field').getBoundingClientRect();
  renderer.setPixelRatio(Math.min(devicePixelRatio||1,2));renderer.setSize(rect.width,rect.height,false);render();
}

function controls(disabled){for(const id of ['play','reset','run','timeline'])$(id).disabled=disabled;}
async function choose(index){
  const ticket=++selection;controls(true);state.playing=false;state.time=0;shown=-1;$('play').textContent='Play';
  $('loading').hidden=false;$('loading').textContent='Loading the selected physical run…';
  try{
    const selected=await runs[index].load();if(ticket!==selection)return;run=selected;
    const [title,note]=caseNotes[index];$('case-title').textContent=title;$('case-note').textContent=note;
    $('case-index').textContent=`Experiment ${String(index+1).padStart(2,'0')} / 03`;
    const physical=run.physicalScale,geometry=run.physicalGeometry;
    $('scale').textContent=geometry.lengthM<1?(geometry.lengthM*100).toFixed(0)+' cm · '+(geometry.massKg*1000).toFixed(1)+' g':geometry.lengthM.toFixed(2)+' m · '+geometry.massKg.toFixed(1)+' kg';
    $('scale-note').textContent=physical?'Uniformly scaled koi geometry; comparative bluegill motion.':'The original geometry and uniform-density mass proxy.';
    $('status').textContent=`${runs[index].label} · integrated at ${run.rate} Hz · recorded at 120 Hz · experimental result`;
    $('timeline').min=run.frames[0].time;$('timeline').max=run.frames.at(-1).time;
    state.time=run.frames[0].time;show(state.time);$('loading').hidden=true;controls(false);
  }catch(error){if(ticket!==selection)return;run=undefined;$('loading').textContent='Study unavailable: '+error.message;console.error(error);}
}

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
  if(state.playing&&run){
    state.time=Math.min(run.frames.at(-1).time,state.time+dt*state.speed);show(state.time);
    if(state.time>=run.frames.at(-1).time){state.playing=false;$('play').textContent='Play';}
  }
  requestAnimationFrame(animate);
}
$('play').onclick=()=>{if(!run)return;if(state.time>=run.frames.at(-1).time){state.time=0;shown=-1;show(0);}state.playing=!state.playing;$('play').textContent=state.playing?'Pause':'Play';};
$('reset').onclick=()=>{state.playing=false;state.time=0;shown=-1;$('play').textContent='Play';show(0);};
$('run').onchange=event=>choose(Number(event.target.value));
$('camera').onchange=event=>{state.camera=event.target.value;render();};
$('speed').onchange=event=>state.speed=Number(event.target.value);
$('anatomy').onchange=event=>{state.anatomy=event.target.checked;anatomy();};
$('follow').onchange=event=>{state.follow=event.target.checked;render();};
$('labels').onchange=event=>{state.labels=event.target.checked;render();};
$('timeline').oninput=event=>{state.playing=false;$('play').textContent='Play';state.time=Number(event.target.value);show(state.time);};
window.addEventListener('resize',resize);document.addEventListener('visibilitychange',()=>state.last=0);

try{
  renderer=new THREE.WebGPURenderer({canvas:$('canvas'),antialias:true,forceWebGL:true});await renderer.init();
  renderer.outputColorSpace=THREE.SRGBColorSpace;renderer.toneMapping=THREE.NeutralToneMapping;
  const [gltf,study]=await Promise.all([new GLTFLoader().loadAsync('aquarium/koi.glb'),loadWaterStudy()]);
  sampler=createAxialFrame(gltf.scene);({manifest,runs}=study);
  scene=new THREE.Scene();scene.background=new THREE.Color(0x061019);scene.add(new THREE.HemisphereLight(0xdce8ea,0x10212a,1.35));
  for(const [color,power,position] of [[0xfff0db,3.7,[-1,3,2]],[0x88bac8,1.6,[1,1,-2]]]){const light=new THREE.DirectionalLight(color,power);light.position.set(...position);scene.add(light);}
  instance=createKoiInstance(gltf.scene,{random:()=>.5,materialFor:material=>{
    const cloned=material.clone();originals.set(cloned,{opacity:cloned.opacity,transparent:cloned.transparent,depthWrite:cloned.depthWrite});cloned.side=THREE.DoubleSide;return cloned;
  }});scene.add(instance.asset);bridge=createRapierRenderBridge(instance,{sampler});
  const grid=new THREE.GridHelper(2.4,24,0x31505c,0x16303b);grid.position.set(0,-.32,0);grid.material.transparent=true;grid.material.opacity=.42;scene.add(grid);
  runs.forEach((item,index)=>{const option=document.createElement('option');option.value=index;option.textContent=item.label;$('run').append(option);});
  $('run').value='1';await choose(1);resize();requestAnimationFrame(animate);
}catch(error){console.error(error);$('loading').textContent='Study unavailable: '+error.message;}
