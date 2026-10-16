import {Box3,Matrix4,Vector3} from './vendor/three.webgpu.js';
import {applySwimPose,createFacialRig} from './aquarium-rig.js?v=88';

const dynamicFin=name=>/^(Pectoral|Pelvic|Caudal|Dorsal|Anal)_(membrane|branched_rays)/.test(name);
const corners=box=>Array.from({length:8},(_,i)=>new Vector3(i&1?box.max.x:box.min.x,i&2?box.max.y:box.min.y,i&4?box.max.z:box.min.z));

function tubeComponents(mesh){
  const a=mesh.geometry.attributes,index=mesh.geometry.index,parent=Array.from({length:a.position.count},(_,i)=>i);
  const find=i=>parent[i]===i?i:parent[i]=find(parent[i]);
  for(let i=0;i<index.count;i+=3){const root=find(index.getX(i));parent[find(index.getX(i+1))]=root;parent[find(index.getX(i+2))]=root;}
  const groups=new Map();for(let i=0;i<parent.length;i++){const root=find(i);if(!groups.has(root))groups.set(root,[]);groups.get(root).push(i);}
  return [...groups.values()].map(ids=>{
    const rows=new Map();for(const i of ids){if(a.uv.getY(i)>.999)continue;const u=a.uv.getX(i);if(!rows.has(u))rows.set(u,[]);rows.get(u).push(i);}
    const centers=[...rows].sort(([x],[y])=>x-y).map(([,ring])=>ring.reduce((p,i)=>p.add(new Vector3().fromBufferAttribute(a.position,i)),new Vector3()).multiplyScalar(1/ring.length));
    const arc=[0];for(let i=1;i<centers.length;i++)arc.push(arc.at(-1)+centers[i].distanceTo(centers[i-1]));
    let radius=0;for(const i of ids){const p=new Vector3().fromBufferAttribute(a.position,i);radius=Math.max(radius,Math.min(...centers.map(c=>p.distanceTo(c))));}
    return {centers,arc,radius};
  });
}

/** Cheap, read-only forecast of a COMPLETE proposed fish pose.
 * Capture a pristine instance before its first deformation. bounds(pose) runs
 * the exact rig on a private bone-only hierarchy, including facial articulation
 * and the scaled three-axis geometric-centering translation. It never changes
 * the live skeleton, geometry, controllers or simulation time.
 *
 * Ordinary skinning is a convex combination: transformed per-influence boxes
 * enclose every strictly positive skin influence. Flexible fins instead use
 * actual basal material anchors and rest ray/tree ARC lengths, not tip chords.
 * Paired fins add their bounded 10mm/7mm cup plus1.5mm/1.05mm passive shear;
 * all other paired operators are rotations. A7mm scaled guard covers barbel
 * displacement (<=3.02mm), ray tube transport and Float32 palette rounding.
 * These bounds are conservative navigation forecasts; actual deformed-geometry
 * containment remains the final guard. Result vectors are reused per call. */
export function createManeuverFootprint(asset){
  const scale=asset.scale.x;
  if(!(scale>0)||Math.abs(scale-asset.scale.y)>1e-10||Math.abs(scale-asset.scale.z)>1e-10)throw new Error('Maneuver footprint needs positive uniform source scale');
  const map=new Map();
  function cloneRig(node){
    if(!node.isBone&&!node.children.some(child=>hasBone(child)))return null;
    const copy=node.clone(false);copy.userData={};map.set(node,copy);
    for(const child of node.children){const cloned=cloneRig(child);if(cloned)copy.add(cloned);}
    return copy;
  }
  function hasBone(node){return node.isBone||node.children.some(hasBone);}
  const shadow=cloneRig(asset),joints=[];
  shadow.traverse(node=>{if(node.isBone&&node.name.startsWith('spine_'))joints.push(node);});joints.sort((a,b)=>a.name.localeCompare(b.name));
  if(joints.length!==34)throw new Error('Maneuver footprint needs the complete exported spine');
  const rest=joints.map(bone=>bone.quaternion.clone()),face=createFacialRig(shadow),boxes=new Map(),spheres=[];
  const bind=new Vector3(),point=new Vector3(),post=new Matrix4();
  asset.updateMatrixWorld(true);
  const meshes=[];asset.traverse(mesh=>{if(mesh.isSkinnedMesh)meshes.push(mesh);});
  function terms(mesh,index){
    const a=mesh.geometry.attributes,terms=[];bind.fromBufferAttribute(a.position,index).applyMatrix4(mesh.bindMatrix);
    for(let slot=0;slot<4;slot++){
      const weight=a.skinWeight.getComponent(index,slot);if(!weight)continue;
      const id=a.skinIndex.getComponent(index,slot),bone=map.get(mesh.skeleton.bones[id]);
      if(!bone)throw new Error('Maneuver footprint skin extends beyond the cloned rig');
      terms.push({bone,point:bind.clone().applyMatrix4(mesh.skeleton.boneInverses[id]),weight});
    }
    return terms;
  }
  for(const mesh of meshes){
    // Attached glTF skins cancel this post transform. Refuse an unsupported
    // detached skin instead of silently forecasting it in the wrong frame.
    post.multiplyMatrices(mesh.matrixWorld,mesh.bindMatrixInverse);
    if(post.elements.some((value,i)=>Math.abs(value-(i%5===0?1:0))>1e-6))throw new Error('Maneuver footprint requires attached glTF skin binding');
    if(dynamicFin(mesh.name))continue;
    for(let i=0;i<mesh.geometry.attributes.position.count;i++)for(const term of terms(mesh,i)){
      if(!boxes.has(term.bone))boxes.set(term.bone,new Box3());boxes.get(term.bone).expandByPoint(term.point);
    }
  }
  for(const family of ['Pectoral','Pelvic'])for(const side of [-1,1]){
    const membranes=meshes.filter(mesh=>mesh.name.startsWith(`${family}_membrane`));
    const membrane=membranes.find(mesh=>Math.sign(mesh.geometry.attributes.position.getZ(0))===side);
    if(!membrane)throw new Error('Maneuver footprint paired-fin attachment is missing');
    const pivot=new Vector3().fromBufferAttribute(membrane.geometry.attributes.position,0),anchor=terms(membrane,0);let radius=0;
    for(const mesh of meshes.filter(mesh=>mesh.name.startsWith(`${family}_`)))for(let i=0;i<mesh.geometry.attributes.position.count;i++){
      const p=new Vector3().fromBufferAttribute(mesh.geometry.attributes.position,i);if(Math.sign(p.z)!==side)continue;radius=Math.max(radius,p.distanceTo(pivot));
    }
    spheres.push({terms:anchor,radius:radius+(family==='Pectoral'?.0115:.00805)});
  }
  for(const family of ['Caudal','Dorsal','Anal']){
    const membrane=meshes.find(mesh=>mesh.name===`${family}_membrane`),rays=meshes.find(mesh=>mesh.name===`${family}_branched_rays`);
    if(!membrane||!rays)throw new Error('Maneuver footprint median/caudal geometry is missing');
    const a=membrane.geometry.attributes,rows=new Map();
    for(let i=0;i<a.position.count;i++){const u=a.uv.getX(i);if(!rows.has(u))rows.set(u,[]);rows.get(u).push(i);}
    const anchors=[...rows.values()].map(ids=>{
      ids.sort((i,j)=>a.uv.getY(j)-a.uv.getY(i));let radius=0;
      for(let i=1;i<ids.length;i++)radius+=new Vector3().fromBufferAttribute(a.position,ids[i]).distanceTo(new Vector3().fromBufferAttribute(a.position,ids[i-1]));
      return {point:new Vector3().fromBufferAttribute(a.position,ids[0]),terms:terms(membrane,ids[0]),radius};
    });
    // Tube branches are separate components. Resolve their actual shared root
    // onto an already rooted shaft ring before accumulating the material arc.
    const tubes=tubeComponents(rays);let remaining=tubes.length;
    for(let pass=0;pass<=tubes.length&&remaining;pass++)for(const tube of tubes){
      if(tube.anchor!==undefined)continue;
      const root=tube.centers[0];let nearest=Infinity,anchorIndex,start;
      anchors.forEach((anchor,i)=>{const distance=root.distanceTo(anchor.point);if(distance<nearest){nearest=distance;anchorIndex=i;start=distance;}});
      if(nearest>1e-5){
        nearest=Infinity;for(const parent of tubes){if(parent===tube||parent.anchor===undefined)continue;
          parent.centers.forEach((center,i)=>{const distance=root.distanceTo(center);if(distance<nearest){nearest=distance;anchorIndex=parent.anchor;start=parent.start+parent.arc[i]+distance;}});
        }
      }
      if(nearest>1e-5)continue;
      tube.anchor=anchorIndex;tube.start=start;remaining--;
      anchors[anchorIndex].radius=Math.max(anchors[anchorIndex].radius,start+tube.arc.at(-1)+tube.radius);
    }
    if(remaining)throw new Error('Maneuver footprint ray tree has no actual material attachment');
    spheres.push(...anchors);
  }
  const primitives=[...boxes].map(([bone,box])=>({bone,corners:corners(box)})),bounds=new Box3(),origin=new Vector3();
  function forecast(pose){
    shadow.scale.setScalar(scale);applySwimPose(shadow,joints,rest,pose);
    origin.set(pose.x,pose.y,pose.z);shadow.position.sub(origin).multiplyScalar(scale).add(origin);shadow.updateMatrixWorld(true);face.pose(pose);
    bounds.makeEmpty();
    for(const primitive of primitives)for(const corner of primitive.corners)bounds.expandByPoint(point.copy(corner).applyMatrix4(primitive.bone.matrixWorld));
    for(const sphere of spheres){
      point.set(0,0,0);for(const term of sphere.terms)point.addScaledVector(bind.copy(term.point).applyMatrix4(term.bone.matrixWorld),term.weight);
      const radius=sphere.radius*scale;bounds.min.min(bind.copy(point).addScalar(-radius));bounds.max.max(bind.copy(point).addScalar(radius));
    }
    bounds.expandByScalar(.007*scale);return bounds;
  }
  return {bounds:forecast,scale,primitiveCount:primitives.length+spheres.length};
}
