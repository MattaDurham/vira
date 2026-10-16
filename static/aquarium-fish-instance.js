/* Independent live koi built from a pristine, undeformed GLTF scene. The
 * geometry is uniformly scaled; the navigation gains remain the authored
 * adult-koi controller, not a calibrated juvenile or second-species model. */
import {Skeleton,Vector3} from './vendor/three.webgpu.js';
import {createSwimmer} from './aquarium-motion.js?v=92';
import {applySwimPose,createFacialRig} from './aquarium-rig.js?v=92';
import {createContainment} from './aquarium-collision.js?v=90';
import {createFinFrame,createFinController,bindFinGeometry,deformFinGeometry} from './aquarium-fins.js?v=90';
import {createCaudalFrame,createCaudalController,bindCaudalGeometry,deformCaudalGeometry} from './aquarium-caudal.js?v=90';
import {createMedianController,createMedianFin,deformMedianFin} from './aquarium-median.js?v=90';
import {createFinNormalSmoother} from './aquarium-fin-normals.js?v=90';
import {createSupportArticulation} from './aquarium-supports.js?v=90';
import {createManeuverFootprint} from './aquarium-footprint.js?v=92';
import {createAxialFrame} from './aquarium-axial-frame.js?v=90';
import {createMassPlacement} from './aquarium-mass-placement.js?v=92';

const mutableGeometry=name=>/^(Caudal|Pectoral|Pelvic|Dorsal|Anal)_/.test(name)||name.includes('sensory_barbel');

/** Capture companions before the source's first pose or vertex deformation.
 * materialFor may return shared, caller-owned materials. Materials and textures
 * are never disposed here; ownedGeometries and ownedSkeletons belong solely to
 * this instance. Immutable body/scale meshes retain their original geometry. */
export function createKoiInstance(sourceAsset,{scale=1,initialPose={},random=Math.random,materialFor,turnControllerFactory,massProfile}={}){
  if(!sourceAsset?.isObject3D)throw new Error('A pristine koi GLTF scene is required');
  if(!Number.isFinite(scale)||scale<=0)throw new Error('Koi scale must be positive and finite');
  const sourceScale=sourceAsset.scale;
  if(!sourceScale.toArray().every(value=>Number.isFinite(value)&&value>0)||
    Math.abs(sourceScale.x-sourceScale.y)>1e-12||Math.abs(sourceScale.x-sourceScale.z)>1e-12)
    throw new Error('The koi source root must have positive uniform scale');
  const sourceNodes=[],clonedNodes=[],asset=sourceAsset.clone(true),nodeMap=new Map();
  sourceAsset.traverse(node=>sourceNodes.push(node));asset.traverse(node=>clonedNodes.push(node));
  sourceNodes.forEach((node,index)=>nodeMap.set(node,clonedNodes[index]));
  const ownedGeometries=new Set(),ownedSkeletons=new Set(),skeletonMap=new Map();
  const joints=[],fins=[],barbels=[],medianMeshes={},medianFins=[],anatomy=[],scales=[],finSupports=[];
  const materials=new Map();
  for(const source of sourceNodes){
    const node=nodeMap.get(source);
    // Object3D.copy JSON-serializes userData. Live rig Vector3/Quaternion caches
    // cannot survive that operation, and cannot describe the clone's frame.
    for(const key of ['bodyRest','bodyCenter','bodyTangent','axial'])delete node.userData[key];
    if(node.isBone&&node.name.startsWith('spine_'))joints.push(node);
    if(!node.isMesh)continue;
    if(source.isSkinnedMesh){
      let skeleton=skeletonMap.get(source.skeleton);
      if(!skeleton){
        const bones=source.skeleton.bones.map(bone=>{
          const clone=nodeMap.get(bone);
          if(!clone)throw new Error('Koi skeleton extends beyond its source scene');
          return clone;
        });
        skeleton=new Skeleton(bones,source.skeleton.boneInverses.map(matrix=>matrix.clone()));
        skeletonMap.set(source.skeleton,skeleton);ownedSkeletons.add(skeleton);
      }
      // An omitted bind matrix would recalculate inverses from current world
      // matrices. Preserve the exported binding independently of the root pose.
      node.bind(skeleton,source.bindMatrix);
    }
    if(mutableGeometry(node.name)){
      node.geometry=source.geometry.clone();node.geometry.userData={...source.geometry.userData};
      ownedGeometries.add(node.geometry);
    }
    node.castShadow=node.receiveShadow=true;node.frustumCulled=false;
    if(!node.name.startsWith('Overlapping_scales')&&!node.name.startsWith('Anatomy'))node.layers.enable(1);
    if(node.name.startsWith('Anatomy')){anatomy.push(node);node.visible=false;}
    if(node.name.startsWith('Overlapping_scales'))scales.push(node);
    if(node.name.includes('branched_rays')){finSupports.push(node);node.visible=true;}
    if(materialFor){
      const replace=original=>{
        const material=materialFor(original,node);
        if(!material?.isMaterial)throw new Error('Koi materialFor must return a material');
        return material;
      };
      node.material=Array.isArray(source.material)?source.material.map(replace):replace(source.material);
    }
    materials.set(node,node.material);
    if(/^(Caudal|Pectoral|Pelvic)_/.test(node.name)){
      const geometry=node.geometry,points=geometry.attributes.position,base=Float32Array.from(points.array);
      fins.push({mesh:node,geometry,points,base,paired:!node.name.startsWith('Caudal'),
        pectoral:node.name.startsWith('Pectoral'),
        reconcileNormals:node.name.includes('branched_rays')?createFinNormalSmoother(geometry):null});
    }
    if(/^(Dorsal|Anal)_(membrane|branched_rays)/.test(node.name))medianMeshes[node.name]=node;
    if(node.name.includes('sensory_barbel')){
      const points=node.geometry.attributes.position;barbels.push({geometry:node.geometry,points,base:Float32Array.from(points.array)});
    }
  }
  for(const pectoral of [true,false]){
    const family=fins.filter(fin=>fin.paired&&fin.pectoral===pectoral),frames={};
    for(const fin of family.filter(fin=>fin.mesh.name.includes('_membrane'))){
      const frame=createFinFrame(fin.base,fin.geometry.attributes.uv.array);frames[frame.side]=frame;
    }
    for(const fin of family)fin.binding=bindFinGeometry(fin.base,frames);
  }
  const caudal=fins.find(fin=>!fin.paired&&fin.mesh.name.startsWith('Caudal_membrane'));
  if(!caudal)throw new Error('The koi caudal membrane is missing');
  const caudalFrame=createCaudalFrame(caudal.base,caudal.geometry.attributes.uv.array);
  for(const fin of fins.filter(fin=>!fin.paired))fin.binding=bindCaudalGeometry(fin.base,caudalFrame);
  for(const [kind,prefix] of [['dorsal','Dorsal'],['anal','Anal']]){
    medianFins.push(createMedianFin(medianMeshes[`${prefix}_membrane`],medianMeshes[`${prefix}_branched_rays`],{kind}));
  }
  joints.sort((a,b)=>a.name.localeCompare(b.name));
  const rest=joints.map(bone=>bone.quaternion.clone()),facialRig=createFacialRig(asset);
  const swimmer=createSwimmer(random,{turnControllerFactory}),fish=swimmer.fish;
  const placement=massProfile?createMassPlacement({...createAxialFrame(asset),profile:massProfile}):null;
  const pairedControllers=Object.fromEntries(['pectoral','pelvic'].map(kind=>[kind,
    Object.fromEntries([-1,1].map(side=>[side,createFinController(side,kind==='pectoral')]))]));
  const caudalController=createCaudalController();
  const medianControllers=Object.fromEntries(['dorsal','anal'].map(kind=>[kind,createMedianController(kind)]));
  let poseKey=0,disposed=false;
  const rootOffset=new Vector3();
  asset.scale.multiplyScalar(scale);
  const physicalScale=asset.scale.x;
  const maneuverFootprint=createManeuverFootprint(asset,{massProfile});swimmer.footprint(maneuverFootprint.bounds);
  const supports=createSupportArticulation(asset);
  for(const geometry of supports.ownedGeometries)ownedGeometries.add(geometry);
  function observe(sample){
    for(const family of Object.values(pairedControllers))for(const controller of Object.values(family))controller.sample(sample,poseKey);
    caudalController.sample(sample,poseKey);
    for(const controller of Object.values(medianControllers))controller.sample(sample,poseKey);
  }
  function pose(sample=fish){
    applySwimPose(asset,joints,rest,sample,{placement});
    // applySwimPose's reaction is measured in unscaled animal coordinates.
    // Convert that translation to this instance's physical uniform scale.
    if(!placement){
      rootOffset.set(sample.x,sample.y,sample.z);
      asset.position.sub(rootOffset).multiplyScalar(physicalScale).add(rootOffset);asset.updateMatrixWorld(true);
    }
    facialRig.pose(sample);
    supports.pose();
    return asset;
  }
  function updateVisual(sample=fish){
    const motion=Object.fromEntries(Object.entries(pairedControllers).map(([kind,family])=>[kind,
      Object.fromEntries(Object.entries(family).map(([side,controller])=>[side,controller.sample(sample,poseKey)]))]));
    const caudalMotion=caudalController.sample(sample,poseKey);
    for(const fin of fins){
      if(fin.paired)deformFinGeometry(fin.binding,motion[fin.pectoral?'pectoral':'pelvic'],fin.points.array);
      else deformCaudalGeometry(fin.binding,caudalMotion,fin.points.array);
      fin.points.needsUpdate=true;fin.geometry.computeVertexNormals();fin.reconcileNormals?.();
    }
    for(const fin of medianFins)deformMedianFin(fin,medianControllers[fin.kind].sample(sample,poseKey));
    for(const {geometry,points,base} of barbels){
      const rootX=base[0],extent=Math.max(.001,Math.abs(base[(points.count-16)*3]-rootX)),age=sample.age??0;
      for(let i=0;i<points.count;i++){
        const x=base[i*3],y=base[i*3+1],z=base[i*3+2],s=Math.max(0,Math.min(1,Math.abs(x-rootX)/extent));
        points.setXYZ(i,x+.001*s*s*Math.sin(age*2.1-s),y+.0018*s*s*Math.sin(age*3.2-s*1.7),z+.0022*s*s*Math.sin(age*2.7-s*2));
      }
      points.needsUpdate=true;geometry.computeVertexNormals();
    }
    return asset;
  }
  function reset(){
    swimmer.reset();Object.assign(fish,initialPose,{bodyScale:physicalScale});poseKey++;observe(fish);pose();updateVisual();return fish;
  }
  reset();
  const containment=createContainment(asset);
  function contain(){
    let corrected=false;
    for(let i=0;i<3;i++){
      if(!swimmer.contain(containment.correction()))break;
      corrected=true;pose();
      for(const fin of medianFins)deformMedianFin(fin,medianControllers[fin.kind].sample(fish,poseKey));
    }
    return corrected;
  }
  return {asset,fish,swimmer,joints,facialRig,supports,containment,fins,medianFins,anatomy,scales,finSupports,
    pairedControllers,caudalController,medianControllers,ownedGeometries,ownedSkeletons,
    get behavior(){return swimmer.behavior;},pose,updateVisual,contain,reset,
    step(elapsed,{follow,flick}={}){
      if(disposed)return;
      if(follow!==undefined)swimmer.follow(follow);
      if(flick!==undefined)swimmer.flick(flick);
      swimmer.step(elapsed,observe);pose();updateVisual();contain();
    },
    setInspection(active,wireMaterial){
      for(const mesh of anatomy)mesh.visible=Boolean(active);
      for(const mesh of scales)mesh.visible=!active;
      for(const [mesh,original] of materials)if(!anatomy.includes(mesh))mesh.material=active&&wireMaterial?wireMaterial:original;
    },
    dispose(){
      if(disposed)return;disposed=true;asset.removeFromParent();
      for(const geometry of ownedGeometries)geometry.dispose();
      for(const skeleton of ownedSkeletons)skeleton.dispose();
    },
  };
}
