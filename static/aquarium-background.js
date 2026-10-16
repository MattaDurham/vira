/* The aquarium runs in its own document; the desktop retains focus, gestures
 * and every window. This adapter owns only the frame and its message bridge. */
export function create({node,canInteract,onMessage,look='natural',motion=false}){
  const document=node.ownerDocument,window=document.defaultView,origin=window.location.origin;
  const frame=document.createElement('iframe');
  frame.className='aquarium-background-frame';frame.title='Aquarium desktop background';
  frame.tabIndex=-1;frame.setAttribute('aria-hidden','true');frame.style.opacity='0';
  let closed=false,ready=false,attention=false;
  let requestedMotion=motion===true,currentLook=look==='wireframe'?'wireframe':'natural';
  const moving=()=>requestedMotion&&!document.hidden;
  const send=(action,fields={})=>{
    if(!closed&&ready)frame.contentWindow?.postMessage({type:'vira-aquarium',action,...fields},origin);
  };
  const clear=()=>{if(attention){send('pointer',{x:null,y:null});attention=false;}};
  const configure=()=>{if(!moving())clear();send('configure',{motion:moving(),look:currentLook});};
  const point=event=>{
    if(!canInteract?.(event.target)||event.target.closest?.('button,a,input,textarea,select,[contenteditable],#background-picker,.ctx-menu,.ctx-pop,#reminder-stickies'))return null;
    const bounds=frame.getBoundingClientRect();
    const x=(event.clientX-bounds.left)/bounds.width,y=(event.clientY-bounds.top)/bounds.height;
    return Number.isFinite(x)&&Number.isFinite(y)&&x>=0&&x<=1&&y>=0&&y<=1?{x,y}:null;
  };
  const pointer=event=>{
    const position=ready&&moving()?point(event):null;
    if(position){send('pointer',position);attention=true;}else clear();
  };
  const tap=event=>{
    if(!ready||!moving()||event.button!==0||event.defaultPrevented||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;
    const position=point(event);if(position)send('tap',position);
    // Keep the desktop click and double-click gestures intact.
  };
  const receive=event=>{
    if(closed||event.source!==frame.contentWindow||event.origin!==origin)return;
    const data=event.data;
    if(!data||typeof data!=='object'||Array.isArray(data)||data.type!=='vira-aquarium')return;
    if(data.action==='ready'){
      if(ready)return;
      ready=true;window.clearTimeout(timeout);frame.style.opacity='1';dispose.message='';configure();onMessage?.('');
    }else if(data.action==='error'&&typeof data.message==='string'&&data.message.length<=240){
      fail(data.message);
    }
  };
  const fail=reason=>{
    if(closed)return;
    const detail=reason.replace(/[\x00-\x1f\x7f]/g,' ').trim();
    dispose();dispose.look=null;
    dispose.message='Aquarium unavailable'+(detail?': '+detail:'')+'. Choose the background again to retry.';
    onMessage?.(dispose.message);
  };
  const failedNavigation=()=>fail('The aquarium could not load');
  const visibility=()=>configure();
  const dispose=()=>{
    if(closed)return;
    clear();closed=true;window.clearTimeout(timeout);
    window.removeEventListener('message',receive);window.removeEventListener('blur',clear);
    document.removeEventListener('visibilitychange',visibility);document.removeEventListener('pointermove',pointer);
    document.removeEventListener('pointerleave',clear);document.removeEventListener('click',tap);
    frame.removeEventListener('error',failedNavigation);frame.remove();
  };
  dispose.motion=enabled=>{if(closed||typeof enabled!=='boolean')return;requestedMotion=enabled;configure();};
  dispose.look=value=>{if(closed||!['natural','wireframe'].includes(value))return;currentLook=value;configure();};
  dispose.explore=()=>window.open('/aquarium-benchmark.html','_blank','noopener');
  dispose.message='Loading aquarium...';
  window.addEventListener('message',receive);window.addEventListener('blur',clear);
  document.addEventListener('visibilitychange',visibility);document.addEventListener('pointermove',pointer);
  document.addEventListener('pointerleave',clear);document.addEventListener('click',tap);
  frame.addEventListener('error',failedNavigation);
  // Large local assets and shader compilation get one minute. A failed frame
  // navigation must not leave the desktop permanently waiting for readiness.
  const timeout=window.setTimeout(()=>fail('The aquarium did not finish loading'),60000);
  frame.src='/aquarium-benchmark.html?background=1';node.appendChild(frame);
  return dispose;
}
