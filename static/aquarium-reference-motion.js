/* Source-retargeted reference ANIMATION. This prescribes root and body motion.
 * Nothing here supplies a force, motor servo or correction to free physics. */
import {Vector3,Quaternion} from './vendor/three.webgpu.js';
import {regionalTurnAngle} from './aquarium-turn.js';
import {createObservedTurnCue} from './aquarium-reference-turn-cue.js';

const clamp=(x,a,b)=>Math.max(a,Math.min(b,x)),Y=new Vector3(0,1,0);
const blank=n=>({p:Array(n).fill(0),v:Array(n).fill(0),a:Array(n).fill(0)});
const copy=s=>({p:[...s.p],v:[...s.v],a:[...s.a]});
const finiteVector=v=>v&&[v.x,v.y,v.z].every(Number.isFinite);
const finiteQ=q=>q&&[q.x,q.y,q.z,q.w].every(Number.isFinite)&&q.lengthSq()>1e-20;

function quintic(first,last,duration){
  return first.p.map((p,j)=>{
    const c0=p,c1=duration*first.v[j],c2=.5*duration*duration*first.a[j];
    const P=last.p[j]-c0-c1-c2,V=duration*last.v[j]-c1-2*c2,A=duration*duration*last.a[j]-2*c2;
    return [c0,c1,c2,10*P-4*V+.5*A,-15*P+7*V-A,6*P-3*V+.5*A];
  });
}
function evaluate(coefficients,time,duration,out){
  const s=clamp(time/duration,0,1);
  coefficients.forEach((c,j)=>{
    out.p[j]=c[0]+s*(c[1]+s*(c[2]+s*(c[3]+s*(c[4]+s*c[5]))));
    out.v[j]=(c[1]+s*(2*c[2]+s*(3*c[3]+s*(4*c[4]+s*5*c[5]))))/duration;
    out.a[j]=(2*c[2]+s*(6*c[3]+s*(12*c[4]+s*20*c[5])))/(duration*duration);
  });return out;
}
function signal(frames,read){
  const times=frames.map(f=>f.time),values=frames.map(f=>[...read(f)]),n=values.length;
  const state=values.map((p,i)=>({p,v:p.map((_,j)=>{
    if(i===0)return (-3*values[0][j]+4*values[1][j]-values[2][j])/(times[2]-times[0]);
    if(i===n-1)return (3*values[n-1][j]-4*values[n-2][j]+values[n-3][j])/(times[n-1]-times[n-3]);
    return (values[i+1][j]-values[i-1][j])/(times[i+1]-times[i-1]);
  }),a:p.map((_,j)=>{
    const a=i===0?0:i===n-1?i-2:i-1,b=a+1,c=a+2,h1=times[b]-times[a],h2=times[c]-times[b];
    return 2*((values[c][j]-values[b][j])/h2-(values[b][j]-values[a][j])/h1)/(h1+h2);
  })}));
  const coefficients=state.slice(0,-1).map((first,i)=>quintic(first,state[i+1],times[i+1]-times[i]));
  const scratch=blank(values[0].length);
  return time=>{
    let first=0,last=n-1;
    while(last-first>1){const mid=(first+last)>>1;if(times[mid]<=time)first=mid;else last=mid;}
    return evaluate(coefficients[first],time-times[first],times[last]-times[first],scratch);
  };
}

/** Bone P/Q and root channels use unscaled neutral-model metres in the clip
 * frame. A viewer's lane/instance scale applies to ALL positions and linear
 * rates exactly once. targetLength scales the source COM path, not the rig.
 * sourceFrameQuaternion is fixed for the entire clip, never current heading. */
export function createReferenceAnimationClip({data,sampler,profile,trial='fast',duration,start=0,mirror=1,
  targetLength=1.496383,entryDuration=.25,exitDuration=.35,anchorCOM=new Vector3(),
  sourceFrameQuaternion,initialHeadQuaternion}={}){
  const source=data?.trials?.[trial],frames=source?.frames;
  if(!frames||frames.length<7||sampler?.centers?.length!==34||sampler?.rest?.length!==34||profile?.bodies29?.length!==29)
    throw new Error('Reference animation requires fitted source, pristine 34-bone frame and public 29-region mass profile');
  const firstTime=frames[0].time,lastTime=frames.at(-1).time,sourceDuration=lastTime-firstTime;
  duration??=sourceDuration;
  if(![duration,start,mirror,targetLength,entryDuration,exitDuration].every(Number.isFinite)||duration<=0||targetLength<=0||
    entryDuration<=0||exitDuration<=0||Math.abs(mirror)!==1||!finiteVector(anchorCOM))throw new Error('Invalid finite reference-animation options');
  if(sourceFrameQuaternion&&initialHeadQuaternion)throw new Error('Choose fixed source frame OR first-observed-head alignment');
  if(sourceFrameQuaternion&&!finiteQ(sourceFrameQuaternion)||initialHeadQuaternion&&!finiteQ(initialHeadQuaternion))throw new Error('Invalid clip frame quaternion');
  const fixedFrame=sourceFrameQuaternion?.clone().normalize()??new Quaternion();
  if(initialHeadQuaternion)fixedFrame.copy(initialHeadQuaternion).normalize()
    .multiply(new Quaternion().setFromAxisAngle(Y,-mirror*frames[0].headReference));
  const frameY=Y.clone().applyQuaternion(fixedFrame),scale=sourceDuration/duration;
  const sourceStart=start+entryDuration,sourceEnd=sourceStart+duration,end=sourceEnd+exitDuration;
  const cue=createObservedTurnCue(data,{trial,duration,start:sourceStart,mirror});
  const rootSignal=signal(frames,f=>[mirror*f.headReference,f.authorCOMOverL[0]*targetLength,-mirror*f.authorCOMOverL[1]*targetLength]);
  const firstCOM=[frames[0].authorCOMOverL[0]*targetLength,-mirror*frames[0].authorCOMOverL[1]*targetLength];
  const rawRoot=blank(4),root=blank(4),temp=new Vector3();
  function observed(age,out=rawRoot){
    const t=clamp(firstTime+(age-sourceStart)*scale,firstTime,lastTime),s=rootSignal(t);
    out.p[0]=s.p[0];out.v[0]=s.v[0]*scale;out.a[0]=s.a[0]*scale*scale;
    temp.set(s.p[1]-firstCOM[0],0,s.p[2]-firstCOM[1]).applyQuaternion(fixedFrame).add(anchorCOM);
    out.p.splice(1,3,temp.x,temp.y,temp.z);
    temp.set(s.v[1],0,s.v[2]).multiplyScalar(scale).applyQuaternion(fixedFrame);out.v.splice(1,3,temp.x,temp.y,temp.z);
    temp.set(s.a[1],0,s.a[2]).multiplyScalar(scale*scale).applyQuaternion(fixedFrame);out.a.splice(1,3,temp.x,temp.y,temp.z);
    return out;
  }
  const first=copy(observed(sourceStart)),last=copy(observed(sourceEnd));
  const before=blank(4),after=blank(4);
  for(let j=0;j<4;j++){
    before.p[j]=first.p[j]-.5*entryDuration*first.v[j];
    after.p[j]=last.p[j]+.5*exitDuration*last.v[j];
  }
  const entry=quintic(before,first,entryDuration),exit=quintic(last,after,exitDuration);
  const local=sampler.createFrame(),restInverse=sampler.rest.map(q=>q.clone().invert());
  const stations=sampler.axial.map(x=>clamp((x-sampler.axial[5])/(sampler.axial.at(-1)-sampler.axial[5]),0,1));
  const velocities=Array.from({length:34},()=>new Vector3()),accelerations=Array.from({length:34},()=>new Vector3());
  const rates=new Float64Array(34),angularAccelerations=new Float64Array(34);
  const regions=profile.bodies29.map((region,i)=>{
    const panel=i===0?0:i+4;
    if(!Number.isFinite(region.mass)||region.mass<=0||region.com?.length!==3)throw new Error('Invalid fixed region mass/COM');
    return {panel,mass:region.mass,offset:new Vector3(...region.com).sub(sampler.centers[panel])};
  });
  const mass=regions.reduce((sum,r)=>sum+r.mass,0),c=new Vector3(),cd=new Vector3(),cdd=new Vector3();
  const R=new Quaternion(),head=new Quaternion(),d=new Vector3(),offset=new Vector3(),w=new Vector3(),alpha=new Vector3(),cross=new Vector3(),cross2=new Vector3();
  const moment=new Vector3(),velocity=new Vector3(),acceleration=new Vector3();
  const fish={age:0,phase:0,stroke:0,drive:0,muscle1:0,muscle2:0,muscle3:0,poseHistory:true,
    yawRate:0,pitchRate:0,bankRate:0,yawLag1:0,yawLag2:0,yawLag3:0,pitchLag1:0,pitchLag2:0,pitchLag3:0,
    bankLag1:0,bankLag2:0,bankLag3:0,torsionDrive:0};
  function createPacket(){
    const rootReference={comWorld:new Vector3(),comVelocityWorld:new Vector3(),comAccelerationWorld:new Vector3(),
      orientation:new Quaternion(),omegaWorld:new Vector3(),alphaWorld:new Vector3(),originWorld:new Vector3(),
      originVelocityWorld:new Vector3(),originAccelerationWorld:new Vector3(),
      posedLocalCOM:new Vector3(),posedLocalCOMVelocity:new Vector3(),posedLocalCOMAcceleration:new Vector3()};
    return {age:0,positions:sampler.centers.map(()=>new Vector3()),rotations:sampler.rest.map(()=>new Quaternion()),
      shape:{b:[0,0,0],v:[0,0,0],a:[0,0,0]},rootReference,headQuaternion:rootReference.orientation,
      source:{ID:source.ID,sourceTime:firstTime,measuredPhase:false,observedRoot:false,authoredBoundaryBlend:'held-before'}};
  }
  const packet=createPacket();
  function sample(age,out=packet){
    if(!Number.isFinite(age))throw new Error('Finite animation age required');
    let region;
    if(age<=start){for(const key of ['p','v','a'])root[key].splice(0,4,...before[key]);region='held-before';}
    else if(age<sourceStart){evaluate(entry,age-start,entryDuration,root);region='root-entry';}
    else if(age<=sourceEnd){observed(age,root);region='source';}
    else if(age<end){evaluate(exit,age-sourceEnd,exitDuration,root);region='root-exit';}
    else{for(const key of ['p','v','a'])root[key].splice(0,4,...after[key]);region='held-after';}
    const shape=cue.sample(age);
    for(const key of ['b','v','a'])for(let j=0;j<3;j++)out.shape[key][j]=shape[key][j];
    Object.assign(fish,{age,turnAnterior:shape.b[0],turnTrunk:shape.b[1],turnPosterior:shape.b[2]});
    sampler.capture(fish,local);
    velocities[0].set(0,0,0);accelerations[0].set(0,0,0);
    for(let i=0;i<34;i++){
      rates[i]=i<=4?0:regionalTurnAngle(stations[i],shape.v);
      angularAccelerations[i]=i<=4?0:regionalTurnAngle(stations[i],shape.a);
      if(i===0)continue;
      const parent=i-1;R.copy(local.rotations[parent]).multiply(restInverse[parent]);
      d.copy(sampler.centers[i]).sub(sampler.centers[parent]).applyQuaternion(R);
      w.set(0,rates[parent],0);alpha.set(0,angularAccelerations[parent],0);
      velocities[i].copy(velocities[parent]).add(cross.copy(w).cross(d));
      accelerations[i].copy(accelerations[parent]).add(cross.copy(alpha).cross(d))
        .add(cross2.copy(w).cross(cross.copy(w).cross(d)));
    }
    c.set(0,0,0);cd.set(0,0,0);cdd.set(0,0,0);
    for(const r of regions){
      R.copy(local.rotations[r.panel]).multiply(restInverse[r.panel]);offset.copy(r.offset).applyQuaternion(R);
      w.set(0,rates[r.panel],0);alpha.set(0,angularAccelerations[r.panel],0);
      moment.copy(local.positions[r.panel]).add(offset);
      velocity.copy(velocities[r.panel]).add(cross.copy(w).cross(offset));
      acceleration.copy(accelerations[r.panel]).add(cross.copy(alpha).cross(offset))
        .add(cross2.copy(w).cross(cross.copy(w).cross(offset)));
      c.addScaledVector(moment,r.mass);cd.addScaledVector(velocity,r.mass);cdd.addScaledVector(acceleration,r.mass);
    }
    c.divideScalar(mass);cd.divideScalar(mass);cdd.divideScalar(mass);
    const r=out.rootReference;
    head.copy(fixedFrame).multiply(R.setFromAxisAngle(Y,root.p[0])).normalize();r.orientation.copy(head);
    r.comWorld.set(root.p[1],root.p[2],root.p[3]);r.comVelocityWorld.set(root.v[1],root.v[2],root.v[3]);r.comAccelerationWorld.set(root.a[1],root.a[2],root.a[3]);
    r.omegaWorld.copy(frameY).multiplyScalar(root.v[0]);r.alphaWorld.copy(frameY).multiplyScalar(root.a[0]);
    r.posedLocalCOM.copy(c);r.posedLocalCOMVelocity.copy(cd);r.posedLocalCOMAcceleration.copy(cdd);
    r.originWorld.copy(r.comWorld).sub(moment.copy(c).applyQuaternion(head));
    w.set(0,root.v[0],0);alpha.set(0,root.a[0],0);
    velocity.copy(cd).add(cross.copy(w).cross(c)).applyQuaternion(head);
    acceleration.copy(cdd).add(cross.copy(w).cross(cd).multiplyScalar(2)).add(cross.copy(alpha).cross(c))
      .add(cross2.copy(w).cross(cross.copy(w).cross(c))).applyQuaternion(head);
    r.originVelocityWorld.copy(r.comVelocityWorld).sub(velocity);r.originAccelerationWorld.copy(r.comAccelerationWorld).sub(acceleration);
    for(let i=0;i<34;i++){
      out.positions[i].copy(local.positions[i]).applyQuaternion(head).add(r.originWorld);
      out.rotations[i].copy(head).multiply(local.rotations[i]).normalize();
    }
    out.age=age;
    const sourceTime=clamp(firstTime+(age-sourceStart)*scale,firstTime,lastTime);
    const markedTolerance=1e-10;
    Object.assign(out.source,{sourceTime,measuredPhase:region==='source'&&sourceTime>=-markedTolerance&&sourceTime<=source.turnDuration+markedTolerance,
      observedRoot:region==='source',authoredBoundaryBlend:region==='source'?(sourceTime<-markedTolerance?'shape-pre-margin':sourceTime>source.turnDuration+markedTolerance?'shape-post-margin':'none'):region});
    return out;
  }
  const metadata=Object.freeze({label:'Source-retargeted reference ANIMATION: bluegill head/body/modeled-COM applied to koi',
    physics:false,trial,ID:source.ID,sourceSpecies:source.species,sourceFullLengthMetres:source.fullLengthCm/100,
    targetLength,targetLengthDefinition:'Koi_skin rostrum to full Caudal_membrane tip; path units match unscaled native rig, not skin-only length',
    packetCoordinates:'Unscaled neutral-model metres in the clip/lane frame; apply display/lane scale to all positions and linear rates once',
    sourceStart,sourceEnd,start,end,duration,entryDuration,exitDuration,mirror,
    markedOnset:sourceStart-firstTime/scale,markedEnd:sourceStart+(source.turnDuration-firstTime)/scale,
    sourceTimeRange:[firstTime,lastTime],sourceMarkedDuration:source.turnDuration,
    markedHeadingDegrees:mirror*frames.find(f=>Math.abs(f.time-source.turnDuration)<1e-9).headReference*180/Math.PI,
    sourcePostHeadingDegrees:last.p[0]*180/Math.PI,authoredFinalHeadingDegrees:after.p[0]*180/Math.PI,
    boundary:'Authored quintic C2 rest entry/exit outside retained source; source body margin closure remains the unchanged cue',
    interpolation:'Shared central first/second derivatives at source knots; three-point one-sided endpoint estimates; quintic Hermite',
    comModel:'Same public29 fixed neutral-envelope regions, rigidly transported by actual posed bone transforms; modeled COM, not CT or equal vertex mass',
    totalNativeProxyMass:mass,source:data.provenance,retarget:data.retarget,shapeFit:source.bounds,
    finScope:'No measured fin strokes are reconstructed; viewer fin motion must remain explicitly authored',
  });
  return {sample,createPacket,metadata,cue,sampler,profile};
}
