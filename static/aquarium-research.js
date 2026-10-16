import {Vector3,Quaternion} from './vendor/three.webgpu.js';
import {researchTopics,buildChapters} from './aquarium-research-data.js?v=89';
import {waterMax} from './aquarium-space.js?v=89';
import {spinalField} from './aquarium-motion.js?v=89';
import {createResearchTrace,traceAxes,createPitchResearchTrace,createRollResearchTrace,createSteeringResearchTrace} from './aquarium-research-trace.js?v=89';

const NS='http://www.w3.org/2000/svg';
const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const svg=(name,attrs={})=>{
  const element=document.createElementNS(NS,name);
  Object.entries(attrs).forEach(([key,value])=>element.setAttribute(key,String(value)));
  return element;
};
const element=(tag,className,text)=>{
  const node=document.createElement(tag);if(className)node.className=className;
  if(text)node.textContent=text;return node;
};

// Use the same camera and world positions as the renderer. Never estimate a
// feature's screen position from heading, depth or an image-space fish outline.
export function projectResearchPoint(world,camera,width,height){
  const view=world.clone().applyMatrix4(camera.matrixWorldInverse);
  const p=world.clone().project(camera);
  const inFront=view.z<-camera.near&&view.z>-camera.far;
  return {x:(p.x+1)*width/2,y:(1-p.y)*height/2,inFront,
    visible:inFront&&Math.abs(p.x)<1&&Math.abs(p.y)<1};
}

export function researchFinAnchor(asset,prefix){
  let mesh;asset.traverse(object=>{if(!mesh&&object.isSkinnedMesh&&object.name.startsWith(prefix))mesh=object;});
  if(!mesh)throw new Error(`Research anchor missing: ${prefix}`);
  return ()=>mesh.getVertexPosition(0,new Vector3()).applyMatrix4(mesh.matrixWorld);
}

// Read a posed joint's world attitude with its authored rest arch removed.
// Its local Y is the bone tangent, so a raw local-X rotation would be wrong.
export function researchJointPitch(joint){
  const orientation=joint.getWorldQuaternion(new Quaternion()).multiply(joint.userData.bodyRest.clone().invert()).normalize();
  return Math.asin(clamp(new Vector3(-1,0,0).applyQuaternion(orientation).y,-1,1));
}

export function researchJointRoll(joint){
  const orientation=joint.getWorldQuaternion(new Quaternion()).multiply(joint.userData.bodyRest.clone().invert()).normalize();
  const forward=new Vector3(-1,0,0).applyQuaternion(orientation),dorsal=new Vector3(0,1,0).applyQuaternion(orientation);
  const upright=new Vector3(0,1,0).addScaledVector(forward,-forward.y).normalize();
  // Positive roll follows the rig's caudal +X axis. Project gravity into the
  // transverse plane so a heading or climb cannot masquerade as body roll.
  return Math.atan2(-forward.dot(upright.clone().cross(dorsal)),upright.dot(dorsal));
}

// Sample the exported distal boundary after its live deformation and skinning.
// The outline therefore follows the same fan, attachment and camera as the fish.
export function researchFinContour(asset,prefix){
  let mesh;asset.traverse(object=>{if(!mesh&&object.isSkinnedMesh&&object.name.startsWith(prefix))mesh=object;});
  if(!mesh)throw new Error(`Research anchor missing: ${prefix}`);
  const uv=mesh.geometry.attributes.uv,edge=[];
  for(let i=0;i<uv.count;i++)if(uv.getY(i)<.001)edge.push(i);
  edge.sort((a,b)=>uv.getX(a)-uv.getX(b));
  return ()=>edge.map(i=>mesh.getVertexPosition(i,new Vector3()).applyMatrix4(mesh.matrixWorld));
}
export function researchCaudalContour(asset){return researchFinContour(asset,'Caudal_membrane');}

export function createResearchLayer({root,canvas,toggle,storyButton,asset,joints,camera,shape,getState,getFish,onAction,onVisibility=()=>{}}){
  const events=new AbortController(),signal=events.signal;
  let open=false,mode='research',active='head',reference=false,previous=-Infinity,traceMode,attitudeAxis='pitch';
  const layer=element('section','research-layer');layer.id='research-layer';layer.hidden=true;
  const pectoralAnchor=researchFinAnchor(asset,'Pectoral_membrane'),pelvicAnchor=researchFinAnchor(asset,'Pelvic_membrane');
  const caudalContour=researchCaudalContour(asset);
  const medianContours=['Dorsal_membrane','Anal_membrane'].map(prefix=>researchFinContour(asset,prefix));
  layer.setAttribute('aria-label','Aquarium research layer');
  const diagram=svg('svg',{'class':'research-diagram','aria-hidden':'true'});
  const spine=svg('path',{'class':'research-spine'}),ray=svg('path',{'class':'research-ray'}),leader=svg('path',{'class':'research-leader'});
  const tailOutline=svg('path',{'class':'research-spine'});
  const medianOutlines=medianContours.map(()=>svg('path',{'class':'research-spine research-median-outline'}));
  const trimArrows=[0,1].map(()=>svg('path',{'class':'research-trim-arrow'}));
  diagram.append(ray,spine,tailOutline,...medianOutlines,leader,...trimArrows);
  const dots=joints.map(()=>{const dot=svg('circle',{r:2.5,'class':'research-joint'});diagram.append(dot);return dot;});
  const pins=Array.from({length:5},()=>{
    const group=svg('g'),line=svg('path',{fill:'none',stroke:'rgba(150,210,190,.35)','stroke-width':.7}),halo=svg('circle',{r:8,'class':'research-pin-halo'}),point=svg('circle',{r:2.5,'class':'research-pin'}),label=svg('text',{'class':'research-pin-label',x:14,y:-12});
    group.append(line,halo,point,label);diagram.append(group);return {group,label,line};
  });
  const card=element('aside','research-card');card.setAttribute('aria-label','Research notes');
  const top=element('div','research-card-top'),eyebrow=element('span','research-eyebrow','FIELD NOTES / 01');
  const close=element('button','research-close','Close');close.type='button';close.setAttribute('aria-label','Close research layer');
  top.append(eyebrow,close);
  const title=element('h2','','The living mechanics'),intro=element('p','research-intro','Follow the animal. Trace the evidence.');
  const tabs=element('div','research-tabs');tabs.setAttribute('role','group');tabs.setAttribute('aria-label','Research view');
  const notesButton=element('button','','Explore the fish'),buildButton=element('button','','How this was built');
  notesButton.type=buildButton.type='button';tabs.append(notesButton,buildButton);
  const topics=element('nav','research-topics');topics.setAttribute('aria-label','Research topics');
  const topicButtons=researchTopics.map((topic,index)=>{
    const button=element('button','',`${String(index+1).padStart(2,'0')} ${topic.navLabel??topic.title}`);button.type='button';button.dataset.topic=topic.id;
    button.addEventListener('click',()=>select(topic.id),{signal});topics.append(button);return button;
  });
  const notes=element('div','research-notes'),topicTitle=element('h3'),claim=element('p','research-claim');
  const mini=svg('svg',{'class':'research-sequence',viewBox:'0 0 300 83','role':'img','aria-label':'Live steering and activation diagram'});
  const miniPath=svg('path',{'class':'research-sequence-path'});mini.append(miniPath);
  const miniCaption=element('p','research-diagram-caption','Axial pose offsets / authored activation');
  const miniDots=Array.from({length:4},(_,i)=>{
    const circle=svg('circle',{cx:24+i*84,cy:26,r:4,'class':'research-sequence-dot'});
    const text=svg('text',{x:24+i*84,y:65,'text-anchor':'middle'});text.textContent=['Head','Shoulder','Trunk','Tail'][i];mini.append(circle,text);return circle;
  });
  const live=element('div','research-live'),liveTag=element('span','','LIVE MODEL'),liveValue=element('span');live.append(liveTag,liveValue);
  const shapeBox=element('div','research-trace research-shape');
  const shapeTitle=element('div','research-trace-title','READ A TURN / LIVE SKIN');
  const shapePlot=svg('svg',{viewBox:'0 0 300 164',role:'img','aria-label':'Live rendered skin midline in the skull horizontal plane. Dashed line is an orthogonal fit through its anterior half. Brackets show perpendicular offsets of both caudal lobe tips. Fixed rest-length scale; this is a model diagnostic adapted from Wu and colleagues, not a recording of a fish.'});
  const shapeAxis=svg('path',{'class':'research-shape-axis'}),shapeCurve=svg('path',{'class':'research-shape-curve'});
  const shapeOffsets=[0,1].map(()=>svg('path',{'class':'research-shape-offset'}));
  const shapeTips=[0,1].map(()=>svg('circle',{r:2.5,'class':'research-shape-tip'}));
  const shapeNose=svg('circle',{r:2.5,'class':'research-shape-nose'});
  const shapeReadout=svg('text',{x:12,y:149,'class':'research-shape-value'}),shapeScale=svg('text',{x:288,y:149,'text-anchor':'end'});
  shapeScale.textContent='0.25 L';
  shapePlot.append(shapeAxis,shapeCurve,...shapeOffsets,...shapeTips,shapeNose,svg('path',{d:'M228,132H288m-60,-3v6m60,-6v6','class':'research-shape-scale'}),shapeReadout,shapeScale);
  shapeBox.append(shapeTitle,shapePlot,element('p','research-trace-caption','Solid: posed skin midline. Dashed: fitted anterior axis. Gold: caudal-tip offsets d(t)/L. Skull frame; fixed rest length L. Both lobes are shown.'));
  const trace=createResearchTrace(),traceBox=element('div','research-trace');
  const traceTitle=element('div','research-trace-title','BEND HISTORY / 4 SECONDS');
  const tracePlot=svg('svg',{viewBox:'0 0 300 108',role:'img','aria-label':'Authored trunk and tail pose offsets from rest over the last four simulation seconds, in three axes. Each row uses a fixed scale of minus to plus 45 degrees; values outside that range are clipped.'});
  const tracePaths=traceAxes.map((axis,i)=>{
    const label=svg('text',{x:0,y:28+i*30});label.textContent=['Sideways','Vertical','Twist'][i];
    const baseline=svg('path',{d:`M58,${25+i*30}H286`,'class':'research-trace-baseline'});
    const paths=[0,1].map(station=>svg('path',{'class':`research-trace-path research-trace-${axis}${station?' research-trace-tail':''}`}));
    tracePlot.append(label,baseline,...paths);return paths;
  });
  const traceStart=svg('text',{x:58,y:106}),traceEnd=svg('text',{x:286,y:106,'text-anchor':'end'});
  traceStart.textContent='−4 s';traceEnd.textContent='Now';tracePlot.append(traceStart,traceEnd);
  traceBox.append(traceTitle,tracePlot,element('p','research-trace-caption','Trunk solid / tail dotted · ±45° · authored model'));
  const pitchTrace=createPitchResearchTrace(),rollTrace=createRollResearchTrace(),steeringTrace=createSteeringResearchTrace(),pitchBox=element('div','research-trace research-pitch');
  const attitudeTitle=element('div','research-trace-title'),attitudeCaption=element('p','research-trace-caption');
  const attitudeControls=element('div','research-attitude-controls');attitudeControls.setAttribute('role','group');attitudeControls.setAttribute('aria-label','Fin motion diagram');
  const attitudeAxes=['pitch','roll','steering'];
  const attitudeButtons=attitudeAxes.map(axis=>{
    const button=element('button','',axis[0].toUpperCase()+axis.slice(1));button.type='button';
    button.addEventListener('click',()=>{attitudeAxis=axis;setAttitudeLabels();previous=-Infinity;update(performance.now());},{signal});
    attitudeControls.append(button);return button;
  });
  const pitchPlot=svg('svg',{viewBox:'0 0 300 86',role:'img','aria-label':'Four seconds of authored nose-up fin command, and actual head and tail frame pitch with the neutral arch removed. Fin command uses plus or minus 8.6 degrees; body frames use plus or minus 30 degrees. Positive values point upward. These are model signals, not measured fish kinematics.'});
  let attitudeFinLabel,attitudeAngleLabel;
  ['Fin trim','Pitch'].forEach((name,i)=>{
    const label=svg('text',{x:0,y:28+i*36});label.textContent=name;
    if(i===0)attitudeFinLabel=label;else attitudeAngleLabel=label;
    pitchPlot.append(label,svg('path',{d:`M58,${25+i*36}H286`,'class':'research-trace-baseline'}));
  });
  const pitchPaths=['fin','head','tail'].map(name=>svg('path',{'class':`research-trace-path research-pitch-${name}`}));pitchPlot.append(...pitchPaths);
  const steeringPaths=Array.from({length:4},(_,i)=>svg('path',{'class':`research-trace-path research-pitch-${i<2?'fin':'head'}${i%2?' research-trace-tail':''}`}));pitchPlot.append(...steeringPaths);
  const pitchStart=svg('text',{x:58,y:85}),pitchEnd=svg('text',{x:286,y:85,'text-anchor':'end'});
  pitchStart.textContent='−4 s';pitchEnd.textContent='Now';pitchPlot.append(pitchStart,pitchEnd);
  pitchBox.append(attitudeTitle,attitudeControls,pitchPlot,attitudeCaption);
  function setAttitudeLabels(){
    const roll=attitudeAxis==='roll',steering=attitudeAxis==='steering';pitchBox.dataset.axis=attitudeAxis;
    attitudeButtons.forEach((button,i)=>button.setAttribute('aria-pressed',String(attitudeAxes[i]===attitudeAxis)));
    pitchPaths.forEach(path=>{path.style.display=steering?'none':'';});
    steeringPaths.forEach(path=>{path.style.display=steering?'':'none';});
    attitudeFinLabel.textContent=steering?'Paddle':'Fin trim';
    if(steering){
      attitudeAngleLabel.textContent='Fore / aft';
      attitudeTitle.textContent='FINITE FIN STROKES / LIVE STEERING';
      attitudeCaption.textContent='Pectoral commands: paddle ±8.6° / fore-aft ±4.6°. Left solid, right dotted. Issued model angles, not measured fin kinematics or water forces.';
      pitchPlot.setAttribute('aria-label','Four seconds of actual issued left and right pectoral paddle and fore-aft commands. Left is solid and right dotted. Paddle uses plus or minus 8.6 degrees; fore-aft uses plus or minus 4.6 degrees. Values are clipped beyond those fixed scales. Timing and angles are authored, not measurements from a biological fish.');
      return;
    }
    attitudeAngleLabel.textContent=roll?'Roll':'Pitch';
    attitudeTitle.textContent=`FIN → HEAD → TAIL / LIVE ${roll?'ROLL':'PITCH'}`;
    attitudeCaption.textContent=roll?'Fin command ±8.6° / head solid, tail dotted ±7.4°. Rest arch removed. Bank direction, gains and recovery are authored.':
      'Fin command ±8.6° / head solid, tail dotted ±30°. Rest arch removed. Arrows show authored trim direction, not measured force.';
    pitchPlot.setAttribute('aria-label',roll?'Four seconds of authored signed fin roll command, and actual head and tail frame roll with neutral arch removed. Fin command uses plus or minus 8.6 degrees; body roll uses plus or minus 7.4 degrees, clipped beyond those limits. Positive roll follows the caudal axis. These are model signals, not measured fish kinematics.':
      'Four seconds of authored nose-up fin command, and actual head and tail frame pitch with the neutral arch removed. Fin command uses plus or minus 8.6 degrees; body frames use plus or minus 30 degrees. Positive values point upward. These are model signals, not measured fish kinematics.');
  }
  setAttitudeLabels();
  const implementationLabel=element('h4','','In this build'),implementation=element('p');
  const limitationLabel=element('h4','','Scope of the evidence'),limitation=element('p','research-caveat');
  const sourceLabel=element('h4','','Sources & footage'),sources=element('ol','research-sources');
  notes.append(topicTitle,claim,shapeBox,mini,miniCaption,traceBox,pitchBox,live,implementationLabel,implementation,limitationLabel,limitation,sourceLabel,sources);
  const story=element('div','research-story');story.hidden=true;
  buildChapters.forEach((chapter,index)=>{
    const section=element('section','research-chapter'),number=element('span','research-chapter-number',String(index+1).padStart(2,'0'));
    section.append(number,element('h3','',chapter.title),element('p','',chapter.text));
    const links=element('div','research-chapter-links');
    chapter.topics.forEach(id=>{
      const topic=researchTopics.find(item=>item.id===id);if(!topic)return;
      const button=element('button','',`Explore ${topic.title.toLowerCase()}`);button.type='button';
      button.addEventListener('click',()=>{setMode('research');select(id);},{signal});links.append(button);
    });section.append(links);story.append(section);
  });
  const mechanicsLink=element('a','research-mechanics-link','Inspect joint mechanics');
  mechanicsLink.href='aquarium-mechanics.html';
  story.append(element('p','','Watch the fish bend and twist from two cameras. This separate mechanics experiment develops the body response before water forces are coupled.'),mechanicsLink);
  const bibliography=element('details','research-bibliography'),summary=element('summary','','Complete source index');bibliography.append(summary);
  const allSources=element('ol','research-sources'),seen=new Set();
  researchTopics.flatMap(topic=>topic.sources).forEach(source=>{
    if(seen.has(source.url))return;seen.add(source.url);allSources.append(sourceItem(source));
  });summary.textContent=`Complete source index · ${seen.size} references`;bibliography.append(allSources);story.append(bibliography);
  const controls=element('div','research-controls'),pause=element('button','','Pause fish'),bones=element('button','','Show skeleton');
  pause.type=bones.type='button';pause.addEventListener('click',()=>onAction('pause'),{signal});bones.addEventListener('click',()=>onAction('skeleton'),{signal});controls.append(pause,bones);
  const unavailable=element('p','research-unavailable','Open Live aquarium to follow the moving anatomy.');unavailable.hidden=true;
  const legend=element('div','research-legend','ATTACHED TO THE LIVE RIG / DIAGRAMS ARE SCHEMATIC');
  card.append(top,title,intro,tabs,topics,notes,story,unavailable);layer.append(diagram,legend,controls,card);root.append(layer);

  function sourceItem(source){
    const li=element('li'),a=element('a','',source.label);a.href=source.url;a.target='_blank';a.rel='noopener noreferrer';li.append(a);return li;
  }
  function select(id){
    const topic=researchTopics.find(item=>item.id===id);if(!topic)return;active=id;
    card.scrollTop=0;
    topicButtons.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.topic===id)));
    topicTitle.textContent=topic.title;claim.textContent=topic.claim;implementation.textContent=topic.implementation;limitation.textContent=topic.limitation;
    sources.replaceChildren(...topic.sources.map(sourceItem));eyebrow.textContent=`FIELD NOTES / ${String(researchTopics.indexOf(topic)+1).padStart(2,'0')}`;
    mini.style.display=!reference&&['muscle','anatomy'].includes(active)?'':'none';miniCaption.hidden=reference||!['muscle','anatomy'].includes(active);live.hidden=reference;previous=-Infinity;
    layer.dataset.topic=id;
  }
  function setMode(value){
    mode=value;card.scrollTop=0;notes.hidden=topics.hidden=mode!=='research';story.hidden=mode!=='story';
    eyebrow.textContent=mode==='story'?`BUILD JOURNAL / ${String(buildChapters.length).padStart(2,'0')} STEPS`:`FIELD NOTES / ${String(researchTopics.findIndex(topic=>topic.id===active)+1).padStart(2,'0')}`;
    notesButton.setAttribute('aria-pressed',String(mode==='research'));buildButton.setAttribute('aria-pressed',String(mode==='story'));
    title.textContent=mode==='story'?'How this was built':'The living mechanics';
    intro.textContent=mode==='story'?'From real animals to a living desktop.':'Follow the animal. Trace the evidence.';
    diagram.style.display=mode==='story'||reference?'none':'';legend.hidden=mode==='story'||reference;previous=-Infinity;
  }
  function setOpen(value,view='research'){
    open=value;layer.hidden=!value;root.dataset.research=String(value);toggle.setAttribute('aria-expanded',String(value));toggle.setAttribute('aria-pressed',String(value));
    onVisibility(value);
    if(value){trace.clear();pitchTrace.clear();rollTrace.clear();steeringTrace.clear();setMode(view);update(performance.now());}else toggle.focus();
  }
  toggle.addEventListener('click',()=>setOpen(!open),{signal});storyButton.addEventListener('click',()=>setOpen(true,'story'),{signal});
  toggle.disabled=storyButton.disabled=false;
  close.addEventListener('click',()=>setOpen(false),{signal});notesButton.addEventListener('click',()=>setMode('research'),{signal});buildButton.addEventListener('click',()=>setMode('story'),{signal});
  root.addEventListener('keydown',event=>{if(open&&event.key==='Escape'){event.preventDefault();setOpen(false);}},{signal});
  select(active);setMode(mode);
  const vector=new Vector3();
  const local=(x,y,z)=>projectResearchPoint(asset.localToWorld(vector.set(x,y,z)).clone(),camera,canvas.clientWidth,canvas.clientHeight);
  function update(now){
    if(!open||now-previous<50)return;previous=now; // 20 Hz annotations bound DOM work independently of native rendering.
    const state=getState(),fish=getFish();pause.textContent=state.paused?'Resume fish':'Pause fish';pause.setAttribute('aria-pressed',String(state.paused));
    bones.textContent=state.wireframe?'Hide skeleton':'Show skeleton';bones.setAttribute('aria-pressed',String(state.wireframe));
    controls.hidden=reference;unavailable.hidden=!reference;
    mini.style.display=!reference&&['muscle','anatomy'].includes(active)?'':'none';miniCaption.hidden=reference||!['muscle','anatomy'].includes(active);live.hidden=reference;
    traceBox.hidden=reference||!['head','muscle','anatomy'].includes(active);
    shapeBox.hidden=reference||active!=='head';
    pitchBox.hidden=reference||active!=='fins'||fish.poseHistory===false;
    if(reference||mode==='story')return;
    if(active==='head'){
      const sample=shape.sample(),L=sample.length,point=p=>({x:16+p.x/L*240,y:67+p.y/L*240});
      const path=points=>points.map((p,i)=>{const q=point(p);return `${i?'L':'M'}${q.x.toFixed(2)},${q.y.toFixed(2)}`;}).join(' ');
      shapeCurve.setAttribute('d',path(sample.diagram.curve));shapeAxis.setAttribute('d',path([sample.diagram.axis.start,sample.diagram.axis.end]));
      const keys=['upper','lower'];
      keys.forEach((key,i)=>{
        const tip=point(sample.diagram.tips[key]),foot=point(sample.diagram.feet[key]);
        shapeOffsets[i].setAttribute('d',`M${tip.x},${tip.y}L${foot.x},${foot.y}`);
        shapeTips[i].setAttribute('cx',tip.x);shapeTips[i].setAttribute('cy',tip.y);
      });
      const nose=point(sample.diagram.curve[0]);shapeNose.setAttribute('cx',nose.x);shapeNose.setAttribute('cy',nose.y);
      shapeReadout.textContent=`Largest tip offset: ${sample.maximumDOverL.toFixed(3)} L`;
      shapePlot.dataset.offset=sample.maximumDOverL.toFixed(6);shapePlot.dataset.restLength=L.toFixed(6);
    }
    const width=canvas.clientWidth,height=canvas.clientHeight;
    const steady=fish.poseHistory===false||!Number.isFinite(fish.muscle1);
    const activation=steady?Array(4).fill(fish.stroke??0):[fish.stroke??0,fish.muscle1,fish.muscle2,fish.muscle3];
    diagram.setAttribute('viewBox',`0 0 ${width} ${height}`);
    const points=joints.map(joint=>projectResearchPoint(joint.getWorldPosition(vector).clone(),camera,width,height));
    const paths=[];let pen=false;
    points.forEach((p,i)=>{
      if(p.visible){paths.push(`${pen?'L':'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`);pen=true;}else pen=false;
      const dot=dots[i];dot.setAttribute('cx',p.x);dot.setAttribute('cy',p.y);dot.style.display=p.visible?'':'none';
      const s=clamp((i-4)/29,0,1)*3,k=Math.min(2,Math.floor(s));
      const value=activation[k]+(activation[k+1]-activation[k])*(s-k);
      dot.setAttribute('r',active==='muscle'?2+value*2.5:2.3);dot.setAttribute('opacity',active==='muscle'?.25+.75*value:.7);
    });
    const axial=['head','muscle','anatomy'].includes(active);spine.setAttribute('d',axial?paths.join(' '):'');dots.forEach(dot=>{if(!axial)dot.style.display='none';});
    const finContours=active==='fins'?[caudalContour(),...medianContours.map(contour=>contour())]:[[],[],[]];
    [tailOutline,...medianOutlines].forEach((outline,i)=>{
      let pen=false;
      outline.setAttribute('d',finContours[i].map(world=>{
        const p=projectResearchPoint(world,camera,width,height);
        if(!p.visible){pen=false;return '';}
        const command=`${pen?'L':'M'}${p.x.toFixed(1)},${p.y.toFixed(1)}`;pen=true;return command;
      }).join(' '));
    });
    let labels;
    if(active==='head')labels=state.study?[[points[1],'Rigid skull'],[points[10],'Trunk flexion'],[points[29],'Tail bend']]:[[points[1],'Head attitude'],[points[10],'Shoulder response'],[points[29],'Tail follow-through']];
    else if(active==='muscle')labels=[[points[7],'Activation'],[points[18],'Travelling bend'],[points[31],'Propulsion']];
    else if(active==='fins')labels=[[projectResearchPoint(pectoralAnchor(),camera,width,height),'Pectoral control'],[projectResearchPoint(pelvicAnchor(),camera,width,height),'Pelvic trim'],[points[32],'Caudal drive'],
      ...finContours.slice(1).map((contour,i)=>[projectResearchPoint(contour[Math.floor(contour.length/2)],camera,width,height),i?'Anal fin':'Dorsal fin'])];
    else if(active==='anatomy')labels=[[points[3],'Rigid anterior'],[points[16],'Articulated centra'],[points[31],'Caudal complex']];
    else if(active==='optics'){
      const center=joints[15].getWorldPosition(vector).clone(),surface=center.clone();surface.y=waterMax[1];
      const a=projectResearchPoint(surface,camera,width,height),b=points[15];
      labels=[[a,'Water interface'],[b,'Vertical depth'],[local(0,0,.12),'View-path attenuation']];
      // The surface is often above the frame. Let the SVG clip its true
      // projected segment instead of removing the depth diagram altogether.
      ray.setAttribute('d',a.inFront&&b.visible?`M${a.x},${a.y}L${b.x},${b.y}`:'');
    }else labels=[[local(-.53,.015,.09),'Modeled facial tissue'],[points[15],'Overlapping scales'],[projectResearchPoint(pectoralAnchor(),camera,width,height),'Branched fin rays']];
    if(active!=='optics')ray.setAttribute('d','');
    trimArrows.forEach((arrow,i)=>{
      const point=active==='fins'&&attitudeAxis==='pitch'?labels[i][0]:null,command=fish.pitchFin??0;
      if(!point?.visible||fish.poseHistory===false){arrow.setAttribute('d','');return;}
      const world=i===0?pectoralAnchor():pelvicAnchor();world.y+=clamp(command/.15,-1,1)*(i===0?.14:-.05);
      const end=projectResearchPoint(world,camera,width,height),dx=end.x-point.x,dy=end.y-point.y,length=Math.hypot(dx,dy);
      if(!end.visible||length<1){arrow.setAttribute('d','');return;}
      const x=dx/length,y=dy/length;
      arrow.setAttribute('d',`M${point.x},${point.y}L${end.x},${end.y}m${-x*4-y*3},${-y*4+x*3}l${x*4+y*3},${y*4-x*3}l${-x*4+y*3},${-y*4-x*3}`);
    });
    const box=card.getBoundingClientRect(),bounds=root.getBoundingClientRect(),placed=[];
    pins.forEach((pin,i)=>{if(i>=labels.length)pin.group.style.display='none';});
    labels.forEach(([p,text],i)=>{
      const pin=pins[i];pin.group.style.display=p?.visible?'':'none';if(!p?.visible)return;
      pin.group.setAttribute('transform',`translate(${p.x},${p.y})`);pin.label.textContent=text;
      const labelWidth=pin.label.getComputedTextLength(),right=width>650?box.left-bounds.left-20:width-16;
      const left=clamp(p.x>width*.57?p.x-14-labelWidth:p.x+14,16,Math.max(16,right-labelWidth));
      const candidates=[i===1?28:-14,-36,36,-58,58,-80,80];
      let baseline;
      for(const offset of candidates){
        const y=clamp(p.y+offset,190,width>650?height-90:box.top-bounds.top-36);
        const overlaps=placed.some(b=>left<b.right+6&&left+labelWidth>b.left-6&&y-11<b.bottom+6&&y+3>b.top-6);
        if(!overlaps){baseline=y;break;}
      }
      baseline??=p.y+candidates.at(-1);
      placed.push({left,right:left+labelWidth,top:baseline-11,bottom:baseline+3});
      pin.label.setAttribute('x',left-p.x);pin.label.setAttribute('text-anchor','start');pin.label.setAttribute('y',baseline-p.y);
      pin.line.setAttribute('d',`M0,0L${clamp(p.x,left,left+labelWidth)-p.x},${baseline-p.y-4}`);
    });
    const anchor=labels.find(([p])=>p?.visible)?.[0];
    const sx=box.left-bounds.left,sy=box.top-bounds.top+85;
    // On narrow screens the notes sit below the animal. Keep connectors in
    // the visible water rather than drawing through the reading surface.
    leader.setAttribute('d',anchor&&width>650?`M${sx},${sy}H${sx-22}L${anchor.x},${anchor.y}`:'');
    const vertical=Math.abs(fish.pitchRate??0)>Math.abs(fish.yawRate??0);
    // Read the same axial field that poses the rig, including the finite
    // turning stroke. An orientation-lag estimate alone misses that bend.
    const field=spinalField(fish,joints.map(joint=>joint.userData.axial));
    const currentTraceMode=state.study?state.studyAxis:'swimming';
    if(traceMode!==currentTraceMode){trace.clear();pitchTrace.clear();rollTrace.clear();steeringTrace.clear();traceMode=currentTraceMode;}
    trace.record(fish.age,field);
    tracePaths.forEach((paths,axis)=>paths.forEach((path,station)=>path.setAttribute('d',trace.paths(axis,station,fish.age))));
    if(fish.poseHistory!==false&&Number.isFinite(fish.pitchFin)){
      pitchTrace.record(fish.age,fish.pitchFin,researchJointPitch(joints[1]),researchJointPitch(joints.at(-1)));
      rollTrace.record(fish.age,fish.rollFin??0,researchJointRoll(joints[1]),researchJointRoll(joints.at(-1)));
      steeringTrace.record(fish.age,fish.pectoralPaddlePlus,fish.pectoralPaddleMinus,fish.pectoralForeAftPlus,fish.pectoralForeAftMinus);
      const attitudeTrace=attitudeAxis==='roll'?rollTrace:pitchTrace;
      pitchPaths.forEach((path,station)=>path.setAttribute('d',attitudeTrace.paths(station,fish.age)));
      steeringPaths.forEach((path,station)=>path.setAttribute('d',steeringTrace.paths(station,fish.age)));
    }
    const ys=[4,14,24,33].map(i=>26+clamp(field[i][vertical?'vertical':'lateral'],-.7,.7)*25);
    miniPath.setAttribute('d',ys.map((y,i)=>`${i?'L':'M'}${24+i*84},${y}`).join(' '));
    miniDots.forEach((dot,i)=>{dot.setAttribute('cy',ys[i]);dot.setAttribute('r',3+activation[i]*5);dot.setAttribute('opacity',.3+.7*activation[i]);});
    miniCaption.textContent=`${vertical?'Vertical':'Sideways'} pose offsets / ±40° / dot size: authored activation`;
    mini.setAttribute('aria-label','Four axial orientation offsets from rest in the animal frame, with a fixed plus or minus 40 degree scale. This is an orientation profile, not a centerline shape. Circle size shows the authored activation envelope, not measured EMG.');
    const taskLabel={trim:'fine trim stroke',steer:'steering stroke',brake:'settling stroke',balance:'balance correction',swim:'fin hold',hover:'hover',retreat:'retreat'}[fish.finTaskMode]??'unavailable';
    liveValue.textContent=active==='optics'?`${Math.max(0,waterMax[1]-joints[15].getWorldPosition(vector).y).toFixed(2)} m below surface`:
      active==='fins'?`${(fish.speed??0).toFixed(2)} m/s / ${attitudeAxis==='steering'?taskLabel:(fish.brake>.2?'braking':fish.stroke>.2?'propulsive bout':'glide / trim')}`:
      `${(fish.pitch*180/Math.PI).toFixed(1)}° pitch / ${(fish.speed??0).toFixed(2)} m/s`;
    canvas.dataset.researchAnchor=anchor?`${anchor.x.toFixed(1)},${anchor.y.toFixed(1)}`:'';
  }
  const requested=new URLSearchParams(location.search).get('research');if(requested)setOpen(true,requested==='story'?'story':'research');
  return {update,setReference(value){reference=value;diagram.style.display=value||mode==='story'?'none':'';legend.hidden=value||mode==='story';previous=-Infinity;update(performance.now());},
    dispose(){events.abort();layer.remove();toggle.disabled=storyButton.disabled=true;delete root.dataset.research;}};
}
