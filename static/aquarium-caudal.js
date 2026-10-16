/* Intrinsic caudal shape control, shared by the membrane and embedded rays.
 * Bluegill observations motivate these modes, not these authored koi values:
 * Flammang & Lauder (2009), doi:10.1242/jeb.021360. This field adds no force. */
const clamp=(value,low,high)=>Math.max(low,Math.min(high,value));
const smooth=value=>value*value*(3-2*value);

export function createCaudalFrame(base,uv){
  let rootX=0,rootY=0,rootZ=0,count=0,maxX=-Infinity,top=-Infinity,bottom=Infinity;
  for(let i=0;i<base.length/3;i++){
    const x=base[i*3],y=base[i*3+1];
    maxX=Math.max(maxX,x);top=Math.max(top,y);bottom=Math.min(bottom,y);
    // The membrane's radial v=1 edge is the complete basal attachment line.
    if(uv[i*2+1]<.999)continue;
    rootX+=x;rootY+=y;rootZ+=base[i*3+2];count++;
  }
  if(count<2)throw new Error('Caudal membrane has no basal attachment line');
  const root={x:rootX/count,y:rootY/count,z:rootZ/count};
  if(!(maxX>root.x&&top>root.y&&bottom<root.y))throw new Error('Caudal fan has no distal lobes');
  return {root,length:maxX-root.x,upper:top-root.y,lower:root.y-bottom};
}

export function createCaudalMotion(fish,output={}){
  const speed=Math.abs(fish.forwardSpeed??fish.speed??0);
  const activation=clamp(fish.poseHistory===false?fish.stroke??0:fish.muscle3??fish.stroke??0,0,1);
  const recruitment=fish.poseHistory===false?0:smooth(clamp((fish.turnRecruitment3??0)/.25,0,1));
  const flow=smooth(clamp(speed/.14,0,1));
  const braking=clamp(fish.brake??0,0,1)*smooth(clamp((speed-.015)/.095,0,1));
  // A coasting tail narrows without folding shut. Braking instead opens the
  // fan and curves its lobes in opposite directions around the central fork.
  // All magnitudes are restrained design values, not bluegill-to-koi scaling.
  return Object.assign(output,{adduction:.19*flow*(1-activation)*(1-braking),
    braking,amplitude:.011*activation,carrier:1-recruitment,phase:fish.phase??0});
}

export function createCaudalController(){
  const names=['adduction','braking','amplitude'],rate=14;
  let age,poseKey,motion,target;
  const velocity={adduction:0,braking:0,amplitude:0};
  return {sample(fish,key=fish.poseHistory===false?'held':'live'){
    const now=fish.age??0;
    target=createCaudalMotion(fish,target);
    if(key!==poseKey||age===undefined||now<age||now-age>.25){
      motion={...target};for(const name of names)velocity[name]=0;
    }else if(now===age)return motion;
    else{
      // Exact critically damped response to the newly observed target. This
      // keeps wall braking and finite kick/glide transitions from snapping
      // the tissue, without inventing earlier commands or elapsed motion.
      const dt=now-age,decay=Math.exp(-rate*dt);
      for(const name of names){
        const error=motion[name]-target[name],b=velocity[name]+rate*error;
        motion[name]=target[name]+(error+b*dt)*decay;
        velocity[name]=(velocity[name]-rate*b*dt)*decay;
      }
      motion.phase=target.phase;
      // The same smooth recruitment envelope used by the axial field acts
      // after compliance, so stored tail loading cannot oppose a loaded turn.
      motion.carrier=target.carrier;
    }
    age=now;poseKey=key;
    return motion;
  }};
}

function coordinates(x,y,z,frame,output,offset=0){
  const s=clamp((x-frame.root.x)/frame.length,0,1);
  const lobe=(y-frame.root.y)/(y>=frame.root.y?frame.upper:frame.lower);
  output[offset]=x;output[offset+1]=y;output[offset+2]=z;
  output[offset+3]=s;output[offset+4]=smooth(s);output[offset+5]=lobe;
}

function deform(values,offset,frame,motion,output,target){
  const x=values[offset],y=values[offset+1],z=values[offset+2];
  const s=values[offset+3],distal=values[offset+4],lobe=values[offset+5];
  const phase=motion.phase-.8*s;
  const amplitude=motion.amplitude*motion.carrier;
  const power=amplitude*distal*Math.sin(phase);
  const cup=.45*amplitude*distal*(1-clamp(lobe*lobe,0,1))*Math.sin(phase-.45);
  // A cubic span profile leaves the fork near the median while the dorsal
  // and ventral tips flare oppositely. The slight asymmetry is authored.
  const flare=motion.braking*distal*(lobe>=0?.019:.017)*lobe*lobe*lobe;
  output[target]=x;
  output[target+1]=frame.root.y+(y-frame.root.y)*(1-motion.adduction*distal);
  output[target+2]=z+power+cup+flare;
  // x is unchanged; y is a positive x-dependent scale; z is a shear in x,y.
  // This triangular map has det(J)=1-adduction*distal >= .81, including rays.
}

export function bindCaudalGeometry(base,frame){
  const count=base.length/3,values=new Float64Array(count*6);
  for(let i=0;i<count;i++)coordinates(base[i*3],base[i*3+1],base[i*3+2],frame,values,i*6);
  return {frame,values};
}

export function deformCaudalGeometry(binding,motion,output){
  for(let i=0;i<binding.values.length/6;i++)deform(binding.values,i*6,binding.frame,motion,output,i*3);
  return output;
}

export function deformCaudalPoint(point,frame,motion,output=[]){
  const values=new Float64Array(6);
  coordinates(point[0],point[1],point[2],frame,values);
  deform(values,0,frame,motion,output,0);
  return output;
}
