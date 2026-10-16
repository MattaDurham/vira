/* Same-origin parent controls for the aquarium background. Importing this
 * module does not register listeners or create a renderer. */
const TYPE='vira-aquarium';
const looks=new Set(['natural','wireframe']);
const stages=new Set(['module-loading','module-ready','data','renderer','textures','model','rig','compile','optics','first-frame','ready']);
const engines=new Set(['unknown','webgpu','webgl2']);
const normalized=value=>Number.isFinite(value)&&value>=0&&value<=1;

export function createAquariumEmbed({host=window,onConfigure=()=>{},onPointer=()=>{},onTap=()=>{},onActivity=()=>{},onVisible=()=>{}}={}){
  const origin=host.location.origin,document=host.document;
  const reduced=host.matchMedia('(prefers-reduced-motion: reduce)');
  let configuration={motion:false,look:'natural'},isReady=false,failed=false,closed=false,active=false;
  const copy=()=>({...configuration});
  function activity(){
    const next=!closed&&!failed&&isReady&&configuration.motion&&!document.hidden&&!reduced.matches;
    if(next===active)return;
    active=next;
    try{onActivity(next);}catch(error){fail(error?.message??String(error));}
  }
  function post(action,detail){host.parent.postMessage({type:TYPE,action,...detail},origin);}
  function fail(message){
    if(closed||failed)return false;
    failed=true;isReady=false;activity();
    post('error',{message:String(message??'Aquarium unavailable').slice(0,240)});return true;
  }
  function configure(){
    try{onConfigure(copy());return true;}
    catch(error){fail(error?.message??String(error));return false;}
  }
  function visibility(){
    activity();
    if(isReady&&!closed&&!failed&&!document.hidden){
      try{onVisible();}catch(error){fail(error?.message??String(error));}
    }
  }
  function message(event){
    if(closed||failed||event.origin!==origin||event.source!==host.parent)return;
    const data=event.data;
    if(!data||typeof data!=='object'||Array.isArray(data)||data.type!==TYPE)return;
    if(data.action==='configure'){
      // Parent packets describe the complete preference state. Partial or
      // malformed packets must not accidentally resume a paused background.
      if(typeof data.motion!=='boolean'||!looks.has(data.look))return;
      if(data.motion===configuration.motion&&data.look===configuration.look)return;
      configuration={motion:data.motion,look:data.look};
      if(isReady&&!configure())return;
      activity();return;
    }
    // Events before asset readiness are dropped, never replayed as old input.
    if(!isReady)return;
    if(data.action==='pointer'){
      if(data.x===null&&data.y===null)onPointer(null);
      else if(normalized(data.x)&&normalized(data.y))onPointer({x:data.x,y:data.y});
    }else if(data.action==='tap'&&normalized(data.x)&&normalized(data.y))onTap({x:data.x,y:data.y});
  }
  host.addEventListener('message',message);
  document.addEventListener('visibilitychange',visibility);
  reduced.addEventListener('change',activity);
  return {
    get active(){return active;},
    get configuration(){return copy();},
    progress(stage,detail={}){
      if(closed||failed||!stages.has(stage)||!detail||typeof detail!=='object'||Array.isArray(detail))return false;
      const engine=detail.engine===undefined?'unknown':detail.engine;
      if(!engines.has(engine))return false;
      post('progress',{stage,engine,hidden:document.hidden===true});return true;
    },
    ready({model,engine}={}){
      if(closed||failed||isReady)return false;
      isReady=true;if(!configure())return false;
      // A renderer callback may fail or dispose synchronously during setup.
      if(closed||failed)return false;
      activity();
      if(closed||failed)return false;
      post('ready',{model,engine});return true;
    },
    error:fail,
    dispose(){
      if(closed)return;
      closed=true;isReady=false;
      host.removeEventListener('message',message);
      document.removeEventListener('visibilitychange',visibility);
      reduced.removeEventListener('change',activity);
      activity();
    },
  };
}

/** Render cadence only. elapsed is seconds between accepted timestamps; it
 * includes skipped callbacks so simulation time is independent of redraw Hz.
 * A rewind starts a new epoch, as does reset() after a pause or resume. */
export function createFrameCadence(fps=30){
  const interval=1000/fps;
  if(!Number.isFinite(fps)||fps<=0||!Number.isFinite(interval)||interval<=0)throw new RangeError('A positive finite frame rate is required');
  let next=null,lastSeen=null,lastAccepted=null,elapsed=0;
  function reset(){next=lastSeen=lastAccepted=null;elapsed=0;}
  return {
    get elapsed(){return elapsed;},
    reset,
    accept(now){
      if(!Number.isFinite(now))return false;
      if(lastSeen!==null&&now<lastSeen)reset();
      lastSeen=now;
      if(lastAccepted===now)return false;
      const tolerance=Math.max(interval*1e-7,Math.abs(now)*Number.EPSILON*8);
      if(next!==null&&now+tolerance<next)return false;
      elapsed=lastAccepted===null?0:(now-lastAccepted)/1000;lastAccepted=now;
      if(next===null)next=now+interval;
      else next+=(Math.floor(Math.max(0,now-next+tolerance)/interval)+1)*interval;
      return true;
    },
  };
}

/** Three.js floors physical framebuffer dimensions after applying this DPR. */
export function backgroundPixelRatio(width,height,dpr,maxPixels=4194304){
  if(!Number.isFinite(width)||!Number.isFinite(height)||width<=0||height<=0)return 1;
  const density=Number.isFinite(dpr)&&dpr>0?Math.min(dpr,2):1;
  const budget=Number.isFinite(maxPixels)&&maxPixels>0?Math.max(1,maxPixels):4194304;
  // Separate square roots avoid overflowing width*height on finite inputs.
  const budgetRatio=Math.sqrt(budget)/Math.sqrt(width)/Math.sqrt(height);
  return Math.max(Number.MIN_VALUE,Math.min(density,budgetRatio,Number.MAX_VALUE/width,Number.MAX_VALUE/height));
}
