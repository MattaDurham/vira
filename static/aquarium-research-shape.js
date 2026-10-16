import {Matrix4,Quaternion,Vector3} from './vendor/three.webgpu.js';

const stationCount=101,wrap=angle=>Math.atan2(Math.sin(angle),Math.cos(angle));
const vectors=count=>Array.from({length:count},()=>new Vector3());
const axis=()=>({origin:new Vector3(),direction:new Vector3(),start:new Vector3(),end:new Vector3()});
const tips=()=>({upper:new Vector3(),lower:new Vector3(),midpoint:new Vector3()});
const frame=count=>({snout:new Vector3(),body:vectors(count),curve:vectors(stationCount),
  bodyCurve:vectors(stationCount),tips:tips(),feet:tips(),projectedTips:tips(),axis:axis()});

// Observe the shipped surface, not the rig's guessed centerline. Construct once
// after loading the neutral asset, BEFORE any intrinsic geometry deformation.
// sample() follows current skin palettes and caudal positions without posing or
// editing the fish. Its reused vectors/objects are valid until the next sample.
// Head coordinates remove the actual skull attitude (including its rest arch),
// with the live snout as origin. Diagram y is head-local Z; distances are in that
// horizontal plane, not camera pixels or 3D tip-to-axis distances.
export function createResearchShape(asset){
  const body=asset.getObjectByName('Koi_skin'),tail=asset.getObjectByName('Caudal_membrane');
  const head=asset.getObjectByName('spine_01');
  if(!body?.isSkinnedMesh||!tail?.isSkinnedMesh||!head?.isBone)throw new Error('Research shape needs the exported skin, caudal membrane and skull frame');
  asset.updateMatrixWorld(true);
  const assetInverse=new Matrix4().copy(asset.matrixWorld).invert();
  const restHead=head.getWorldQuaternion(new Quaternion()).premultiply(asset.getWorldQuaternion(new Quaternion()).invert());
  const meshes=[];asset.traverse(mesh=>{if(mesh.isSkinnedMesh&&!mesh.name.startsWith('Anatomy')&&!/barbel/i.test(mesh.name))meshes.push(mesh);});
  const skeletons=[...new Set(meshes.map(mesh=>mesh.skeleton))];
  skeletons.forEach(skeleton=>skeleton.update());
  const transforms=new Map(meshes.map(mesh=>[mesh,new Matrix4().multiplyMatrices(mesh.matrixWorld,mesh.bindMatrixInverse)]));
  const bindPoint=new Vector3(),temporary=new Vector3();
  function vertex(mesh,index,out){
    const attrs=mesh.geometry.attributes,palette=mesh.skeleton.boneMatrices;
    bindPoint.fromBufferAttribute(attrs.position,index).applyMatrix4(mesh.bindMatrix);out.set(0,0,0);
    for(let slot=0;slot<4;slot++){
      const weight=attrs.skinWeight.getComponent(index,slot);if(!weight)continue;
      const e=attrs.skinIndex.getComponent(index,slot)*16;
      out.x+=weight*(palette[e]*bindPoint.x+palette[e+4]*bindPoint.y+palette[e+8]*bindPoint.z+palette[e+12]);
      out.y+=weight*(palette[e+1]*bindPoint.x+palette[e+5]*bindPoint.y+palette[e+9]*bindPoint.z+palette[e+13]);
      out.z+=weight*(palette[e+2]*bindPoint.x+palette[e+6]*bindPoint.y+palette[e+10]*bindPoint.z+palette[e+14]);
    }
    return out.applyMatrix4(transforms.get(mesh));
  }
  let snoutX=Infinity,tailX=-Infinity,snoutSamples=[];
  for(const mesh of meshes)for(let i=0;i<mesh.geometry.attributes.position.count;i++){
    vertex(mesh,i,temporary).applyMatrix4(assetInverse);
    if(temporary.x<snoutX-1e-7){snoutX=temporary.x;snoutSamples=[{mesh,index:i}];}
    else if(Math.abs(temporary.x-snoutX)<1e-7)snoutSamples.push({mesh,index:i});
    if(mesh.name.startsWith('Caudal'))tailX=Math.max(tailX,temporary.x);
  }
  const length=tailX-snoutX;
  if(!(length>0))throw new Error('Research shape has no neutral snout-to-caudal extent');

  // UV/normal seams can repeat one material position many times. Deduplicate
  // exact rest XYZ before averaging, so draw-call tessellation is not anatomy.
  const attrs=body.geometry.attributes,ringMap=new Map();
  for(let i=0;i<attrs.position.count;i++){
    const p=[attrs.position.getX(i),attrs.position.getY(i),attrs.position.getZ(i)];
    if(!ringMap.has(p[0]))ringMap.set(p[0],new Map());
    ringMap.get(p[0]).set(p.join(','),i);
  }
  const rings=[...ringMap].sort(([x],[y])=>x-y).map(([,unique])=>{
    const ids=[...unique.values()],weights=new Map();
    for(const index of ids){
      bindPoint.fromBufferAttribute(attrs.position,index).applyMatrix4(body.bindMatrix);
      for(let slot=0;slot<4;slot++){
        const weight=attrs.skinWeight.getComponent(index,slot)/ids.length;if(!weight)continue;
        const id=attrs.skinIndex.getComponent(index,slot);if(!weights.has(id))weights.set(id,[0,0,0,0]);
        const q=weights.get(id);q[0]+=weight*bindPoint.x;q[1]+=weight*bindPoint.y;q[2]+=weight*bindPoint.z;q[3]+=weight;
      }
    }
    return [...weights];
  });
  const tailAttrs=tail.geometry.attributes,tipIndices={};
  for(const [name,sign] of [['upper',1],['lower',-1]]){
    let extent=-Infinity,ids=[];
    for(let i=0;i<tailAttrs.position.count;i++){
      if(Math.sign(tailAttrs.position.getY(i))!==sign)continue;
      const x=tailAttrs.position.getX(i);
      if(x>extent+1e-7){extent=x;ids=[i];}else if(Math.abs(x-extent)<1e-7)ids.push(i);
    }
    if(!ids.length)throw new Error('Research shape has no caudal lobe');tipIndices[name]=ids;
  }
  const world=frame(rings.length),local=frame(rings.length),orientation=new Quaternion(),inverseOrientation=new Quaternion();
  const diagram={curve:Array.from({length:stationCount},()=>({x:0,y:0})),
    bodyCurve:Array.from({length:stationCount},()=>({x:0,y:0})),axis:{start:{x:0,y:0},end:{x:0,y:0}},
    tips:{upper:{x:0,y:0},lower:{x:0,y:0},midpoint:{x:0,y:0}},feet:{upper:{x:0,y:0},lower:{x:0,y:0},midpoint:{x:0,y:0}}};
  const result={length,ringCount:rings.length,world,head:local,diagram,
    dOverL:{upper:0,lower:0,midpoint:0},maximumDOverL:0,coherence:null,headTailAngle:0,chordShortening:0};
  function observe(){
    asset.updateMatrixWorld(true);skeletons.forEach(skeleton=>skeleton.update());
    for(const mesh of meshes)transforms.get(mesh).multiplyMatrices(mesh.matrixWorld,mesh.bindMatrixInverse);
    const palette=body.skeleton.boneMatrices,post=transforms.get(body);
    rings.forEach((weights,i)=>{
      const out=world.body[i];out.set(0,0,0);
      for(const [id,q] of weights){const e=id*16;
        out.x+=palette[e]*q[0]+palette[e+4]*q[1]+palette[e+8]*q[2]+palette[e+12]*q[3];
        out.y+=palette[e+1]*q[0]+palette[e+5]*q[1]+palette[e+9]*q[2]+palette[e+13]*q[3];
        out.z+=palette[e+2]*q[0]+palette[e+6]*q[1]+palette[e+10]*q[2]+palette[e+14]*q[3];
      }
      out.applyMatrix4(post);
    });
    world.snout.set(0,0,0);for(const {mesh,index} of snoutSamples)world.snout.addScaledVector(vertex(mesh,index,temporary),1/snoutSamples.length);
    for(const name of ['upper','lower']){const out=world.tips[name];out.set(0,0,0);for(const index of tipIndices[name])out.addScaledVector(vertex(tail,index,temporary),1/tipIndices[name].length);}
    world.tips.midpoint.copy(world.tips.upper).add(world.tips.lower).multiplyScalar(.5);
    orientation.copy(head.getWorldQuaternion(orientation)).multiply(restHead.clone().invert()).normalize();inverseOrientation.copy(orientation).invert();
    local.snout.set(0,0,0);
    world.body.forEach((point,i)=>local.body[i].copy(point).sub(world.snout).applyQuaternion(inverseOrientation));
    for(const name of Object.keys(local.tips))local.tips[name].copy(world.tips[name]).sub(world.snout).applyQuaternion(inverseOrientation);
  }
  observe();
  const bodyPoints=[local.snout,...local.body],fullPoints=[...bodyPoints,local.tips.midpoint];
  // Station labels remain attached to neutral material even when the live body
  // curves. The nasal region's dense rings therefore cannot bias the head fit.
  const labels=points=>{let distance=0;return points.map((point,i)=>{if(i)distance+=point.distanceTo(points[i-1]);return distance;});};
  const bodyLabels=labels(bodyPoints),fullLabels=labels(fullPoints);
  function resample(points,materialLabels,out){
    let index=0;const total=materialLabels.at(-1);
    out.forEach((point,i)=>{const station=i/(out.length-1)*total;while(index<points.length-2&&materialLabels[index+1]<station)index++;
      const span=materialLabels[index+1]-materialLabels[index];point.copy(points[index]).lerp(points[index+1],span?(station-materialLabels[index])/span:0);
    });
  }
  const toWorld=(point,out)=>out.copy(point).applyQuaternion(orientation).add(world.snout);
  function sample(){
    observe();resample(bodyPoints,bodyLabels,local.bodyCurve);resample(fullPoints,fullLabels,local.curve);
    const anterior=local.curve.slice(0,51);let x=0,z=0;
    for(const point of anterior){x+=point.x/anterior.length;z+=point.z/anterior.length;}
    let xx=0,zz=0,xz=0;for(const point of anterior){const dx=point.x-x,dz=point.z-z;xx+=dx*dx;zz+=dz*dz;xz+=dx*dz;}
    const angle=.5*Math.atan2(2*xz,xx-zz),direction=local.axis.direction.set(Math.cos(angle),0,Math.sin(angle));
    temporary.copy(anterior.at(-1)).sub(anterior[0]);if(direction.dot(temporary)<0)direction.negate();
    local.axis.origin.set(x,0,z);
    const along=point=>(point.x-x)*direction.x+(point.z-z)*direction.z;
    local.axis.start.copy(local.axis.origin).addScaledVector(direction,along(local.curve[0]));
    local.axis.end.copy(local.axis.origin).addScaledVector(direction,along(local.curve.at(-1)));
    result.maximumDOverL=0;
    for(const name of Object.keys(local.tips)){
      const point=local.tips[name];local.feet[name].copy(local.axis.origin).addScaledVector(direction,along(point));
      local.projectedTips[name].copy(point);local.projectedTips[name].y=0;
      const distance=(-(point.x-x)*direction.z+(point.z-z)*direction.x)/length;
      result.dOverL[name]=distance;result.maximumDOverL=Math.max(result.maximumDOverL,Math.abs(distance));
      toWorld(local.feet[name],world.feet[name]);toWorld(local.projectedTips[name],world.projectedTips[name]);
    }
    let previous,net=0,total=0,arc=0;
    for(let i=1;i<local.bodyCurve.length;i++){
      const a=local.bodyCurve[i-1],b=local.bodyCurve[i],dx=b.x-a.x,dz=b.z-a.z;
      const tangent=Math.atan2(dz,dx);if(previous!==undefined){const delta=wrap(tangent-previous);net+=delta;total+=Math.abs(delta);}previous=tangent;arc+=Math.hypot(dx,dz);
    }
    // Tiny nearly straight curves have an ill-conditioned coherence ratio.
    result.coherence=total>1e-3?Math.abs(net)/total:null;result.headTailAngle=net;
    const a=local.bodyCurve[0],b=local.bodyCurve.at(-1);result.chordShortening=arc?1-Math.hypot(b.x-a.x,b.z-a.z)/arc:0;
    local.curve.forEach((point,i)=>{toWorld(point,world.curve[i]);diagram.curve[i].x=point.x;diagram.curve[i].y=point.z;});
    local.bodyCurve.forEach((point,i)=>{toWorld(point,world.bodyCurve[i]);diagram.bodyCurve[i].x=point.x;diagram.bodyCurve[i].y=point.z;});
    toWorld(local.axis.origin,world.axis.origin);world.axis.direction.copy(direction).applyQuaternion(orientation);
    for(const end of ['start','end']){toWorld(local.axis[end],world.axis[end]);diagram.axis[end].x=local.axis[end].x;diagram.axis[end].y=local.axis[end].z;}
    for(const name of Object.keys(local.tips))for(const key of ['tips','feet']){diagram[key][name].x=local[key][name].x;diagram[key][name].y=local[key][name].z;}
    return result;
  }
  return {sample};
}
