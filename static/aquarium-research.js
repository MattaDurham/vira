import {Vector3} from './vendor/three.webgpu.js';
import {researchTopics,buildChapters} from './aquarium-research-data.js?v=68';
import {waterMax} from './aquarium-space.js?v=68';

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
  return {x:(p.x+1)*width/2,y:(1-p.y)*height/2,
    visible:view.z<-camera.near&&view.z>-camera.far&&Math.abs(p.x)<1&&Math.abs(p.y)<1};
}

export function researchFinAnchor(asset,prefix){
  let mesh;asset.traverse(object=>{if(!mesh&&object.isSkinnedMesh&&object.name.startsWith(prefix))mesh=object;});
  if(!mesh)throw new Error(`Research anchor missing: ${prefix}`);
  return ()=>mesh.getVertexPosition(0,new Vector3()).applyMatrix4(mesh.matrixWorld);
}

export function createResearchLayer({root,canvas,toggle,storyButton,asset,joints,camera,getState,getFish,onAction,onVisibility=()=>{}}){
  const events=new AbortController(),signal=events.signal;
  let open=false,mode='research',active='head',reference=false,previous=-Infinity;
  const layer=element('section','research-layer');layer.id='research-layer';layer.hidden=true;
  const pectoralAnchor=researchFinAnchor(asset,'Pectoral_membrane'),pelvicAnchor=researchFinAnchor(asset,'Pelvic_membrane');
  layer.setAttribute('aria-label','Aquarium research layer');
  const diagram=svg('svg',{'class':'research-diagram','aria-hidden':'true'});
  const spine=svg('path',{'class':'research-spine'}),ray=svg('path',{'class':'research-ray'}),leader=svg('path',{'class':'research-leader'});
  diagram.append(ray,spine,leader);
  const dots=joints.map(()=>{const dot=svg('circle',{r:2.5,'class':'research-joint'});diagram.append(dot);return dot;});
  const pins=Array.from({length:3},()=>{
    const group=svg('g'),halo=svg('circle',{r:8,'class':'research-pin-halo'}),point=svg('circle',{r:2.5,'class':'research-pin'}),label=svg('text',{'class':'research-pin-label',x:14,y:-12});
    group.append(halo,point,label);diagram.append(group);return {group,label};
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
  const miniCaption=element('p','research-diagram-caption','Steering lag / circle size: authored activation');
  const miniDots=Array.from({length:4},(_,i)=>{
    const circle=svg('circle',{cx:24+i*84,cy:26,r:4,'class':'research-sequence-dot'});
    const text=svg('text',{x:24+i*84,y:65,'text-anchor':'middle'});text.textContent=['Head','Shoulder','Trunk','Tail'][i];mini.append(circle,text);return circle;
  });
  const live=element('div','research-live'),liveTag=element('span','','LIVE MODEL'),liveValue=element('span');live.append(liveTag,liveValue);
  const implementationLabel=element('h4','','In this build'),implementation=element('p');
  const limitationLabel=element('h4','','Scope of the evidence'),limitation=element('p','research-caveat');
  const sourceLabel=element('h4','','Sources & footage'),sources=element('ol','research-sources');
  notes.append(topicTitle,claim,mini,miniCaption,live,implementationLabel,implementation,limitationLabel,limitation,sourceLabel,sources);
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
  const bibliography=element('details','research-bibliography'),summary=element('summary','','Complete source index');bibliography.append(summary);
  const allSources=element('ol','research-sources'),seen=new Set();
  researchTopics.flatMap(topic=>topic.sources).forEach(source=>{
    if(seen.has(source.url))return;seen.add(source.url);allSources.append(sourceItem(source));
  });bibliography.append(allSources);story.append(bibliography);
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
    topicButtons.forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.topic===id)));
    topicTitle.textContent=topic.title;claim.textContent=topic.claim;implementation.textContent=topic.implementation;limitation.textContent=topic.limitation;
    sources.replaceChildren(...topic.sources.map(sourceItem));eyebrow.textContent=`FIELD NOTES / ${String(researchTopics.indexOf(topic)+1).padStart(2,'0')}`;
    mini.style.display=!reference&&['head','muscle','anatomy'].includes(active)?'':'none';miniCaption.hidden=reference||!['head','muscle','anatomy'].includes(active);live.hidden=reference;previous=-Infinity;
    layer.dataset.topic=id;
  }
  function setMode(value){
    mode=value;notes.hidden=topics.hidden=mode!=='research';story.hidden=mode!=='story';
    eyebrow.textContent=mode==='story'?'BUILD JOURNAL / 04 STEPS':`FIELD NOTES / ${String(researchTopics.findIndex(topic=>topic.id===active)+1).padStart(2,'0')}`;
    notesButton.setAttribute('aria-pressed',String(mode==='research'));buildButton.setAttribute('aria-pressed',String(mode==='story'));
    title.textContent=mode==='story'?'How this was built':'The living mechanics';
    intro.textContent=mode==='story'?'From real animals to a living desktop.':'Follow the animal. Trace the evidence.';
    diagram.style.display=mode==='story'||reference?'none':'';legend.hidden=mode==='story'||reference;previous=-Infinity;
  }
  function setOpen(value,view='research'){
    open=value;layer.hidden=!value;root.dataset.research=String(value);toggle.setAttribute('aria-expanded',String(value));toggle.setAttribute('aria-pressed',String(value));
    onVisibility(value);
    if(value){setMode(view);update(performance.now());}else toggle.focus();
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
    mini.style.display=!reference&&['head','muscle','anatomy'].includes(active)?'':'none';miniCaption.hidden=reference||!['head','muscle','anatomy'].includes(active);live.hidden=reference;
    if(reference||mode==='story')return;
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
    let labels;
    if(active==='head')labels=state.study?[[points[1],'Rigid skull'],[points[10],'Trunk flexion'],[points[29],'Tail bend']]:[[points[1],'Skull leads'],[points[10],'Shoulder follows'],[points[29],'Tail releases']];
    else if(active==='muscle')labels=[[points[7],'Activation'],[points[18],'Travelling bend'],[points[31],'Propulsion']];
    else if(active==='fins')labels=[[projectResearchPoint(pectoralAnchor(),camera,width,height),'Pectoral control'],[projectResearchPoint(pelvicAnchor(),camera,width,height),'Pelvic trim'],[points[32],'Caudal drive']];
    else if(active==='anatomy')labels=[[points[3],'Rigid anterior'],[points[16],'Articulated centra'],[points[31],'Caudal complex']];
    else if(active==='optics'){
      const center=joints[15].getWorldPosition(vector).clone(),surface=center.clone();surface.y=waterMax[1];
      const a=projectResearchPoint(surface,camera,width,height),b=points[15];
      labels=[[a,'Water interface'],[b,'Vertical depth'],[local(0,0,.12),'View-path attenuation']];
      ray.setAttribute('d',a.visible&&b.visible?`M${a.x},${a.y}L${b.x},${b.y}`:'');
    }else labels=[[local(-.53,.015,.09),'Modeled facial tissue'],[points[15],'Overlapping scales'],[projectResearchPoint(pectoralAnchor(),camera,width,height),'Branched fin rays']];
    if(active!=='optics')ray.setAttribute('d','');
    labels.forEach(([p,text],i)=>{
      const pin=pins[i];pin.group.style.display=p?.visible?'':'none';if(!p?.visible)return;
      pin.group.setAttribute('transform',`translate(${p.x},${p.y})`);pin.label.textContent=text;
      const left=p.x>width*.57;pin.label.setAttribute('x',left?-14:14);pin.label.setAttribute('text-anchor',left?'end':'start');
      pin.label.setAttribute('y',i===1?28:-14);
    });
    const anchor=labels.find(([p])=>p?.visible)?.[0],box=card.getBoundingClientRect(),bounds=root.getBoundingClientRect();
    const sx=box.left-bounds.left,sy=box.top-bounds.top+85;
    // On narrow screens the notes sit below the animal. Keep connectors in
    // the visible water rather than drawing through the reading surface.
    leader.setAttribute('d',anchor&&width>650?`M${sx},${sy}H${sx-22}L${anchor.x},${anchor.y}`:'');
    const vertical=Math.abs(fish.pitchRate??0)>Math.abs(fish.yawRate??0),prefix=vertical?'pitchLag':'yawLag',rate=vertical?(fish.pitchRate??0):(fish.yawRate??0);
    const lag=steady?[0,-rate/14,-rate*(1/14+1/12),-rate*(1/14+1/12+1/10)]:[0,fish[prefix+'1']??0,fish[prefix+'2']??0,fish[prefix+'3']??0];
    const ys=lag.map(value=>26+clamp(value,-.35,.35)*50);
    miniPath.setAttribute('d',ys.map((y,i)=>`${i?'L':'M'}${24+i*84},${y}`).join(' '));
    miniDots.forEach((dot,i)=>{dot.setAttribute('cy',ys[i]);dot.setAttribute('r',3+activation[i]*5);dot.setAttribute('opacity',.3+.7*activation[i]);});
    miniCaption.textContent=`${steady?'Steady study':vertical?'Pitch lag':'Yaw lag'} / circle size: authored activation`;
    mini.setAttribute('aria-label','Live diagram: steering lag bends the line; circle size shows the authored muscle activation envelope, not measured EMG.');
    liveValue.textContent=active==='optics'?`${Math.max(0,waterMax[1]-fish.y).toFixed(2)} m below surface`:
      active==='fins'?`${(fish.speed??0).toFixed(2)} m/s / ${fish.brake>.2?'braking':fish.stroke>.2?'propulsive bout':'glide / trim'}`:
      `${(fish.pitch*180/Math.PI).toFixed(1)}° pitch / ${(fish.speed??0).toFixed(2)} m/s`;
    canvas.dataset.researchAnchor=anchor?`${anchor.x.toFixed(1)},${anchor.y.toFixed(1)}`:'';
  }
  const requested=new URLSearchParams(location.search).get('research');if(requested)setOpen(true,requested==='story'?'story':'research');
  return {update,setReference(value){reference=value;diagram.style.display=value||mode==='story'?'none':'';legend.hidden=value||mode==='story';previous=-Infinity;update(performance.now());},
    dispose(){events.abort();layer.remove();toggle.disabled=storyButton.disabled=true;delete root.dataset.research;}};
}
