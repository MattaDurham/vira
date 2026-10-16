import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {performance} from 'node:perf_hooks';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture} from '../static/vendor/three.webgpu.js';
import {applySwimPose} from '../static/aquarium-rig.js';
import * as current from '../static/aquarium-median.js';

// Independent reference: the former reconstruction evaluates the membrane
// frame separately for every actual ray vertex. Keep its arithmetic intact.
const uncachedRayTissue=`function rayTissue(family){
  const surface=family.bindings[0].target,binding=family.bindings[1],values=binding.material,output=binding.target;
  for(let i=0;i<binding.count;i++){
    const o=i*6,a=values[o]*3,b=values[o+1]*3,c=values[o+2]*3;
    const u=[surface[b]-surface[a],surface[b+1]-surface[a+1],surface[b+2]-surface[a+2]];
    const v=[surface[c]-surface[a],surface[c+1]-surface[a+1],surface[c+2]-surface[a+2]],normal=normalize(cross(u,v));
    for(let k=0;k<3;k++)output[i*3+k]=surface[a+k]+values[o+3]*u[k]+values[o+4]*v[k]+values[o+5]*normal[k];
  }
}`;
const moduleURL=new URL('../static/aquarium-median.js',import.meta.url);

function replaceRayTissue(source,replacement){
  const start=source.indexOf('function rayTissue(family){'),end=source.indexOf('\nfunction posedNormals(binding){',start);
  assert.ok(start>=0&&end>start,'the independent former ray reconstruction has a bounded module seam');
  return source.slice(0,start)+replacement+'\n'+source.slice(end);
}

async function loadModule(source){
  const absolute=source.replace(/from '(\.[^']+)'/g,(_,path)=>`from '${new URL(path,moduleURL)}'`);
  return import(`data:text/javascript;base64,${Buffer.from(absolute).toString('base64')}`);
}

async function geometry(api,bytes){
  const loader=new GLTFLoader().register(()=>({name:'MedianCacheGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const joints=[];asset.traverse(node=>{if(node.isBone&&node.name.startsWith('spine_'))joints.push(node);});
  joints.sort((a,b)=>a.name.localeCompare(b.name));
  assert.equal(joints.length,34,'the cache is checked on the actual articulated animal');
  const rest=joints.map(joint=>joint.quaternion.clone());
  const families=['Dorsal','Anal'].map(prefix=>({
    controller:api.createMedianController(prefix.toLowerCase()),
    field:api.createMedianFin(asset.getObjectByName(`${prefix}_membrane`),asset.getObjectByName(`${prefix}_branched_rays`),{kind:prefix.toLowerCase()})
  }));
  return {asset,joints,rest,families};
}

function equalValues(actual,expected,label){
  assert.equal(actual.length,expected.length,`${label} retains every material vertex`);
  const left=Buffer.from(actual.buffer,actual.byteOffset,actual.byteLength),right=Buffer.from(expected.buffer,expected.byteOffset,expected.byteLength);
  assert.ok(left.equals(right),`${label} is bit-identical to the uncached actual-geometry reconstruction`);
}

function samples(){
  const result=[];
  for(const sign of [1,-1]){
    for(let frame=0;frame<20;frame++){
      const t=frame/19,load=Math.sin(Math.PI*t),age=frame/120;
      result.push({key:`live-${sign}`,fish:{x:.2+frame*.003,y:.8,z:-.1,yaw:sign*.4*t,pitch:.18*Math.sin(t*4),bank:sign*.12*Math.sin(t*5),
        age,phase:t*6,stroke:.4,drive:.15,muscle1:.4,muscle2:.4,muscle3:.4,yawRate:sign*Math.sin(t*5),finSteer:sign*.6*load,
        yawLag1:sign*.03*load,yawLag2:sign*.06*load,yawLag3:sign*.09*load,
        pitchLag1:.02*load,pitchLag2:.04*load,pitchLag3:.06*load,bankLag1:.01*load,bankLag2:.02*load,bankLag3:.03*load,
        turnAnterior:sign*2.4*load,turnTrunk:sign*2.2*load,turnPosterior:sign*1.6*load,
        turnRecruitment:.25*load,turnRecruitment1:.25*load,turnRecruitment2:.25*load,turnRecruitment3:.25*load}});
    }
  }
  // Same timestamp with changed geometry, backwards time, a pause/resume gap,
  // and held/live transitions must all refresh the current material basis.
  for(const [age,key,bend,held] of [[.1,'held',.8,true],[.1,'held',-1.1,true],[.05,'held',.4,true],[2,'live-reset',-1.8,false],[2,'live-reset',1.8,false],[0,'live-reset',0,false]]){
    result.push({key,fish:{x:0,y:.8,z:0,yaw:bend*.2,pitch:bend*.08,bank:-bend*.05,age,phase:age*3,stroke:.2,drive:.1,
      yawRate:bend*.4,finSteer:bend*.2,poseHistory:!held,turnAnterior:bend,turnTrunk:bend*.8,turnPosterior:bend*.5}});
  }
  return result;
}

export async function assertMedianCache({candidate=current,reference,source}={}){
  source??=await readFile(moduleURL,'utf8');
  reference??=await loadModule(replaceRayTissue(source,uncachedRayTissue));
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const old=await geometry(reference,bytes),updated=await geometry(candidate,bytes),poses=samples();
  const report={poses:poses.length,families:2,comparedScalars:0,uncachedMs:0,cachedMs:0,frames:[]};
  for(const [poseIndex,{fish,key}] of poses.entries()){
    for(const model of [old,updated])applySwimPose(model.asset,model.joints,model.rest,fish);
    for(let index=0;index<old.families.length;index++){
      const a=old.families[index],b=updated.families[index];
      const originalMotion=a.controller.sample(fish,key),cachedMotion=b.controller.sample(fish,key);
      assert.deepEqual(cachedMotion,originalMotion,'controller history is unchanged by the geometry cache');
      // Alternate measurement order to avoid consistently favoring the second
      // call. These diagnostics do not enforce a machine-dependent time limit.
      for(const [api,family,motion,name] of (poseIndex%2?
        [[candidate,b.field,cachedMotion,'cachedMs'],[reference,a.field,originalMotion,'uncachedMs']]:
        [[reference,a.field,originalMotion,'uncachedMs'],[candidate,b.field,cachedMotion,'cachedMs']])){
        const start=performance.now();api.deformMedianFin(family,motion);
        if(poseIndex>=4)report[name]+=performance.now()-start;
      }
      assert.equal(b.field.minimumSkinDeterminant,a.field.minimumSkinDeterminant,'skin inversion detection is unchanged');
      for(let binding=0;binding<2;binding++){
        const before=a.field.bindings[binding],after=b.field.bindings[binding],label=`pose ${poseIndex}, ${b.field.kind}, binding ${binding}`;
        for(const name of ['posed','target','normals']){
          equalValues(after[name],before[name],`${label} ${name}`);report.comparedScalars+=after[name].length;
        }
        for(const name of ['position','normal']){
          equalValues(after.mesh.geometry.attributes[name].array,before.mesh.geometry.attributes[name].array,`${label} uploaded ${name}`);
          report.comparedScalars+=after.mesh.geometry.attributes[name].array.length;
        }
      }
      assert.equal(b.field.maximumTubeCorrection,a.field.maximumTubeCorrection,'actual tube length/contact correction is unchanged');
    }
  }
  for(const {field} of updated.families){
    const rays=field.bindings[1],frames=rays.materialFrames;
    if(frames)report.frames.push({kind:field.kind,rayVertices:rays.count,triangleFrames:frames.indices.length/3,bytes:frames.indices.byteLength+frames.offsets.byteLength+frames.values.byteLength});
  }
  return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const source=await readFile(moduleURL,'utf8'),report=await assertMedianCache({source});
  const stale=source.replace('i<indices.length;i+=3){','!binding.materialFrames.stale&&i<indices.length;i+=3){')
    .replace('const o=i*6,f=offsets[i];','binding.materialFrames.stale=true;const o=i*6,f=offsets[i];');
  const wrongTriangle=source.replace('const o=i*6,f=offsets[i];','const o=i*6,f=0;');
  assert.notEqual(stale,source,'the frozen material-frame mutation is connected');
  assert.notEqual(wrongTriangle,source,'the wrong-triangle mutation is connected');
  const staleModule=await loadModule(stale),wrongTriangleModule=await loadModule(wrongTriangle);
  await assert.rejects(()=>assertMedianCache({candidate:staleModule,source}),/bit-identical/);
  await assert.rejects(()=>assertMedianCache({candidate:wrongTriangleModule,source}),/bit-identical/);
  console.log(`Actual median cache passed: ${JSON.stringify(report)}; stale-frame and wrong-triangle mutations rejected`);
}
