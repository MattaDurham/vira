// Observed-shape cue. Source head orientation/modeled COM are deliberately
// available only through reference(), never through the relative-shape sample() channels.
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const ease=t=>t*t*t*(10+t*(-15+6*t));
const easeD=t=>30*t*t*(1-t)*(1-t);
const easeDD=t=>60*t*(1-t)*(1-2*t);
function createSignal(frames,read){
 const times=frames.map(f=>f.time),values=frames.map(read),channels=values[0].length;
 const rates=values.map((row,i)=>row.map((_,j)=>{
  const a=Math.max(0,i-1),b=Math.min(values.length-1,i+1);return(values[b][j]-values[a][j])/(times[b]-times[a]);
 }));
 const accelerations=values.map((row,i)=>row.map((_,j)=>{
  const a=i===0?0:i===values.length-1?i-2:i-1,b=a+1,c=a+2,h1=times[b]-times[a],h2=times[c]-times[b];
  return 2*((values[c][j]-values[b][j])/h2-(values[b][j]-values[a][j])/h1)/(h1+h2);
 }));
 const out={p:Array(channels).fill(0),v:Array(channels).fill(0),a:Array(channels).fill(0)};
 return time=>{
  let left=0,right=times.length-1;
  while(right-left>1){const mid=(left+right)>>1;if(times[mid]<=time)left=mid;else right=mid;}
  const h=times[right]-times[left],s=clamp((time-times[left])/h,0,1);
  for(let j=0;j<channels;j++){
   const c0=values[left][j],c1=h*rates[left][j],c2=.5*h*h*accelerations[left][j];
   const P=values[right][j]-c0-c1-c2,V=h*rates[right][j]-c1-2*c2,A=h*h*accelerations[right][j]-2*c2;
   const c3=10*P-4*V+.5*A,c4=-15*P+7*V-A,c5=6*P-3*V+.5*A;
   out.p[j]=c0+s*(c1+s*(c2+s*(c3+s*(c4+s*c5))));
   out.v[j]=(c1+s*(2*c2+s*(3*c3+s*(4*c4+s*5*c5))))/h;
   out.a[j]=(2*c2+s*(6*c3+s*(12*c4+s*20*c5)))/(h*h);
  }
  return out;
 };
}
export function createObservedTurnCue(data,{trial='fast',duration,start=0,mirror=1}={}){
 const source=data.trials?.[trial];if(!source||source.frames.length<7)throw new Error('Observed source trial required');
 const frames=source.frames,first=frames[0].time,last=frames.at(-1).time,sourceDuration=last-first;
 duration??=sourceDuration;
 if(![duration,start,mirror].every(Number.isFinite)||duration<=0||Math.abs(mirror)!==1||!(first<0&&last>source.turnDuration))throw new Error('Finite duration/start and mirror±1, with source pre/post observations required');
 const scale=sourceDuration/duration,signal=createSignal(frames,f=>f.b),ref=createSignal(frames,f=>[f.headReference,f.tailReference,...f.authorCOMOverL]);
 const out={b:[0,0,0],v:[0,0,0],a:[0,0,0]},reference={sourceTime:first,head:0,tail:0,comOverL:[0,0],headRate:0,tailRate:0,comVelocityOverL:[0,0]};
 const metadata=Object.freeze({trial,ID:source.ID,species:source.species,sourceFullLengthCm:source.fullLengthCm,stimulusCarPeakBLs:source.stimulusPeakBLs,
  sourceTimeRange:[first,last],sourceTurnDuration:source.turnDuration,duration,start,markedOnset:start-first/scale,markedEnd:start+(source.turnDuration-first)/scale,end:start+duration,
  interpolation:'quintic Hermite with shared central finite-difference first/second derivatives at each60Hz knot; C2, no stop-start rates',
  boundary:'authored C2 multiplier on the five pre-turn/five post-turn observations only; marked turnframes preserved at all knots',
  retarget:data.retarget,source:data.provenance,shapeFit:source.bounds});
 function window(t){
  if(t<=first||t>=last)return[0,0,0];
  if(t<0){const h=-first,x=(t-first)/h;return[ease(x),easeD(x)/h,easeDD(x)/(h*h)];}
  if(t>source.turnDuration){const h=last-source.turnDuration,x=(t-source.turnDuration)/h;return[1-ease(x),-easeD(x)/h,-easeDD(x)/(h*h)];}
  return[1,0,0];
 }
 function sample(age){
  if(!Number.isFinite(age))throw new Error('Finite cue age required');
  const t=first+(age-start)*scale,[w,dw,ddw]=window(t),s=signal(clamp(t,first,last));
  for(let j=0;j<3;j++){
   out.b[j]=mirror*w*s.p[j];
   out.v[j]=mirror*scale*(w*s.v[j]+dw*s.p[j]);
   out.a[j]=mirror*scale*scale*(w*s.a[j]+2*dw*s.v[j]+ddw*s.p[j]);
  }
  return out;
 }
 function referenceAt(age){
  const t=clamp(first+(age-start)*scale,first,last),s=ref(t),rateScale=age<start||age>start+duration?0:scale;reference.sourceTime=t;
  reference.head=mirror*s.p[0];reference.tail=mirror*s.p[1];reference.headRate=mirror*rateScale*s.v[0];reference.tailRate=mirror*rateScale*s.v[1];
  // The source frame is posterior+X / transverseY. To compare in canonical
  // fish XZ, use [COM.x,0,-COM.y]; do not apply this to a physical body.
  reference.comOverL[0]=s.p[2];reference.comOverL[1]=mirror*s.p[3];reference.comVelocityOverL[0]=rateScale*s.v[2];reference.comVelocityOverL[1]=mirror*rateScale*s.v[3];
  return reference;
 }
 return{sample,reference:referenceAt,metadata};
}
