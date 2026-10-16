import assert from 'node:assert/strict';
import {createAquariumEmbed,createFrameCadence,backgroundPixelRatio} from '../static/aquarium-embed.js';

class Events{
  constructor(){this.listeners=new Map();}
  addEventListener(type,fn){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(fn);}
  removeEventListener(type,fn){this.listeners.get(type)?.delete(fn);}
  emit(type,event={}){for(const fn of [...(this.listeners.get(type)??[])])fn(event);}
  get listenerCount(){return [...this.listeners.values()].reduce((sum,set)=>sum+set.size,0);}
}
function fixture(callbacks={}){
  const host=new Events(),document=new Events(),media=new Events(),posts=[],calls={configure:[],pointer:[],tap:[],activity:[]},order=[];
  document.hidden=false;media.matches=false;
  Object.assign(host,{location:{origin:'https://aquarium.example'},document,matchMedia:query=>{assert.equal(query,'(prefers-reduced-motion: reduce)');return media;},parent:{postMessage:(packet,origin)=>{posts.push({packet,origin});order.push(packet.action);}}});
  const embed=createAquariumEmbed({host,...Object.fromEntries(['Configure','Pointer','Tap','Activity'].map(name=>['on'+name,value=>{calls[name.toLowerCase()].push(value);order.push(name.toLowerCase());callbacks['on'+name]?.(value);}]))});
  const send=(action,detail={},overrides={})=>host.emit('message',{origin:host.location.origin,source:host.parent,data:{type:'vira-aquarium',action,...detail},...overrides});
  return {host,document,media,posts,calls,order,embed,send};
}
function assertMessaging(){
  const f=fixture();assert.equal(f.embed.active,false);assert.deepEqual(f.embed.configuration,{motion:false,look:'natural'});assert.deepEqual(f.calls.activity,[]);
  for(const action of ['pointer','tap'])f.send(action,{x:.3,y:.7});
  assert.deepEqual(f.calls.pointer,[]);assert.deepEqual(f.calls.tap,[]);
  f.send('configure',{motion:true,look:'natural'});f.send('configure',{motion:false,look:'wireframe'});f.send('configure',{motion:true,look:'wireframe'});
  assert.deepEqual(f.calls.configure,[],'configuration is queued until assets are ready');
  const wrongPackets=[null,42,[],{type:'other',action:'configure',motion:false,look:'natural'},{type:'vira-aquarium',action:'configure',motion:'false',look:'natural'},{type:'vira-aquarium',action:'configure',motion:false,look:'other'},{type:'vira-aquarium',action:'configure',motion:false},{type:'vira-aquarium',action:'configure',look:'natural'}];
  for(const data of wrongPackets)f.host.emit('message',{origin:f.host.location.origin,source:f.host.parent,data});
  f.send('configure',{motion:false,look:'natural'},{origin:'https://other.example'});f.send('configure',{motion:false,look:'natural'},{source:{}});
  assert.deepEqual(f.embed.configuration,{motion:true,look:'wireframe'},'wrong sources/origins and incomplete packets cannot alter the queued preference');
  assert.equal(f.embed.ready({model:'refined',engine:'WebGPU'}),true);
  assert.deepEqual(f.calls.configure,[{motion:true,look:'wireframe'}]);assert.deepEqual(f.calls.activity,[true]);assert.equal(f.embed.active,true);
  assert.deepEqual(f.order,['configure','activity','ready'],'configuration is applied before activation and parent readiness');
  assert.deepEqual(f.posts,[{packet:{type:'vira-aquarium',action:'ready',model:'refined',engine:'WebGPU'},origin:f.host.location.origin}]);assert.equal(f.embed.ready({model:'other',engine:'other'}),false);
  const copy=f.embed.configuration;copy.motion=false;f.calls.configure[0].motion=false;assert.equal(f.embed.configuration.motion,true,'callbacks and consumers cannot mutate internal preferences');
  f.send('configure',{motion:true,look:'wireframe'});assert.equal(f.calls.configure.length,1,'same-value configuration does not repeat callbacks');
  f.send('configure',{motion:false,look:'natural'});assert.deepEqual(f.calls.activity,[true,false]);
  for(const [x,y] of [[0,0],[1,1],[.25,.75],[null,null]])f.send('pointer',{x,y});
  for(const [x,y] of [[0,0],[1,1],[.5,.5]])f.send('tap',{x,y});
  assert.deepEqual(f.calls.pointer,[{x:0,y:0},{x:1,y:1},{x:.25,y:.75},null]);assert.equal(f.calls.tap.length,3,'ready inputs are forwarded even while motion preference is paused');
  const badPairs=[[null,0],[0,null],[NaN,.1],[.1,Infinity],[-.1,0],[1.1,0],['0',0],[0,undefined],[true,.1]];
  for(const [x,y] of badPairs)for(const action of ['pointer','tap'])f.send(action,{x,y});f.send('tap',{x:null,y:null});
  for(const action of ['pointer','tap']){f.send(action,{x:.1,y:.2},{source:{}});f.send(action,{x:.1,y:.2},{origin:'https://other.example'});}
  assert.equal(f.calls.pointer.length,4);assert.equal(f.calls.tap.length,3);
  f.embed.dispose();assert.equal(f.host.listenerCount+f.document.listenerCount+f.media.listenerCount,0);
}
function assertPointerClear(){
  const followed=[];let projected=0;
  const project=point=>{projected++;return {x:point.x*10,y:point.y*10};};
  const actor={follow:point=>followed.push(point)};
  const f=fixture({onPointer:point=>actor.follow(point?project(point):null)});
  f.send('configure',{motion:true,look:'natural'});f.embed.ready();
  f.send('pointer',{x:.2,y:.4});assert.equal(projected,1);
  f.send('pointer',{x:null,y:null});assert.equal(projected,1);assert.equal(followed.at(-1),null,'active clear bypasses coordinate projection');
  f.send('configure',{motion:false,look:'natural'});
  f.send('pointer',{x:null,y:null});assert.equal(projected,1);assert.equal(followed.at(-1),null,'paused clear bypasses coordinate projection');
  assert.deepEqual(followed,[{x:2,y:4},null,null]);f.embed.dispose();
}
function assertActivity(){
  const f=fixture();f.document.hidden=true;f.media.matches=true;f.send('configure',{motion:true,look:'natural'});f.embed.ready({model:'refined',engine:'WebGL'});
  assert.equal(f.embed.active,false);assert.deepEqual(f.calls.activity,[]);f.document.hidden=false;f.document.emit('visibilitychange');assert.deepEqual(f.calls.activity,[],'reduced motion still suppresses the now-visible child');
  f.media.matches=false;f.media.emit('change');assert.deepEqual(f.calls.activity,[true]);
  f.document.emit('visibilitychange');f.media.emit('change');assert.deepEqual(f.calls.activity,[true],'unchanged effective activity is idempotent');
  f.document.hidden=true;f.document.emit('visibilitychange');f.document.hidden=false;f.document.emit('visibilitychange');f.media.matches=true;f.media.emit('change');f.media.matches=false;f.media.emit('change');
  assert.deepEqual(f.calls.activity,[true,false,true,false,true]);
  f.embed.dispose();assert.equal(f.embed.active,false);assert.deepEqual(f.calls.activity,[true,false,true,false,true,false]);
  const count=JSON.stringify(f.calls),posts=f.posts.length;f.embed.dispose();f.send('configure',{motion:true,look:'natural'});f.document.emit('visibilitychange');f.media.emit('change');f.embed.ready({model:'late',engine:'late'});f.embed.error('late');
  assert.equal(JSON.stringify(f.calls),count);assert.equal(f.posts.length,posts);assert.equal(f.host.listenerCount+f.document.listenerCount+f.media.listenerCount,0);
}
function assertFailures(){
  const f=fixture();f.send('configure',{motion:true,look:'natural'});f.embed.ready({model:'refined',engine:'WebGPU'});assert.equal(f.embed.error('failure '.repeat(100)),true);
  const error=f.posts.at(-1);assert.equal(error.origin,f.host.location.origin);assert.equal(error.packet.type,'vira-aquarium');assert.equal(error.packet.action,'error');assert.equal(error.packet.message.length,240);assert.equal(f.embed.active,false);assert.deepEqual(f.calls.activity,[true,false]);
  f.send('configure',{motion:true,look:'wireframe'});f.send('pointer',{x:.5,y:.5});f.send('tap',{x:.5,y:.5});assert.equal(f.embed.ready({model:'late',engine:'late'}),false);assert.equal(f.embed.error('again'),false);assert.equal(f.posts.length,2);assert.deepEqual(f.calls.pointer,[]);assert.deepEqual(f.calls.tap,[]);f.embed.dispose();
  const abandoned=fixture();abandoned.embed.dispose();assert.equal(abandoned.embed.ready({model:'late',engine:'late'}),false);assert.equal(abandoned.embed.error('late'),false);assert.deepEqual(abandoned.posts,[]);
  let reentrant;
  reentrant=fixture({onConfigure:()=>reentrant.embed.dispose()});reentrant.send('configure',{motion:true,look:'natural'});assert.equal(reentrant.embed.ready({model:'refined',engine:'WebGPU'}),false);assert.deepEqual(reentrant.posts,[]);assert.deepEqual(reentrant.calls.activity,[]);
  let failed;
  failed=fixture({onConfigure:()=>failed.embed.error('setup failed')});failed.send('configure',{motion:true,look:'natural'});assert.equal(failed.embed.ready({model:'refined',engine:'WebGPU'}),false);assert.deepEqual(failed.posts.map(p=>p.packet.action),['error']);assert.equal(failed.embed.active,false);failed.embed.dispose();
  let removed;
  removed=fixture({onActivity:value=>{if(value)removed.embed.dispose();}});removed.send('configure',{motion:true,look:'natural'});assert.equal(removed.embed.ready({model:'refined',engine:'WebGPU'}),false);assert.deepEqual(removed.posts,[]);assert.deepEqual(removed.calls.activity,[true,false]);
  const initialThrow=fixture({onConfigure:()=>{throw new Error('initial configuration failed');}});
  initialThrow.send('configure',{motion:true,look:'natural'});assert.equal(initialThrow.embed.ready(),false);assert.equal(initialThrow.embed.active,false);assert.deepEqual(initialThrow.posts.map(p=>p.packet.action),['error']);assert.equal(initialThrow.posts[0].packet.message,'initial configuration failed');assert.equal(initialThrow.embed.ready(),false);initialThrow.send('configure',{motion:true,look:'wireframe'});assert.equal(initialThrow.posts.length,1);initialThrow.embed.dispose();
  let configured=0;
  const pauseThrow=fixture({onConfigure:()=>{if(++configured>1)throw new Error('pause configuration failed');}});
  pauseThrow.send('configure',{motion:true,look:'natural'});assert.equal(pauseThrow.embed.ready(),true);assert.equal(pauseThrow.embed.active,true);
  pauseThrow.send('configure',{motion:false,look:'natural'});assert.equal(pauseThrow.embed.active,false);assert.deepEqual(pauseThrow.calls.activity,[true,false]);assert.deepEqual(pauseThrow.posts.map(p=>p.packet.action),['ready','error']);assert.equal(pauseThrow.posts[1].packet.message,'pause configuration failed');pauseThrow.send('configure',{motion:true,look:'natural'});assert.equal(pauseThrow.embed.ready(),false);assert.equal(pauseThrow.posts.length,2);pauseThrow.embed.dispose();
}
function frameClock(sourceRate){
  const cadence=createFrameCadence(30);let frames=0,time=0,position=0,accumulator=0,ticks=0;
  for(let i=0;i<=sourceRate*2;i++){
    if(!cadence.accept(i*1000/sourceRate))continue;frames++;const dt=cadence.elapsed;time+=dt;position+=.2*dt;accumulator+=dt;
    while(accumulator+1e-12>=1/120){accumulator-=1/120;ticks++;}
  }
  assert.equal(frames,61,'initial render plus exactly30 accepted intervals per second');assert.ok(Math.abs(time-2)<1e-12);assert.ok(Math.abs(position-.4)<1e-12,'skipping rendering callbacks must preserve elapsed motion');assert.equal(ticks,240,'the actual accepted-frame clock advances the same fixed120Hz accumulator');return {sourceRate,frames,elapsed:time,ticks};
}
function assertCadence(){
  const rates=[30,60,120,144].map(frameClock),c=createFrameCadence();assert.equal(c.elapsed,0);assert.equal(c.accept(0),true);assert.equal(c.elapsed,0);assert.equal(c.accept(0),false);
  assert.equal(c.accept(16),false);assert.equal(c.accept(NaN),false);assert.equal(c.accept(Infinity),false);assert.equal(c.accept(1000/30),true);assert.ok(Math.abs(c.elapsed-1/30)<1e-12);
  assert.equal(c.accept(5000),true);assert.ok(Math.abs(c.elapsed-(5-1/30))<1e-12);assert.equal(c.accept(5000),false);assert.equal(c.accept(5001),false,'a long gap never emits a catch-up burst');
  c.reset();assert.equal(c.elapsed,0);assert.equal(c.accept(9000),true);assert.equal(c.elapsed,0,'resume begins a new accepted-time epoch');assert.equal(c.accept(10),true);assert.equal(c.elapsed,0,'rewinding the clock begins a new epoch');
  for(const fps of [0,-1,NaN,Infinity,Number.MIN_VALUE])assert.throws(()=>createFrameCadence(fps),RangeError);
  // A raw-callback-dt integration would produce only half/quarter the intended
  // motion at60/120Hz. The joined accepted-clock oracle above requires .4m.
  assert.ok(Math.abs((60/60)*.2-.4)>.1);assert.ok(Math.abs((60/120)*.2-.4)>.1);
  return rates;
}
function assertPixels(){
  assert.equal(backgroundPixelRatio(1000,1000,1),1);assert.equal(backgroundPixelRatio(1000,1000,4),2);
  for(const dpr of [undefined,0,-1,NaN,Infinity])assert.equal(backgroundPixelRatio(1000,1000,dpr),1);
  for(const [w,h] of [[0,100],[100,0],[-1,100],[NaN,100],[Infinity,100]])assert.equal(backgroundPixelRatio(w,h,2),1);
  for(const [w,h,dpr,budget] of [[3840,2160,2,4194304],[7680,4320,2,4194304],[2000,1000,2,1000000],[1e308,1e308,2,4194304]]){
    const r=backgroundPixelRatio(w,h,dpr,budget),x=w*r,y=h*r;assert.ok(Number.isFinite(r)&&r>0&&r<=2);assert.ok(Number.isFinite(x)&&Number.isFinite(y));assert.ok(x*y<=budget*(1+1e-12));assert.ok(Math.floor(x)*Math.floor(y)<=budget);
  }
  assert.equal(backgroundPixelRatio(2000,1000,2,NaN),backgroundPixelRatio(2000,1000,2));
}
assertMessaging();assertPointerClear();assertActivity();assertFailures();const cadence=assertCadence();assertPixels();
console.log(JSON.stringify({messaging:true,activity:true,terminalErrors:true,cleanup:true,cadence,pixelBudget:true}));
