import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Skeleton,Vector3,Quaternion,Matrix3,Matrix4} from '../static/vendor/three.webgpu.js';
import {createSupportArticulation} from '../static/aquarium-supports.js';

const point=(values,index)=>new Vector3().fromArray(values,index*3);
const center=(values,indices)=>indices.reduce((sum,i)=>sum.add(point(values,i)),new Vector3()).multiplyScalar(1/indices.length);
const supportName=name=>name.startsWith('Anatomy_dorsal_pterygiophore');
const radialName=name=>name.startsWith('Anatomy_dorsal_radial_joint');

async function pristineAsset(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'SupportGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  return (await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;
}

// SkeletonUtils-style cloning preserves the shared pristine geometry while
// rewiring every skin to the corresponding independent cloned bones.
function cloneSkeleton(source){
  const asset=source.clone(true),original=[],copies=[],nodes=new Map(),skeletons=new Map();
  source.traverse(node=>original.push(node));asset.traverse(node=>copies.push(node));
  original.forEach((node,i)=>nodes.set(node,copies[i]));
  for(const node of original)if(node.isSkinnedMesh){
    let skeleton=skeletons.get(node.skeleton);
    if(!skeleton){
      skeleton=new Skeleton(node.skeleton.bones.map(bone=>nodes.get(bone)),node.skeleton.boneInverses.map(matrix=>matrix.clone()));
      skeletons.set(node.skeleton,skeleton);
    }
    nodes.get(node).bind(skeleton,node.bindMatrix);
  }
  return asset;
}

function rings(mesh){
  const uv=mesh.geometry.attributes.uv,rows=new Map();
  for(let i=0;i<uv.count;i++){
    if(uv.getY(i)>.999)continue;
    const u=uv.getX(i);if(!rows.has(u))rows.set(u,[]);rows.get(u).push(i);
  }
  const result=[...rows].sort(([a],[b])=>a-b).map(([,ids])=>ids.sort((a,b)=>uv.getY(a)-uv.getY(b)));
  assert.equal(result.length,9,'each actual support and radial joint retains nine longitudinal tube rings');
  assert.ok(result.every(ids=>ids.length===8),'each material ring excludes its UV seam duplicate');
  return result;
}

function snapshot(mesh){
  mesh.skeleton.update();
  const a=mesh.geometry.attributes,palette=mesh.skeleton.boneMatrices,positions=new Float64Array(a.position.count*3),normals=new Float64Array(positions.length);
  const post=new Matrix4().multiplyMatrices(mesh.matrixWorld,mesh.bindMatrixInverse),worldNormal=new Matrix3().getNormalMatrix(mesh.matrixWorld);
  const input=new Vector3(),value=new Vector3(),skin=new Matrix4(),bone=new Matrix4(),local=new Matrix4(),normalMatrix=new Matrix3();
  for(let i=0;i<a.position.count;i++){
    input.fromBufferAttribute(a.position,i).applyMatrix4(mesh.bindMatrix);const x=input.x,y=input.y,z=input.z;
    let px=0,py=0,pz=0;skin.elements.fill(0);
    for(let slot=0;slot<4;slot++){
      const weight=a.skinWeight.getComponent(i,slot);if(!weight)continue;
      const o=a.skinIndex.getComponent(i,slot)*16;
      px+=weight*(palette[o]*x+palette[o+4]*y+palette[o+8]*z+palette[o+12]);
      py+=weight*(palette[o+1]*x+palette[o+5]*y+palette[o+9]*z+palette[o+13]);
      pz+=weight*(palette[o+2]*x+palette[o+6]*y+palette[o+10]*z+palette[o+14]);
      bone.fromArray(palette,o);for(let k=0;k<16;k++)skin.elements[k]+=weight*bone.elements[k];
    }
    value.set(px,py,pz).applyMatrix4(post).toArray(positions,i*3);
    local.multiplyMatrices(mesh.bindMatrixInverse,skin).multiply(mesh.bindMatrix);normalMatrix.setFromMatrix4(local);
    value.fromBufferAttribute(a.normal,i).applyMatrix3(normalMatrix).applyMatrix3(worldNormal).normalize().toArray(normals,i*3);
  }
  return {positions,normals};
}

function closestMaterial(value,line){
  let best;
  for(let i=1;i<line.length;i++){
    const direction=line[i].clone().sub(line[i-1]);
    const t=Math.max(0,Math.min(1,value.clone().sub(line[i-1]).dot(direction)/direction.lengthSq()));
    const anchor=line[i-1].clone().addScaledVector(direction,t),gap=anchor.distanceTo(value);
    if(!best||gap<best.gap)best={i,t,gap};
  }
  return best;
}

function geometryState(asset){
  const result=[];asset.traverse(mesh=>{if(mesh.isMesh)result.push({mesh,geometry:mesh.geometry,index:mesh.geometry.index?.array.slice(),
    attributes:Object.fromEntries(Object.entries(mesh.geometry.attributes).map(([name,a])=>[name,a.array.slice()]))});});return result;
}

function model(source,create){
  const asset=cloneSkeleton(source),original=geometryState(asset),joints=[],supports=[],radials=[];
  asset.traverse(node=>{if(node.isBone&&node.name.startsWith('spine_'))joints.push(node);if(!node.isSkinnedMesh)return;
    if(supportName(node.name))supports.push(node);if(radialName(node.name))radials.push(node);});
  joints.sort((a,b)=>a.name.localeCompare(b.name));asset.updateMatrixWorld(true);
  const inverse=asset.getWorldQuaternion(new Quaternion()).invert();
  const frames=joints.map(bone=>({rest:bone.quaternion.clone(),body:bone.getWorldQuaternion(new Quaternion()).premultiply(inverse),
    center:asset.worldToLocal(bone.getWorldPosition(new Vector3()))}));
  const neutral=new Map([...supports,...radials].map(mesh=>[mesh,{...snapshot(mesh),rings:rings(mesh)}]));
  supports.sort((a,b)=>center(neutral.get(a).positions,neutral.get(a).rings.at(-1)).x-center(neutral.get(b).positions,neutral.get(b).rings.at(-1)).x);
  radials.sort((a,b)=>center(neutral.get(a).positions,neutral.get(a).rings[0]).x-center(neutral.get(b).positions,neutral.get(b).rings[0]).x);
  assert.equal(supports.length,20,'the actual asset has twenty independently articulated dorsal supports');
  assert.equal(radials.length,20,'all twenty cartilage counterparts are measured');
  const pairs=supports.map((support,i)=>{
    const radial=radials[i],record=neutral.get(support),line=neutral.get(radial).rings.map(ids=>center(neutral.get(radial).positions,ids));
    const tip=center(record.positions,record.rings.at(-1)),material=closestMaterial(tip,line);
    assert.ok(material.gap<1e-6,'neutral material support ends lie on their corresponding cartilage centerline');
    return {support,radial,rings:record.rings,radialRings:neutral.get(radial).rings,material};
  });
  const articulation=create(asset),owned=new Set(articulation.ownedGeometries);
  assert.equal(owned.size,20,'only twenty support geometries become privately owned');
  for(const record of original){
    if(supportName(record.mesh.name)){
      assert.notEqual(record.mesh.geometry,record.geometry,'each mutable support geometry is cloned before its first pose');
      assert.ok(owned.has(record.mesh.geometry),'each cloned support belongs to the articulation lifecycle');
    }else assert.equal(record.mesh.geometry,record.geometry,'radial cartilage and every unrelated mesh retain their original geometry');
  }
  return {asset,original,joints,frames,pairs,articulation};
}

function poseSkeleton(model,{angle=0,vertical=0,twist=0,scale=1,root=false}={}){
  const {asset,joints,frames}=model;
  asset.position.set(root?.31:0,root?.72:0,root?-.46:0);asset.rotation.set(root?.23:0,root?.81:0,root?-.37:0,'YXZ');asset.scale.setScalar(scale);
  joints.forEach((bone,i)=>bone.quaternion.copy(frames[i].rest));asset.updateMatrixWorld(true);
  const rotation=asset.getWorldQuaternion(new Quaternion()),start=frames[5].center.x,length=frames.at(-1).center.x-start;
  for(let i=0;i<joints.length;i++){
    const s=i<=4?0:Math.max(0,Math.min(1,(frames[i].center.x-start)/length));
    const turn=new Quaternion().setFromAxisAngle(new Vector3(0,1,0),-angle*s),bend=new Quaternion().setFromAxisAngle(new Vector3(0,0,1),vertical*s);
    const tangent=new Vector3(0,1,0).applyQuaternion(frames[i].body),torsion=new Quaternion().setFromAxisAngle(tangent,twist*s);
    const desired=rotation.clone().multiply(turn).multiply(bend).multiply(torsion).multiply(frames[i].body);
    joints[i].quaternion.copy(joints[i].parent.getWorldQuaternion(new Quaternion()).invert().multiply(desired));joints[i].updateMatrixWorld(true);
  }
  asset.updateMatrixWorld(true);
}

function landmarks(values){
  const count=values.length/3,first=0;let second=1,third=2,fourth=3,largest=-1;
  const a=point(values,first);
  for(let i=1;i<count;i++){const d=a.distanceToSquared(point(values,i));if(d>largest){largest=d;second=i;}}
  const ab=point(values,second).sub(a);largest=-1;
  for(let i=0;i<count;i++){const area=ab.clone().cross(point(values,i).sub(a)).lengthSq();if(area>largest){largest=area;third=i;}}
  const normal=ab.clone().cross(point(values,third).sub(a));largest=-1;
  for(let i=0;i<count;i++){const volume=Math.abs(normal.dot(point(values,i).sub(a)));if(volume>largest){largest=volume;fourth=i;}}
  assert.ok(largest>1e-10,'four actual tube vertices identify a noncoplanar rigid solid');return[first,second,third,fourth];
}

function basis(values,ids){
  const a=point(values,ids[0]),u=point(values,ids[1]).sub(a).normalize(),v=point(values,ids[2]).sub(a);
  v.addScaledVector(u,-u.dot(v)).normalize();const w=u.clone().cross(v);
  return new Matrix3().set(u.x,v.x,w.x,u.y,v.y,w.y,u.z,v.z,w.z);
}

function assertRigid(before,after,report){
  const ids=landmarks(before.positions),rotation=basis(after.positions,ids).multiply(basis(before.positions,ids).transpose());
  const origin=point(before.positions,ids[0]),target=point(after.positions,ids[0]);
  for(let i=0;i<before.positions.length/3;i++){
    const expected=point(before.positions,i).sub(origin).applyMatrix3(rotation).add(target),error=expected.distanceTo(point(after.positions,i));
    report.maximumRigidError=Math.max(report.maximumRigidError,error);
    assert.ok(error<2e-6,'every actual bone vertex undergoes one rigid rotation without stretch, shear or reflection');
    const normal=point(before.normals,i).applyMatrix3(rotation),actual=point(after.normals,i);
    assert.ok(actual.toArray().every(Number.isFinite)&&Math.abs(actual.length()-1)<1e-6,'uploaded bone normals remain finite unit directions');
    assert.ok(normal.dot(actual)>.99999,'uploaded normals follow the same rigid rotation as the actual bone surface');
  }
  const volume=values=>point(values,ids[1]).sub(point(values,ids[0])).cross(point(values,ids[2]).sub(point(values,ids[0]))).dot(point(values,ids[3]).sub(point(values,ids[0])));
  const ratio=volume(after.positions)/volume(before.positions);
  assert.ok(ratio>.999&&ratio<1.001,'the actual rigid solid retains signed volume and handedness');
}

function radialSolid(mesh,values,rows){
  const positions=values.positions,index=mesh.geometry.index.array,triangles=[];
  for(let i=0;i<index.length;i+=3)triangles.push([0,1,2].map(k=>point(positions,index[i+k])));
  const axis=center(positions,rows.at(-1)).sub(center(positions,rows[0]));
  for(const end of [0,rows.length-1]){
    const ids=rows[end],middle=center(positions,ids),outward=axis.clone().multiplyScalar(end?1:-1);
    for(let i=0;i<ids.length;i++){
      const a=point(positions,ids[i]),b=point(positions,ids[(i+1)%ids.length]);
      triangles.push(a.clone().sub(middle).cross(b.clone().sub(middle)).dot(outward)<0?[middle,b,a]:[middle,a,b]);
    }
  }
  return triangles;
}

function winding(value,triangles){
  let angle=0;for(const triangle of triangles){
    const [a,b,c]=triangle.map(p=>p.clone().sub(value)),la=a.length(),lb=b.length(),lc=c.length();
    angle+=2*Math.atan2(a.dot(b.clone().cross(c)),la*lb*lc+a.dot(b)*lc+b.dot(c)*la+c.dot(a)*lb);
  }return angle/(4*Math.PI);
}

function checkPose(model,options,report){
  poseSkeleton(model,options);
  const before=new Map(model.pairs.flatMap(({support,radial})=>[support,radial]).map(mesh=>[mesh,snapshot(mesh)]));
  const bones=model.joints.map(bone=>bone.matrixWorld.toArray());model.articulation.pose();
  model.joints.forEach((bone,i)=>assert.deepEqual(bone.matrixWorld.toArray(),bones[i],'support articulation never changes the swimming skeleton'));
  for(const {support,radial,rings:rows,radialRings,material} of model.pairs){
    const original=before.get(support),after=snapshot(support),cartilage=snapshot(radial),line=radialRings.map(ids=>center(cartilage.positions,ids));
    assert.deepEqual(cartilage,before.get(radial),'the actual cartilage geometry and its rendered pose remain untouched');
    assertRigid(original,after,report);
    const proximal=center(original.positions,rows[0]),tip=center(after.positions,rows.at(-1));
    const target=line[material.i-1].clone().lerp(line[material.i],material.t),chord=proximal.distanceTo(center(original.positions,rows.at(-1)));
    const anchorError=proximal.distanceTo(center(after.positions,rows[0])),gap=target.distanceTo(tip),minimum=Math.abs(proximal.distanceTo(target)-chord);
    report.maximumAnchorError=Math.max(report.maximumAnchorError,anchorError);report.maximumMaterialGap=Math.max(report.maximumMaterialGap,gap);
    assert.ok(anchorError<1e-6,'the actual proximal support ring remains anchored');
    assert.ok(gap<minimum+2e-6,'the rigid distal endpoint reaches the closest possible fixed cartilage material target');
    const solid=radialSolid(radial,cartilage,radialRings);
    assert.ok(Math.abs(Math.abs(winding(center(cartilage.positions,radialRings[4]),solid))-1)<1e-4,'diagnostic end caps close the actual cartilage tube with consistent winding');
    const inside=rows.at(-1).filter(id=>Math.abs(winding(point(after.positions,id),solid))>.5).length;
    // Compound torsion can expose part of an oblique rim while the two actual
    // solids still overlap. A material surface point inside the cartilage
    // proves local contact without imposing complete socket containment.
    if(options.vertical||options.twist)assert.ok(inside>0,'the actual support rim retains local contact with the cartilage volume');
    else assert.equal(inside,8,'the complete actual distal support rim stays within the cartilage volume during a planar C');
  }
  const paused=model.pairs.map(({support})=>({positions:support.geometry.attributes.position.array.slice(),normals:support.geometry.attributes.normal.array.slice()}));
  model.articulation.pose();
  model.pairs.forEach(({support},i)=>{
    assert.deepEqual(support.geometry.attributes.position.array,paused[i].positions,'repeated paused poses do not accumulate position drift');
    assert.deepEqual(support.geometry.attributes.normal.array,paused[i].normals,'repeated paused poses do not accumulate normal drift');
  });
}

function assertOriginal(state){
  for(const record of state){
    assert.deepEqual(record.geometry.index?.array,record.index,'articulation never changes source topology');
    for(const [name,values] of Object.entries(record.attributes))assert.deepEqual(record.geometry.attributes[name].array,values,
      'the pristine and shared source buffers remain unchanged');
  }
}

export async function assertSupportArticulation(create=createSupportArticulation){
  const source=await pristineAsset(),sourceState=geometryState(source),a=model(source,create),b=model(source,create);
  for(let i=0;i<a.pairs.length;i++){
    assert.notEqual(a.pairs[i].support.geometry,b.pairs[i].support.geometry,'independent skeleton instances own independent mutable bone geometry');
    assert.notEqual(a.pairs[i].support.skeleton,b.pairs[i].support.skeleton,'each instance retains its independent skeleton');
  }
  const report={maximumRigidError:0,maximumAnchorError:0,maximumMaterialGap:0,poses:0};
  for(const angle of [0,.55,-.55,1.2,-1.2,1.8,-1.8,2.4,-2.4]){checkPose(a,{angle},report);report.poses++;}
  for(const sign of [-1,1]){
    checkPose(a,{angle:sign*2.4,vertical:sign*.16,twist:sign*.20,root:true},report);report.poses++;
    for(let i=0;i<=12;i++){
      const t=i/12,u=t<.4?t/.4:(1-t)/.6,q=u*u*u*(10+u*(-15+6*u));
      checkPose(b,{angle:sign*2.4*q,vertical:sign*.16*q,twist:sign*.20*q,scale:.43,root:true},report);report.poses++;
    }
  }
  const held=a.pairs.map(({support})=>support.geometry.attributes.position.array.slice());checkPose(b,{angle:-1.8},report);
  a.pairs.forEach(({support},i)=>assert.deepEqual(support.geometry.attributes.position.array,held[i],'posing another skeleton instance cannot alter this fish'));
  assertOriginal(sourceState);assertOriginal(a.original);assertOriginal(b.original);
  let ownedDisposals=0,foreignDisposals=0;
  for(const model of [a,b])for(const geometry of model.articulation.ownedGeometries)geometry.addEventListener('dispose',()=>ownedDisposals++);
  for(const record of sourceState)record.geometry.addEventListener('dispose',()=>foreignDisposals++);
  a.articulation.dispose();a.articulation.dispose();assert.equal(ownedDisposals,20,'disposal releases each owned support geometry exactly once');
  assert.equal(foreignDisposals,0,'disposing one fish preserves the pristine and other fish geometry');
  b.articulation.dispose();assert.equal(ownedDisposals,40,'both independent geometry lifecycles release their own supports');
  return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const report=await assertSupportArticulation();console.log('Aquarium rigid dorsal support articulation passed',JSON.stringify(report));
}
