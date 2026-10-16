import assert from 'node:assert/strict';
import {create} from '../static/aquarium-background.js';

class Target{
  constructor(){this.listeners=new Map();}
  addEventListener(type,fn){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(fn);}
  removeEventListener(type,fn){this.listeners.get(type)?.delete(fn);}
  emit(type,fields={}){for(const fn of this.listeners.get(type)??[])fn({target:this,...fields});}
  count(){return [...this.listeners.values()].reduce((sum,list)=>sum+list.size,0);}
}

function environment(){
  const document=new Target(),window=new Target(),sent=[],messages=[],opened=[],timers=new Map();
  let serial=0;
  window.location={origin:'http://localhost:8399'};
  window.setTimeout=fn=>{timers.set(++serial,fn);return serial;};window.clearTimeout=id=>timers.delete(id);
  window.open=(...args)=>opened.push(args);document.defaultView=window;document.hidden=false;
  const node={ownerDocument:document,children:[],appendChild(frame){this.children.push(frame);frame.parent=this;}};
  const desktop={closest:()=>null},control={closest:()=>control};
  document.createElement=tag=>{
    assert.equal(tag,'iframe');
    const frame=new Target();frame.attributes={};frame.style={};frame.setAttribute=(key,value)=>{frame.attributes[key]=value;};
    frame.getBoundingClientRect=()=>({left:10,top:20,width:1000,height:500});
    frame.contentWindow={postMessage:(data,origin)=>sent.push({data,origin})};
    frame.remove=()=>{node.children=node.children.filter(child=>child!==frame);};
    return frame;
  };
  const start=options=>create({node,canInteract:target=>target===desktop,onMessage:value=>messages.push(value),...options});
  const message=(frame,data,extra={})=>window.emit('message',{source:frame.contentWindow,origin:window.location.origin,data,...extra});
  const event=(extra={})=>({target:desktop,clientX:510,clientY:270,button:0,defaultPrevented:false,...extra});
  return {document,window,node,desktop,control,sent,messages,opened,timers,start,message,event};
}

const data=record=>record.data;
{
  const env=environment(),stop=env.start({motion:true}),frame=env.node.children[0];
  assert.equal(frame.src,'/aquarium-benchmark.html?background=1','the real aquarium loads only after this adapter is selected');
  assert.equal(frame.tabIndex,-1);assert.equal(frame.attributes['aria-hidden'],'true');
  assert.equal(frame.style.opacity,'0','the static poster covers booting study/loading content without changing frame bounds');
  assert.equal(env.timers.size,1,'readiness has one finite deadline and no polling');
  env.document.emit('pointermove',env.event());assert.equal(env.sent.length,0,'no input enters a booting scene');
  stop.motion(false);stop.look('wireframe');
  for(const extra of [{origin:'https://elsewhere.example'},{source:{}}]){
    env.message(frame,{type:'vira-aquarium',action:'ready'},extra);
    env.message(frame,{type:'vira-aquarium',action:'error',message:'spoofed'},extra);
  }
  env.message(frame,{type:'another-app',action:'ready'});env.message(frame,['vira-aquarium','ready']);
  assert.equal(env.sent.length,0,'only the expected origin, frame and message type can acknowledge readiness');
  assert.equal(env.node.children.length,1);
  assert.equal(frame.style.opacity,'0','untrusted readiness cannot reveal study chrome');
  env.message(frame,{type:'vira-aquarium',action:'ready'});
  assert.deepEqual(data(env.sent.at(-1)),{type:'vira-aquarium',action:'configure',motion:false,look:'wireframe'},'readiness applies the latest preferences');
  assert.equal(env.sent.at(-1).origin,env.window.location.origin,'outgoing messages target the exact origin');
  assert.equal(frame.style.opacity,'1','only the completed first-frame handshake reveals the aquarium');
  assert.equal(env.timers.size,0);assert.equal(env.messages.at(-1),'');
  const readyMessages=env.sent.length;env.message(frame,{type:'vira-aquarium',action:'ready'});
  assert.equal(env.sent.length,readyMessages,'duplicate readiness does not create another bridge');
  stop.motion(true);
  env.document.emit('pointermove',env.event());
  assert.deepEqual(data(env.sent.at(-1)),{type:'vira-aquarium',action:'pointer',x:.5,y:.5},'CSS viewport coordinates map to normalized aquarium coordinates');
  env.document.emit('pointermove',env.event({target:env.control}));
  assert.deepEqual(data(env.sent.at(-1)),{type:'vira-aquarium',action:'pointer',x:null,y:null},'hovering a window or control clears fish attention');
  const quiet=env.sent.length;
  env.document.emit('pointermove',env.event({clientX:Infinity}));env.document.emit('pointermove',env.event({clientY:900}));
  for(const fields of [{target:env.control},{button:1},{defaultPrevented:true},{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{clientX:NaN},{clientX:-10}])env.document.emit('click',env.event(fields));
  assert.equal(env.sent.length,quiet,'UI clicks, modified clicks and invalid positions never enter the aquarium');
  let prevented=false;env.document.emit('click',env.event({preventDefault(){prevented=true;}}));
  assert.deepEqual(data(env.sent.at(-1)),{type:'vira-aquarium',action:'tap',x:.5,y:.5});
  assert.equal(prevented,false,'the aquarium keeps the desktop click gesture');
  assert.equal(env.document.listeners.get('dblclick')?.size??0,0,'desktop double-click behavior is untouched');
  env.document.emit('pointermove',env.event());env.window.emit('blur');
  assert.deepEqual(data(env.sent.at(-1)),{type:'vira-aquarium',action:'pointer',x:null,y:null});
  env.document.emit('pointermove',env.event());env.document.hidden=true;env.document.emit('visibilitychange');
  assert.deepEqual(data(env.sent.at(-1)),{type:'vira-aquarium',action:'configure',motion:false,look:'wireframe'});
  const hidden=env.sent.length;env.document.emit('click',env.event());env.document.emit('pointermove',env.event());
  assert.equal(env.sent.length,hidden,'a hidden desktop never forwards motion input');
  env.document.hidden=false;env.document.emit('visibilitychange');assert.equal(data(env.sent.at(-1)).motion,true);
  stop.motion(false);const paused=env.sent.length;
  env.document.emit('click',env.event());env.document.emit('pointermove',env.event());
  assert.equal(env.sent.length,paused,'paused or system-reduced motion disables attention and taps');
  stop.look('invalid');stop.motion('true');assert.equal(env.sent.length,paused,'configuration fields remain bounded');
  stop.explore();assert.deepEqual(env.opened,[['/aquarium-benchmark.html','_blank','noopener']]);
  stop();stop();
  assert.equal(env.node.children.length,0);assert.equal(env.document.count(),0);assert.equal(env.window.count(),0);assert.equal(frame.count(),0);
  assert.equal(env.timers.size,0,'disposal retires every timer and listener');
  const retired=env.sent.length;env.message(frame,{type:'vira-aquarium',action:'ready'});stop.motion(true);stop.look('natural');
  assert.equal(env.sent.length,retired,'a removed frame cannot rejoin through a stale message');
}
{
  const env=environment(),stop=env.start({motion:true}),frame=env.node.children[0];
  env.document.hidden=true;env.message(frame,{type:'vira-aquarium',action:'ready'});
  assert.equal(data(env.sent.at(-1)).motion,false,'a frame that finishes loading in a hidden tab remains still');
  env.message(frame,{type:'vira-aquarium',action:'error',message:'x'.repeat(241)});
  assert.equal(env.node.children.length,1,'oversized error messages are ignored');
  env.message(frame,{type:'vira-aquarium',action:'error',message:'GPU\nnot available'});
  assert.equal(env.node.children.length,0);assert.equal(env.document.count(),0);assert.equal(env.window.count(),0);
  assert.match(env.messages.at(-1),/GPU not available/);assert.equal(stop.look,null,'a failed live renderer does not advertise a working look control');
}
{
  const env=environment(),stop=env.start(),frame=env.node.children[0];
  const timeout=[...env.timers.values()][0];timeout();
  assert.equal(env.node.children.length,0);assert.equal(env.timers.size,0);
  assert.match(env.messages.at(-1),/did not finish loading/);stop();
  env.message(frame,{type:'vira-aquarium',action:'ready'});assert.equal(env.sent.length,0);
}
{
  const env=environment();env.start();env.node.children[0].emit('error');
  assert.equal(env.node.children.length,0);assert.match(env.messages.at(-1),/could not load/);
}
console.log('Aquarium background readiness, source/origin filtering, desktop input, visibility, motion, errors and cleanup pass.');
