import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';
import {createAquariumEmbed,createFrameCadence,backgroundPixelRatio} from '../static/aquarium-embed.js';

class Events{
  constructor(){this.listeners=new Map();}
  addEventListener(type,fn){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(fn);}
  removeEventListener(type,fn){this.listeners.get(type)?.delete(fn);}
  emit(type,event={}){for(const fn of [...(this.listeners.get(type)??[])])fn(event);}
  get listenerCount(){return [...this.listeners.values()].reduce((sum,set)=>sum+set.size,0);}
}
function fixture(callbacks={}){
  const host=new Events(),document=new Events(),media=new Events(),posts=[],calls={configure:[],pointer:[],tap:[],activity:[],visible:[]},order=[];
  document.hidden=false;media.matches=false;
  Object.assign(host,{location:{origin:'https://aquarium.example'},document,matchMedia:query=>{assert.equal(query,'(prefers-reduced-motion: reduce)');return media;},parent:{postMessage:(packet,origin)=>{posts.push({packet,origin});order.push(packet.action);}}});
  const embed=createAquariumEmbed({host,...Object.fromEntries(['Configure','Pointer','Tap','Activity','Visible'].map(name=>['on'+name,value=>{calls[name.toLowerCase()].push(value);order.push(name.toLowerCase());callbacks['on'+name]?.(value);}]))});
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
function assertProgress(){
  const f=fixture();f.send('configure',{motion:true,look:'wireframe'});
  const stages=['module-loading','module-ready','data','renderer','textures','model','rig','compile','optics','first-frame','ready'];
  for(const stage of stages){
    const before=f.posts.length;assert.equal(f.embed.progress(stage),true);assert.equal(f.posts.length,before+1);
    assert.deepEqual(f.posts.at(-1),{origin:f.host.location.origin,packet:{type:'vira-aquarium',action:'progress',stage,engine:'unknown',hidden:false}});
  }
  f.document.hidden=true;
  for(const engine of ['webgpu','webgl2']){assert.equal(f.embed.progress('compile',{engine,ignored:'not transmitted'}),true);assert.deepEqual(f.posts.at(-1).packet,{type:'vira-aquarium',action:'progress',stage:'compile',engine,hidden:true});}
  const before=f.posts.length;
  for(const stage of [undefined,null,'','loading','COMPiLE',{},42])assert.equal(f.embed.progress(stage),false);
  for(const engine of [null,'','WebGPU','webgl',false,{},42])assert.equal(f.embed.progress('data',{engine}),false);
  for(const detail of [null,false,42,'webgpu',[]])assert.equal(f.embed.progress('data',detail),false);
  assert.equal(f.posts.length,before);assert.equal(f.embed.active,false);assert.deepEqual(f.embed.configuration,{motion:true,look:'wireframe'});assert.deepEqual(f.calls.configure,[]);assert.deepEqual(f.calls.activity,[],'progress cannot activate or consume queued configuration');
  f.document.hidden=false;f.embed.ready();assert.equal(f.embed.active,true);const activity=f.calls.activity.slice();
  assert.equal(f.embed.progress('ready',{engine:'webgpu'}),true);assert.equal(f.embed.active,true);assert.deepEqual(f.calls.activity,activity,'progress leaves active motion untouched');
  f.embed.error('failed');const failedPosts=f.posts.length;assert.equal(f.embed.progress('compile'),false);assert.equal(f.posts.length,failedPosts);f.embed.dispose();
  const disposed=fixture();disposed.embed.dispose();assert.equal(disposed.embed.progress('module-loading'),false);assert.deepEqual(disposed.posts,[]);
}
function assertVisibilityStill(){
  const f=fixture();f.document.emit('visibilitychange');assert.equal(f.calls.visible.length,0,'a visible child before readiness cannot draw');
  f.document.hidden=true;f.embed.ready({model:'refined',engine:'webgpu'});assert.equal(f.embed.active,false);assert.equal(f.calls.visible.length,0);
  f.document.hidden=false;f.document.emit('visibilitychange');assert.equal(f.calls.visible.length,1,'a paused child must draw its first still after hidden startup');assert.deepEqual(f.calls.activity,[],'revealing a paused child never advances motion');assert.equal(f.embed.configuration.motion,false);
  f.document.hidden=true;f.document.emit('visibilitychange');assert.equal(f.calls.visible.length,1,'hiding never requests a still');
  f.embed.dispose();f.document.hidden=false;f.document.emit('visibilitychange');assert.equal(f.calls.visible.length,1);assert.equal(f.host.listenerCount+f.document.listenerCount+f.media.listenerCount,0);
  const reduced=fixture();reduced.media.matches=true;reduced.document.hidden=true;reduced.send('configure',{motion:true,look:'natural'});reduced.embed.ready();reduced.document.hidden=false;reduced.document.emit('visibilitychange');assert.equal(reduced.calls.visible.length,1);assert.deepEqual(reduced.calls.activity,[],'reduced motion can reveal a still without activating simulation');reduced.embed.error('failed');reduced.document.emit('visibilitychange');assert.equal(reduced.calls.visible.length,1);reduced.embed.dispose();
  const throwing=fixture({onVisible:()=>{throw new Error('still failed');}});throwing.document.hidden=true;throwing.embed.ready();throwing.document.hidden=false;throwing.document.emit('visibilitychange');assert.equal(throwing.embed.active,false);assert.deepEqual(throwing.posts.map(post=>post.packet.action),['ready','error']);assert.equal(throwing.posts.at(-1).packet.message,'still failed');throwing.document.emit('visibilitychange');assert.equal(throwing.calls.visible.length,1);throwing.embed.dispose();
}
function assertEarlyHTMLReporter(){
  const html=readFileSync(new URL('../static/aquarium-benchmark.html',import.meta.url),'utf8');
  const source=html.match(/<script\b[^>]*\bid=["']aquarium-startup["'][^>]*>([\s\S]*?)<\/script>/i)?.[1];
  assert.ok(source,'exercise the actual HTML bootstrap before its module graph runs');
  function startup({search='?background=1',hidden=false}={}){
    const host=new Events(),document=new Events(),media=new Events(),posts=[],registration=[],removal=[],loading={textContent:'Preparing the aquarium'};
    document.hidden=hidden;document.documentElement={dataset:{}};document.getElementById=id=>id==='loading'?loading:null;media.matches=false;
    Object.assign(host,{location:{origin:'https://aquarium.example',search},document,matchMedia:()=>media,parent:{postMessage:(packet,origin)=>posts.push({packet:JSON.parse(JSON.stringify(packet)),origin})}});
    const add=host.addEventListener.bind(host),remove=host.removeEventListener.bind(host);
    host.addEventListener=(type,fn,options)=>{registration.push({type,fn,options});add(type,fn);};
    host.removeEventListener=(type,fn,options)=>{removal.push({type,fn,options});remove(type,fn);};
    runInNewContext(source,{window:host,document,location:host.location,URLSearchParams},{timeout:1000});
    return {host,document,media,posts,registration,removal,loading};
  }
  function initial(f,hidden){
    assert.deepEqual(f.posts,[{origin:f.host.location.origin,packet:{type:'vira-aquarium',action:'progress',stage:'module-loading',engine:'unknown',hidden}}]);
    assert.equal(f.document.documentElement.dataset.aquariumStage,'module-loading');
    assert.equal(f.registration.find(record=>record.type==='error')?.options,true,'module resource errors require capture');
  }
  function failed(f,event,type='error'){
    const handler=f.registration.find(record=>record.type===type).fn;f.host.emit(type,event);
    assert.equal(f.posts.length,2);assert.equal(f.posts[1].origin,f.host.location.origin);const packet=f.posts[1].packet;
    assert.equal(packet.type,'vira-aquarium');assert.equal(packet.action,'error');assert.deepEqual(Object.keys(packet).sort(),['action','message','type']);assert.ok(packet.message.length>0&&packet.message.length<=240);assert.ok(!/[\x00-\x1f\x7f]/.test(packet.message));
    assert.equal(f.document.documentElement.dataset.aquariumError,packet.message);assert.equal(f.loading.textContent,`Aquarium unavailable: ${packet.message}`);
    assert.equal(f.host.listenerCount,0);assert.equal(f.host.__viraAquariumStartup,undefined);
    assert.equal(f.removal.find(record=>record.type==='error')?.options,true,'cleanup must match capture registration');
    f.host.emit(type,event);handler(event);assert.equal(f.posts.length,2,'terminal reporting ignores queued and repeated failures');
    return packet.message;
  }
  const module=startup({hidden:true});initial(module,true);module.host.emit('error',{target:{id:'unrelated-image'}});assert.equal(module.posts.length,1);
  assert.equal(failed(module,{target:{id:'aquarium-module'}}),'Aquarium modules could not load');
  const windowError=startup();initial(windowError,false);const message=failed(windowError,{target:windowError.host,message:'GPU\nstartup\u0000failed\u007f '+'.'.repeat(300)});assert.equal(message.length,240);assert.ok(message.startsWith('GPU startup failed  '));
  const rejection=startup();initial(rejection,false);assert.equal(failed(rejection,{reason:new Error('Import\tfailed\rnow')},'unhandledrejection'),'Import failed now');
  const longRejection=startup();assert.equal(failed(longRejection,{reason:'Rejected\n'+'.'.repeat(300)},'unhandledrejection').length,240);
  const departed=startup(),lateError=departed.registration.find(record=>record.type==='error').fn;
  departed.host.emit('pagehide');assert.equal(departed.host.listenerCount,0);lateError({target:{id:'aquarium-module'}});departed.host.emit('unhandledrejection',{reason:'late'});assert.equal(departed.posts.length,1);assert.equal(departed.host.__viraAquariumStartup,undefined);
  const handoff=startup();handoff.host.__viraAquariumStartup.dispose();assert.equal(handoff.host.listenerCount,0);
  const embed=createAquariumEmbed({host:handoff.host});assert.equal(embed.progress('module-ready'),true);assert.equal(embed.ready({model:'refined',engine:'webgpu'}),true);handoff.host.emit('error',{target:{id:'aquarium-module'}});
  assert.deepEqual(handoff.posts.map(post=>post.packet.action),['progress','progress','ready'],'the runtime helper owns reporting after explicit bootstrap disposal');embed.dispose();
  const standalone=startup({search:'?research=1'});assert.deepEqual(standalone.posts,[]);assert.equal(standalone.host.listenerCount,0);assert.equal(standalone.host.__viraAquariumStartup,undefined);assert.deepEqual(standalone.document.documentElement.dataset,{});
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
  const redrawThrow=fixture({onActivity:value=>{if(!value)throw new Error('secondary still redraw failure');}});
  redrawThrow.send('configure',{motion:true,look:'natural'});assert.equal(redrawThrow.embed.ready(),true);assert.equal(redrawThrow.embed.active,true);
  assert.equal(redrawThrow.embed.error('original render failure'),true,'an activity callback must not interrupt the original terminal report');assert.equal(redrawThrow.embed.active,false);assert.deepEqual(redrawThrow.calls.activity,[true,false]);assert.deepEqual(redrawThrow.posts.map(post=>post.packet.action),['ready','error']);assert.equal(redrawThrow.posts.at(-1).packet.message,'original render failure');assert.equal(redrawThrow.embed.error('late'),false);redrawThrow.embed.dispose();
  const startThrow=fixture({onActivity:value=>{throw new Error(value?'initial activity failure':'secondary stop failure');}});
  startThrow.send('configure',{motion:true,look:'natural'});assert.equal(startThrow.embed.ready(),false);assert.equal(startThrow.embed.active,false);assert.deepEqual(startThrow.calls.activity,[true,false]);assert.deepEqual(startThrow.posts.map(post=>post.packet.action),['error']);assert.equal(startThrow.posts[0].packet.message,'initial activity failure','recursive stop failure cannot replace the first failure');assert.equal(startThrow.embed.ready(),false);startThrow.embed.dispose();
  const disposeThrow=fixture({onActivity:value=>{if(!value)throw new Error('teardown redraw failure');}});
  disposeThrow.send('configure',{motion:true,look:'natural'});assert.equal(disposeThrow.embed.ready(),true);assert.doesNotThrow(()=>disposeThrow.embed.dispose());assert.equal(disposeThrow.embed.active,false);assert.equal(disposeThrow.host.listenerCount+disposeThrow.document.listenerCount+disposeThrow.media.listenerCount,0);assert.deepEqual(disposeThrow.posts.map(post=>post.packet.action),['ready'],'disposing cannot emit a late failure');assert.doesNotThrow(()=>disposeThrow.embed.dispose());
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
assertMessaging();assertPointerClear();assertProgress();assertVisibilityStill();assertEarlyHTMLReporter();assertActivity();assertFailures();const cadence=assertCadence();assertPixels();
console.log(JSON.stringify({messaging:true,progress:true,visibilityStill:true,earlyHTMLReporter:true,activity:true,terminalErrors:true,cleanup:true,cadence,pixelBudget:true}));
