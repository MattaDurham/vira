/* Paired-fin tissue and its embedded rays share this bind-space deformation.
 * It preserves the authored attachment and skeleton; it does not add thrust. */
import {finActuation,finPhaseRate} from './aquarium-motion.js?v=80';

const TAU=Math.PI*2,tableSize=33,maxDelay=.24;
const clamp=(value,low,high)=>Math.max(low,Math.min(high,value));
const dot=(a,b)=>a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
const normalize=a=>{const length=Math.hypot(...a);return a.map(value=>value/length);};
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];

export function createFinFrame(base,uv){
  const root={x:base[0],y:base[1],z:base[2]},side=Math.sign(root.z)||1;
  const tips=[],mean=[0,0,0];let first,last;
  for(let i=0;i<base.length/3;i++){
    // glTF flips the authored radial UV: v=1 is the attachment, v=0 the edge.
    if(uv[i*2+1]>.001)continue;
    const delta=[base[i*3]-root.x,base[i*3+1]-root.y,(base[i*3+2]-root.z)*side];
    tips.push(delta);for(let axis=0;axis<3;axis++)mean[axis]+=delta[axis];
    if(uv[i*2]<.001)first=delta;
    if(uv[i*2]>.999)last=delta;
  }
  if(!tips.length||!first||!last)throw new Error('Paired fin has no radial boundary');
  const span=normalize(mean),difference=last.map((value,i)=>value-first[i]);
  const along=dot(difference,span),chord=normalize(difference.map((value,i)=>value-along*span[i]));
  const normal=cross(span,chord),length=Math.max(...tips.map(tip=>dot(tip,span)));
  const angles=tips.map(tip=>Math.atan2(dot(tip,chord),dot(tip,span)));
  return {root,side,span,chord,normal,length,angleMin:Math.min(...angles),angleMax:Math.max(...angles)};
}

function finMotion(fish,side,pectoral,samplePaddle,output){
  const age=fish.age??0,speed=Math.abs(fish.forwardSpeed??fish.speed??0);
  const phase=fish.finPhase??age*TAU*.65;
  const phaseRate=fish.finPhase===undefined?TAU*.65:finPhaseRate(fish);
  const base=finActuation(fish,side,pectoral),motion=output??{samples:new Float64Array(tableSize*2)};
  const {samples}=motion,delayed=samplePaddle?null:{...fish};
  for(let i=0;i<tableSize;i++){
    const delay=i/(tableSize-1)*maxDelay;
    let paddle;
    if(samplePaddle)paddle=samplePaddle(age-delay);
    else{
      delayed.age=age-delay;delayed.finPhase=phase-phaseRate*delay;
      paddle=finActuation(delayed,side,pectoral).paddle;
    }
    const loaded=Math.max(0,paddle)/(.06+Math.abs(paddle));
    const recovery=Math.max(0,-paddle)/(.025+Math.abs(paddle));
    // The distal sheet cups under loading and feathers back toward edge-on
    // during recovery. These bounds describe this authored rig, not koi data.
    samples[i*2]=(.010*loaded-.003*recovery)*(pectoral?1:.7);
    samples[i*2+1]=(.50*loaded-.08*recovery)*(pectoral?1:.7);
  }
  const flap=-(base.spread+base.paddle),trail=clamp(speed*.6,0,.22);
  // Flow bends the relaxed distal tissue a little. It settles with the stroke
  // envelope and speed, without a free-running decorative flutter.
  const activation=clamp(fish.poseHistory===false?fish.stroke??0:fish.muscle3??fish.stroke??0,0,1);
  const passive=.0015*clamp(speed/.25,0,1)*(.15+.85*activation)*(pectoral?1:.7);
  return Object.assign(motion,{passive,flap,trail,incidence:base.incidence??0,flapCos:Math.cos(flap),flapSin:Math.sin(flap),
    trailCos:Math.cos(trail),trailSin:Math.sin(trail)});
}

// Authored or frozen samples have no elapsed command history. Their travelling
// phase field remains useful for inspecting the complete power/recovery cycle.
export function createFinMotion(fish,side,pectoral=true,samplePaddle){
  return finMotion(fish,side,pectoral,samplePaddle);
}

export function createFinController(side,pectoral=true){
  // A fixed ring bounds four fins to 16 KiB of command history. It retains the
  // full .24 s response at up to 1,000 observations/s, above display refresh.
  const capacity=256,ages=new Float64Array(capacity),paddles=new Float64Array(capacity);
  let start=0,count=0,lastAge,lastPoseKey,motion;
  const index=i=>(start+i)%capacity;
  const readPaddle=age=>{
    if(!count||age<ages[start])return 0;
    let low=0,high=count-1;
    if(age>=ages[index(high)])return paddles[index(high)];
    while(high-low>1){
      const middle=(low+high)>>1;
      if(ages[index(middle)]<=age)low=middle;else high=middle;
    }
    const first=index(low),second=index(high);
    const fraction=(age-ages[first])/(ages[second]-ages[first]);
    return paddles[first]+(paddles[second]-paddles[first])*fraction;
  };
  return {sample(fish,poseKey=fish.poseHistory===false?'held':'live'){
    // Switching an inspection pose can change its controls while paused. Its
    // new command starts now; it cannot inherit the previous pose's history.
    if(poseKey!==lastPoseKey){start=count=0;lastAge=undefined;lastPoseKey=poseKey;}
    const age=fish.age??0;
    // An unchanged simulation age is a pause, not another observation. Gaps
    // longer than the response window contain unknown commands; seed anew.
    if(age===lastAge)return motion;
    if(lastAge!==undefined&&(age<lastAge||age-lastAge>maxDelay))start=count=0;
    if(count===capacity){start=(start+1)%capacity;count--;}
    const next=index(count++);ages[next]=age;
    paddles[next]=finActuation(fish,side,pectoral).paddle;
    lastAge=age;
    // Keep one observation before the window for interpolation at its edge.
    while(count>1&&ages[index(1)]<=age-maxDelay){start=(start+1)%capacity;count--;}
    motion=finMotion(fish,side,pectoral,readPaddle,motion);
    return motion;
  }};
}

function coordinates(x,y,z,frame,out,offset=0){
  const delta=[x-frame.root.x,y-frame.root.y,(z-frame.root.z)*frame.side];
  const a=dot(delta,frame.span),b=dot(delta,frame.chord),c=dot(delta,frame.normal);
  const s=clamp(a/frame.length,0,1);
  const angle=Math.atan2(b,Math.max(.001,a));
  const u=clamp((angle-frame.angleMin)/(frame.angleMax-frame.angleMin),0,1);
  out[offset]=a;out[offset+1]=b;out[offset+2]=c;
  out[offset+3]=s*s;
  out[offset+4]=Math.sin(Math.PI*u)**2*s*s;
  // Span compliance lags the base; trailing rays lag the leading rays too.
  out[offset+5]=(s*.12+u*.08)/maxDelay*(tableSize-1);
  out[offset+6]=s*.12/maxDelay*(tableSize-1);
}

function sample(samples,index,channel){
  const low=Math.floor(index),fraction=index-low;
  return samples[low*2+channel]*(1-fraction)+samples[Math.min(tableSize-1,low+1)*2+channel]*fraction;
}

function deform(values,offset,frame,motion,out,target){
  const a=values[offset],b=values[offset+1],c=values[offset+2];
  const cup=(sample(motion.samples,values[offset+5],0)+motion.passive)*values[offset+4];
  // Normal shear and a span-dependent twist are both invertible. Their
  // controls depend on a,b but never on the displaced normal coordinate c.
  // Signed basal feathering rotates the whole foil about its own span. The
  // shared command is already filtered by the swimmer's authored pitch model;
  // distal loading/recovery retains its separate observed-history delay.
  const twist=(motion.incidence??0)+sample(motion.samples,values[offset+6],1)*values[offset+3];
  const cosine=Math.cos(twist),sine=Math.sin(twist);
  const chord=b*cosine-(c+cup)*sine,normal=b*sine+(c+cup)*cosine;
  const x=a*frame.span[0]+chord*frame.chord[0]+normal*frame.normal[0];
  const y=a*frame.span[1]+chord*frame.chord[1]+normal*frame.normal[1];
  const z=a*frame.span[2]+chord*frame.chord[2]+normal*frame.normal[2];
  const raisedY=y*motion.flapCos-z*motion.flapSin;
  const raisedZ=y*motion.flapSin+z*motion.flapCos;
  out[target]=frame.root.x+x*motion.trailCos+raisedZ*motion.trailSin;
  out[target+1]=frame.root.y+raisedY;
  out[target+2]=frame.root.z+frame.side*(raisedZ*motion.trailCos-x*motion.trailSin);
}

export function bindFinGeometry(base,frames){
  const count=base.length/3,values=new Float64Array(count*7),sides=new Int8Array(count);
  for(let i=0;i<count;i++){
    const side=Math.sign(base[i*3+2])||1,frame=frames[side];
    if(!frame)throw new Error('Paired fin attachment is missing');
    sides[i]=side;coordinates(base[i*3],base[i*3+1],base[i*3+2],frame,values,i*7);
  }
  return {values,sides,frames};
}

export function deformFinGeometry(binding,motions,output){
  for(let i=0;i<binding.sides.length;i++){
    const side=binding.sides[i];
    deform(binding.values,i*7,binding.frames[side],motions[side],output,i*3);
  }
  return output;
}

export function deformFinPoint(point,frame,motion,output=[]){
  const values=new Float64Array(7);
  coordinates(point[0],point[1],point[2],frame,values);
  deform(values,0,frame,motion,output,0);
  return output;
}
