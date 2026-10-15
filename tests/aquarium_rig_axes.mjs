import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Quaternion} from '../static/vendor/three.webgpu.js';
import {applySwimPose} from '../static/aquarium-rig.js';
import {referencePose} from '../static/aquarium-motion.js';

async function geometry(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'AxisGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const joints=[];asset.traverse(object=>{if(object.isBone&&object.name.startsWith('spine_'))joints.push(object);});
  joints.sort((a,b)=>a.name.localeCompare(b.name));
  const body=asset.getObjectByName('Koi_skin'),positions=body.geometry.attributes.position;
  let surface=0,nearest=Infinity,head=0;
  for(let i=0;i<positions.count;i++){
    const distance=(positions.getX(i)-.2)**2+positions.getY(i)**2+(positions.getZ(i)-.08)**2;
    if(distance<nearest){nearest=distance;surface=i;}
    if(positions.getX(i)<positions.getX(head))head=i;
  }
  assert.equal(joints.length,34,'the axis probe exercises the exported articulated column');
  return {asset,joints,rest:joints.map(bone=>bone.quaternion.clone()),body,surface,head};
}

async function assertTwistAxes(pose){
  const {asset,joints,rest,body,surface,head}=await geometry();
  const report={maximumCenterlineShift:0,maximumTangentAngle:0,minimumSkinMotion:Infinity,maximumSkullShift:0};
  const snapshot=fish=>{
    pose(asset,joints,rest,fish);body.skeleton.update();
    return {centers:joints.map(bone=>bone.getWorldPosition(new Vector3())),
      skull:joints[0].getWorldQuaternion(new Quaternion()).normalize(),
      head:body.getVertexPosition(head,new Vector3()).applyMatrix4(body.matrixWorld),
      skin:body.getVertexPosition(surface,new Vector3()).applyMatrix4(body.matrixWorld)};
  };
  // A pure roll must preserve each connected segment's direction even when
  // that segment already carries lateral/vertical bending and a rest arch.
  for(const combined of [false,true])for(const sign of [-1,1]){
    const fish={...referencePose,x:.2,y:.8,z:-.2,yaw:.7,pitch:.23,bank:.12,phase:1.3,
      drive:combined?.25:0,stroke:combined?.8:0,yawRate:combined?.4:0,pitchRate:combined?.2:0,
      lateralGain:combined?1:0,verticalGain:combined?1:0,torsionDrive:sign*.28};
    const before=snapshot({...fish,twistGain:0}),after=snapshot({...fish,twistGain:1});
    after.centers.forEach((point,i)=>{
      report.maximumCenterlineShift=Math.max(report.maximumCenterlineShift,point.distanceTo(before.centers[i]));
      if(i){
        const a=point.clone().sub(after.centers[i-1]),b=before.centers[i].clone().sub(before.centers[i-1]);
        report.maximumTangentAngle=Math.max(report.maximumTangentAngle,a.angleTo(b));
      }
    });
    report.maximumSkullShift=Math.max(report.maximumSkullShift,after.head.distanceTo(before.head));
    assert.ok(after.skull.angleTo(before.skull)<1e-6,'axial twist cannot rotate the rigid skull');
    report.minimumSkinMotion=Math.min(report.minimumSkinMotion,after.skin.distanceTo(before.skin));
  }
  assert.ok(report.maximumCenterlineShift<1e-6,'pure twist preserves the actual connected centerline, including combined bends');
  assert.ok(report.maximumTangentAngle<1e-5,'pure twist does not add an unintended lateral or vertical bend');
  assert.ok(report.maximumSkullShift<1e-6,'twist leaves the actual skinned head attached and stationary');
  assert.ok(report.minimumSkinMotion>.004,'the real skin rotates around the spine rather than disabling torsion');
  return report;
}

const report=await assertTwistAxes(applySwimPose);
// Restore the former animal-X twist in memory. The same exported geometry
// invariant must reject it; no working-tree file is changed for mutation.
const moduleURL=new URL('../static/aquarium-rig.js',import.meta.url);
const source=await readFile(moduleURL,'utf8');
const mutated=source.replace('twist.setFromAxisAngle(bone.userData.bodyTangent,f.twist)',
  'twist.setFromAxisAngle(axis,f.twist)');
assert.notEqual(mutated,source,'the axis mutation was applied');
const resolved=mutated.replace(/from '(\.[^']+)'/g,(_,specifier)=>`from '${new URL(specifier,moduleURL).href}'`);
const oldAxis=await import(`data:text/javascript;base64,${Buffer.from(resolved).toString('base64')}`);
await assert.rejects(()=>assertTwistAxes(oldAxis.applySwimPose),/pure twist preserves the actual connected centerline/,
  'the actual-GLB regression rejects the former body-X torsion axis');
console.log(`Rig axes passed: ${JSON.stringify(report)}; animal-X mutation rejected`);
