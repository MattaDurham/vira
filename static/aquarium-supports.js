import {Matrix4,Quaternion,Vector3} from './vendor/three.webgpu.js';

function tubeRings(mesh){
  const uv=mesh.geometry.attributes.uv,rows=new Map();
  if(!uv)throw new Error('Dorsal support articulation needs the actual tube UV rings');
  for(let i=0;i<uv.count;i++){
    if(uv.getY(i)>.999)continue; // The duplicated circumferential seam is not extra material.
    const u=uv.getX(i);if(!rows.has(u))rows.set(u,[]);rows.get(u).push(i);
  }
  const rings=[...rows].sort(([a],[b])=>a-b).map(([,indices])=>indices);
  if(rings.length<2||!rings.every(indices=>indices.length===8))throw new Error('Dorsal support tube sampling is unsupported');
  return rings;
}

/** Articulate the rigid dorsal supports toward their actual radial cartilage.
 * Construct on a pristine instance, before its first pose. Each support gets
 * one owned geometry clone; radial cartilage, source buffers, materials and
 * skeletons remain caller-owned. Capture all companions before constructing
 * this helper so a clone never inherits already corrected bind positions.
 *
 * Call pose() after applySwimPose and before median-fin deformation. It starts
 * from immutable bind positions every time, rotates about the actually posed
 * proximal endpoint, then inverts the current skin map for normal rendering.
 * There is no scaling, accumulated displacement, force model or invented
 * cartilage measurement. The small remaining endpoint-length discrepancy is
 * retained rather than stretching the bone to force an exact endpoint match.
 * Root transforms, including positive uniform instance scale, are removed for
 * this animal-frame construction. ownedGeometries may join a caller's disposal
 * set, OR dispose() may release them; do not use both ownership paths. */
export function createSupportArticulation(asset){
  const supports=[],radials=[];
  asset.traverse(mesh=>{
    if(!mesh.isSkinnedMesh)return;
    if(mesh.name.startsWith('Anatomy_dorsal_pterygiophore'))supports.push(mesh);
    if(mesh.name.startsWith('Anatomy_dorsal_radial_joint'))radials.push(mesh);
  });
  if(!supports.length||supports.length!==radials.length)throw new Error('Dorsal supports and radial cartilage must form complete pairs');
  asset.updateMatrixWorld(true);
  const skeletons=[...new Set([...supports,...radials].map(mesh=>mesh.skeleton))];
  skeletons.forEach(skeleton=>skeleton.update());
  const assetInverse=new Matrix4().copy(asset.matrixWorld).invert(),skin=new Matrix4(),bindPoint=new Vector3(),point=new Vector3();
  const records=new Map([...supports,...radials].map(mesh=>[mesh,{mesh,rings:tubeRings(mesh),
    base:Float64Array.from(mesh.geometry.attributes.position.array),
    normals:Float64Array.from(mesh.geometry.attributes.normal.array),post:new Matrix4()}]));
  const refresh=()=>{
    asset.updateMatrixWorld(true);assetInverse.copy(asset.matrixWorld).invert();
    skeletons.forEach(skeleton=>skeleton.update());
    for(const record of records.values())record.post.copy(assetInverse).multiply(record.mesh.matrixWorld).multiply(record.mesh.bindMatrixInverse);
  };
  function vertex(record,index,out){
    const {mesh,post}=record,attrs=mesh.geometry.attributes,palette=mesh.skeleton.boneMatrices;
    bindPoint.fromArray(record.base,index*3).applyMatrix4(mesh.bindMatrix);out.set(0,0,0);
    for(let slot=0;slot<4;slot++){
      const weight=attrs.skinWeight.getComponent(index,slot);if(!weight)continue;
      const e=attrs.skinIndex.getComponent(index,slot)*16;
      out.x+=weight*(palette[e]*bindPoint.x+palette[e+4]*bindPoint.y+palette[e+8]*bindPoint.z+palette[e+12]);
      out.y+=weight*(palette[e+1]*bindPoint.x+palette[e+5]*bindPoint.y+palette[e+9]*bindPoint.z+palette[e+13]);
      out.z+=weight*(palette[e+2]*bindPoint.x+palette[e+6]*bindPoint.y+palette[e+10]*bindPoint.z+palette[e+14]);
    }
    return out.applyMatrix4(post);
  }
  function center(record,indices,out){
    out.set(0,0,0);for(const index of indices)out.add(vertex(record,index,point));return out.multiplyScalar(1/indices.length);
  }
  refresh();
  const neutral=new Map([...records.values()].map(record=>[record.mesh,record.rings.map(indices=>center(record,indices,new Vector3()))]));
  supports.sort((a,b)=>neutral.get(a).at(-1).x-neutral.get(b).at(-1).x);
  radials.sort((a,b)=>neutral.get(a)[0].x-neutral.get(b)[0].x);
  const pairs=supports.map((mesh,i)=>{
    const support=records.get(mesh),radial=records.get(radials[i]),tip=neutral.get(mesh).at(-1),line=neutral.get(radials[i]);
    if(Math.abs(tip.x-line[0].x)>1e-5)throw new Error('Dorsal supports do not match their authored radial stations');
    let material;
    for(let j=1;j<line.length;j++){
      const direction=line[j].clone().sub(line[j-1]),length=direction.lengthSq();
      const fraction=length?Math.max(0,Math.min(1,tip.clone().sub(line[j-1]).dot(direction)/length)):0;
      const distance=tip.distanceTo(line[j-1].clone().addScaledVector(direction,fraction));
      if(!material||distance<material.distance)material={first:j-1,last:j,fraction,distance};
    }
    if(material.distance>.001)throw new Error('Dorsal support has no neutral radial attachment');
    // The actual support is one rigid bone. Reject a later asset that silently
    // turns it into a stretched LBS rod; it needs a different articulation rig.
    const attrs=mesh.geometry.attributes;
    let joint;for(let slot=0;slot<4;slot++)if(attrs.skinWeight.getComponent(0,slot)>0)joint=attrs.skinIndex.getComponent(0,slot);
    for(let index=0;index<attrs.position.count;index++){
      let total=0;
      for(let slot=0;slot<4;slot++){
        const weight=attrs.skinWeight.getComponent(index,slot);total+=weight;
        if(weight&&((attrs.skinIndex.getComponent(index,slot)!==joint)||Math.abs(weight-1)>1e-7))throw new Error('Dorsal support must retain a single rigid skin joint');
      }
      if(Math.abs(total-1)>1e-7)throw new Error('Dorsal support skin weights must sum to one');
    }
    return {support,radial,material,joint,matrix:new Matrix4(),inverse:new Matrix4(),
      proximal:new Vector3(),distal:new Vector3(),target:new Vector3(),other:new Vector3(),rotation:new Quaternion()};
  });
  const ownedGeometries=new Set();
  for(const {support} of pairs){support.mesh.geometry=support.mesh.geometry.clone();ownedGeometries.add(support.mesh.geometry);}
  let disposed=false;
  function pose(){
    if(disposed)return asset;
    refresh();
    for(const pair of pairs){
      const {support,radial,material,joint,matrix,inverse,proximal,distal,target,other,rotation}=pair;
      const {mesh,base,normals,post}=support;
      skin.fromArray(mesh.skeleton.boneMatrices,joint*16);
      matrix.copy(post).multiply(skin).multiply(mesh.bindMatrix);inverse.copy(matrix).invert();
      center(support,support.rings[0],proximal);center(support,support.rings.at(-1),distal);
      center(radial,radial.rings[material.first],target);center(radial,radial.rings[material.last],other);target.lerp(other,material.fraction);
      const originalDirection=distal.clone().sub(proximal),targetDirection=target.clone().sub(proximal);
      if(originalDirection.lengthSq()<1e-16||targetDirection.lengthSq()<1e-16)throw new Error('Dorsal support articulation has a degenerate endpoint');
      rotation.setFromUnitVectors(originalDirection.normalize(),targetDirection.normalize());
      const positions=mesh.geometry.attributes.position,normal=mesh.geometry.attributes.normal;
      for(let i=0;i<positions.count;i++){
        point.fromArray(base,i*3).applyMatrix4(matrix).sub(proximal).applyQuaternion(rotation).add(proximal).applyMatrix4(inverse);
        positions.setXYZ(i,point.x,point.y,point.z);
        point.fromArray(normals,i*3).transformDirection(matrix).applyQuaternion(rotation).transformDirection(inverse);
        normal.setXYZ(i,point.x,point.y,point.z);
      }
      positions.needsUpdate=true;normal.needsUpdate=true;
    }
    return asset;
  }
  return {pose,ownedGeometries,count:pairs.length,dispose(){
    if(disposed)return;disposed=true;for(const geometry of ownedGeometries)geometry.dispose();
  }};
}
