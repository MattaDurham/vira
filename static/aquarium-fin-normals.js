// Capture this before the first normal recomputation. glTF UV seams duplicate
// vertices whose authored normals still describe one smooth tube surface.
export function createFinNormalSmoother(geometry){
  const {position,normal,skinIndex,skinWeight}=geometry.attributes;
  const count=position.count,groups=[],coincident=new Map();
  const influences=i=>{
    if(!skinIndex||!skinWeight)return '';
    const weights=new Map();
    for(let slot=0;slot<skinWeight.itemSize;slot++){
      const weight=skinWeight.getComponent(i,slot);
      if(weight>0){
        const joint=skinIndex.getComponent(i,slot);
        weights.set(joint,(weights.get(joint)??0)+weight);
      }
    }
    return [...weights].sort((a,b)=>a[0]-b[0]).map(pair=>pair.join(':')).join(',');
  };
  for(let i=0;i<count;i++){
    // Exact exported positions avoid merging nearby branches or thin tissue.
    const key=[position.getX(i),position.getY(i),position.getZ(i),influences(i)].join('|');
    const x=normal.getX(i),y=normal.getY(i),z=normal.getZ(i),length=Math.hypot(x,y,z);
    if(!length)continue;
    const nx=x/length,ny=y/length,nz=z/length;
    let candidates=coincident.get(key);
    if(!candidates)coincident.set(key,candidates=[]);
    // Compare with a fixed authored normal so a chain cannot bridge a crease.
    let group=candidates.find(item=>Math.hypot(nx-item.x,ny-item.y,nz-item.z)<.00001);
    if(!group){group={x:nx,y:ny,z:nz,indices:[]};candidates.push(group);}
    group.indices.push(i*3);
  }
  for(const candidates of coincident.values())for(const group of candidates){
    if(group.indices.length>1)groups.push(Uint32Array.from(group.indices));
  }
  // The frame loop reuses its existing normal buffer and these cached groups.
  // Positions, indices, UVs, skin weights and deliberately sharp splits stay put.
  return function reconcile(attribute=geometry.attributes.normal){
    const output=attribute.array??attribute;
    for(const group of groups){
      let x=0,y=0,z=0;
      for(const offset of group){x+=output[offset];y+=output[offset+1];z+=output[offset+2];}
      const length=Math.hypot(x,y,z);
      if(length<1e-12)continue;
      x/=length;y/=length;z/=length;
      for(const offset of group){output[offset]=x;output[offset+1]=y;output[offset+2]=z;}
    }
    if(attribute.isBufferAttribute)attribute.needsUpdate=true;
    return attribute;
  };
}
