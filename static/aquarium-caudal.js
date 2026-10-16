/* Intrinsic caudal shape control, shared by the membrane and embedded rays.
 * Bluegill observations motivate these modes, not these authored koi values:
 * Flammang & Lauder (2009), doi:10.1242/jeb.021360. Near-inextensible ray
 * bending follows Alben et al. (2007), doi:10.1098/rsif.2006.0181; real rays
 * retain finite elasticity. This authored reduced-order field adds no force. */
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
  const frame={root,length:maxX-root.x,upper:top-root.y,lower:root.y-bottom,
    base:Float64Array.from(base),paths:[],triangles:[]};
  const rows=new Map();
  for(let i=0;i<base.length/3;i++){
    const u=uv[i*2];if(!rows.has(u))rows.set(u,[]);
    rows.get(u).push({index:i,v:1-uv[i*2+1]});
  }
  for(const [,row] of [...rows].sort(([a],[b])=>a-b)){
    const indices=row.sort((a,b)=>a.v-b.v).map(item=>item.index);
    const first=indices[0],last=indices.at(-1),lengths=new Float64Array(indices.length-1);
    for(let j=1;j<indices.length;j++)lengths[j-1]=Math.hypot(...[0,1,2].map(axis=>base[indices[j]*3+axis]-base[indices[j-1]*3+axis]));
    frame.paths.push({indices,lengths,first,last});
  }
  const addTriangle=indices=>{
    const [a,b,c]=indices.map(i=>[base[i*3],base[i*3+1]]);
    const ux=b[0]-a[0],uy=b[1]-a[1],vx=c[0]-a[0],vy=c[1]-a[1],det=ux*vy-uy*vx;
    if(Math.abs(det)<1e-14)return;
    frame.triangles.push({indices,x:a[0],y:a[1],ux,uy,vx,vy,det,
      minX:Math.min(a[0],b[0],c[0]),maxX:Math.max(a[0],b[0],c[0]),
      minY:Math.min(a[1],b[1],c[1]),maxY:Math.max(a[1],b[1],c[1])});
  };
  for(let i=1;i<frame.paths.length;i++){
    const left=frame.paths[i-1].indices,right=frame.paths[i].indices;
    if(left.length!==right.length)throw new Error('Caudal radial sampling is inconsistent');
    for(let j=1;j<left.length;j++){
      addTriangle([left[j-1],right[j-1],right[j]]);
      addTriangle([left[j-1],right[j],left[j]]);
    }
  }
  if(frame.paths.length<3||!frame.triangles.length)throw new Error('Caudal radial tissue atlas is incomplete');
  if(frame.triangles.some(triangle=>triangle.det<=0))throw new Error('Caudal material atlas must have a consistent planar orientation');
  return frame;
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

function lateralOffset(x,y,frame,motion){
  const s=clamp((x-frame.root.x)/frame.length,0,1);
  const lobe=(y-frame.root.y)/(y>=frame.root.y?frame.upper:frame.lower);
  const distal=smooth(s);
  const phase=motion.phase-.8*s;
  const amplitude=motion.amplitude*motion.carrier;
  const power=amplitude*distal*Math.sin(phase);
  const cup=.45*amplitude*distal*(1-clamp(lobe*lobe,0,1))*Math.sin(phase-.45);
  // A cubic span profile leaves the fork near the median while the dorsal
  // and ventral tips flare oppositely. The slight asymmetry is authored.
  const flare=motion.braking*distal*(lobe>=0?.019:.017)*lobe*lobe*lobe;
  return power+cup+flare;
}

function tissue(frame,motion){
  const controls=[motion.adduction,motion.braking,motion.amplitude,motion.carrier,motion.phase];
  if(frame.controls&&controls.every((value,i)=>value===frame.controls[i]))return frame.deformed;
  frame.controls=controls;
  const base=frame.base,output=frame.deformed??=new Float64Array(base.length);
  for(const {indices,lengths,first,last} of frame.paths){
    const root=first*3,tip=last*3,dx=base[tip]-base[root],dy=base[tip+1]-base[root+1];
    const reach=Math.hypot(dx,dy),s=clamp((base[tip]-frame.root.x)/frame.length,0,1);
    const targetY=dy-motion.adduction*smooth(s)*(base[tip+1]-frame.root.y);
    // Adduction rotates the whole ray about its own basal attachment. The
    // distal fan closes by direction change, never by shortening its tissue.
    const angle=Math.asin(clamp(targetY/reach,-.99,.99))-Math.atan2(dy,dx);
    const cosine=Math.cos(angle),sine=Math.sin(angle);
    output.set(base.subarray(root,root+3),root);
    let previousOffset=lateralOffset(base[root],base[root+1],frame,motion);
    for(let j=1;j<indices.length;j++){
      const a=indices[j-1]*3,b=indices[j]*3;
      const x=base[b]-base[a],y=base[b+1]-base[a+1];
      const offset=lateralOffset(base[b],base[b+1],frame,motion);
      const z=base[b+2]-base[a+2]+offset-previousOffset;
      const length=lengths[j-1],normal=clamp(z/length,-.8,.8);
      // Integrate a unit tangent over each actual material segment. Segmented
      // ray bending changes its chord while every sampled arc segment keeps
      // its rest length. The bound leaves finite in-plane tangent everywhere.
      const inPlane=Math.sqrt(1-normal*normal)*length/Math.hypot(x,y);
      output[b]=output[a]+(x*cosine-y*sine)*inPlane;
      output[b+1]=output[a+1]+(x*sine+y*cosine)*inPlane;
      output[b+2]=output[a+2]+normal*length;
      previousOffset=offset;
    }
  }
  return output;
}

function coordinates(x,y,z,frame,values,offset,offsets,target){
  let nearest=Infinity,chosen,weights;
  for(const triangle of frame.triangles){
    const dx=Math.max(triangle.minX-x,0,x-triangle.maxX),dy=Math.max(triangle.minY-y,0,y-triangle.maxY);
    if(dx*dx+dy*dy>nearest)continue;
    const px=x-triangle.x,py=y-triangle.y;
    const b=(px*triangle.vy-py*triangle.vx)/triangle.det,c=(triangle.ux*py-triangle.uy*px)/triangle.det;
    if(b>=-1e-10&&c>=-1e-10&&b+c<=1+1e-10){chosen=triangle;weights=[1-b-c,b,c];break;}
    // Ray tubes extend slightly outside the membrane edge. Continue its
    // nearest displacement rather than clamping their physical coordinates.
    for(const [a,b] of [[0,1],[1,2],[2,0]]){
      const i=triangle.indices[a]*3,j=triangle.indices[b]*3;
      const ux=frame.base[j]-frame.base[i],uy=frame.base[j+1]-frame.base[i+1];
      const t=clamp(((x-frame.base[i])*ux+(y-frame.base[i+1])*uy)/(ux*ux+uy*uy),0,1);
      const squared=(x-frame.base[i]-t*ux)**2+(y-frame.base[i+1]-t*uy)**2;
      if(squared>=nearest)continue;
      nearest=squared;chosen=triangle;weights=[0,0,0];weights[a]=1-t;weights[b]=t;
    }
  }
  if(!chosen)throw new Error('Caudal point has no tissue attachment');
  for(let j=0;j<3;j++){values[offset+j]=chosen.indices[j];values[offset+3+j]=weights[j];}
  for(let axis=0;axis<3;axis++){
    const surface=chosen.indices.reduce((sum,index,j)=>sum+frame.base[index*3+axis]*weights[j],0);
    offsets[target+axis]=[x,y,z][axis]-surface;
  }
}

function deform(binding,motion,output){
  const source=tissue(binding.frame,motion),values=binding.values;
  for(let i=0;i<values.length/6;i++)for(let axis=0;axis<3;axis++){
    let value=binding.offsets[i*3+axis];
    for(let j=0;j<3;j++)value+=source[values[i*6+j]*3+axis]*values[i*6+3+j];
    output[i*3+axis]=value;
  }
  return output;
}

export function bindCaudalGeometry(base,frame){
  const count=base.length/3,values=new Float64Array(count*6),offsets=new Float64Array(count*3);
  for(let i=0;i<count;i++)coordinates(base[i*3],base[i*3+1],base[i*3+2],frame,values,i*6,offsets,i*3);
  return {frame,values,offsets};
}

export function deformCaudalGeometry(binding,motion,output){
  return deform(binding,motion,output);
}

export function deformCaudalPoint(point,frame,motion,output=[]){
  return deform(bindCaudalGeometry(point,frame),motion,output);
}
