import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Quaternion,Matrix4,Group} from '../static/vendor/three.webgpu.js';
import {createKoiInstance} from '../static/aquarium-fish-instance.js';
import {createAxialFrame} from '../static/aquarium-axial-frame.js';
import {createRapierChain} from '../static/aquarium-articulated.js';
import {createRapierRenderBridge} from '../static/aquarium-articulated-render.js';

const quaternionError=(a,b)=>2*Math.acos(Math.min(1,Math.abs(a.dot(b))/(a.length()*b.length())));
const copyPacket=packet=>({positions:packet.positions.map(p=>p.clone()),rotations:packet.rotations.map(q=>q.clone())});
const packetJSON=packet=>JSON.stringify({positions:packet.positions.map(p=>p.toArray()),rotations:packet.rotations.map(q=>q.toArray())});
const facialNames=['jaw_lower','jaw_upper','operculum_L','operculum_R','eye_L','eye_R'];
const rigidPacket=(sampler,R=new Quaternion(),T=new Vector3())=>({positions:sampler.centers.map(p=>p.clone().applyQuaternion(R).add(T)),rotations:sampler.rest.map(q=>R.clone().multiply(q))});

export async function assertArticulatedRender(create=createRapierRenderBridge){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const source=(await new GLTFLoader().register(()=>({name:'IndependentRapierSkin',loadTexture:()=>Promise.resolve(new Texture())})).parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;
  const sampler=createAxialFrame(source),sourceSkin=source.getObjectByName('Koi_skin'),sourceBoneState=sampler.joints.map(b=>({p:b.position.toArray(),q:b.quaternion.toArray()}));
  const profile=JSON.parse(await readFile(new URL('../static/aquarium/body-mass.json',import.meta.url),'utf8'));
  assert.equal(createHash('sha256').update(bytes).digest('hex'),profile.assetSHA256,'the authored mass profile belongs to this actual uploaded asset');
  const chain=await createRapierChain(sampler,profile,{naturalFrequency:30,dt:1/240});chain.configure('active');
  try{
  chain.setWholeVelocity(new Vector3(.023,-.011,.017),new Vector3(.15,.42,-.11));
  const physicalPackets=[];
  for(let i=0;i<240;i++){
    const t=i/240,load=Math.sin(Math.min(1,t/.7)*Math.PI/2)**2;
    chain.setTargets({phase:3.2*t,drive:.18,stroke:.2,muscle1:.2,muscle2:.18,muscle3:.15,
      turnAnterior:1.8*load,turnTrunk:1.6*load,turnPosterior:1.4*load,torsionDrive:.15*Math.sin(t*2)});
    chain.step();if(i===0||(i+1)%60===0)physicalPackets.push({age:chain.age,packet:chain.boneTransforms()});
  }
  const first=physicalPackets[0].packet,last=physicalPackets.at(-1).packet;
  assert.ok(last.positions[0].distanceTo(first.positions[0])>.001&&quaternionError(last.rotations[0],first.rotations[0])>.01,
    'the physical trajectory includes actual head translation and rotation');
  const headReaction=last.rotations[0].clone().multiply(sampler.rest[0].clone().invert());
  const tailReaction=last.rotations[33].clone().multiply(sampler.rest[33].clone().invert());
  assert.ok(quaternionError(headReaction,tailReaction)>.02,'the physical trajectory includes actual relative body bending');
  const report={scales:[],physicsFrames:physicalPackets.length,vertices:0,maximumBonePositionError:0,maximumBoneOrientationError:0,
    maximumSkinPositionError:0,maximumWholeSkinRigidCovarianceError:0,maximumFacialPositionError:0,maximumFacialOrientationError:0,maximumCranialDistanceError:0,
    deliberateJointGap:.012,waterBounds:'Not tested; this is a transformation/mapping proof only.'};
  const desiredP=new Vector3(),actualP=new Vector3(),desiredQ=new Quaternion(),actualQ=new Quaternion(),point=new Vector3(),bind=new Vector3(),sum=new Vector3();
  const boneByName=new Map(sampler.joints.map((bone,i)=>[bone.name,i]));
  for(const scale of [1,.46]){
    const instance=createKoiInstance(source,{scale}),lane=new Group();lane.position.set(.28,.81,-.31);lane.rotation.set(.08,-.29,.04);lane.updateMatrixWorld(true);lane.add(instance.asset);lane.updateMatrixWorld(true);
    let bridge;
    try{
    bridge=create(instance,{sampler});const laneRotation=lane.getWorldQuaternion(new Quaternion()),localScale=new Vector3(scale,scale,scale);
    const visual={...instance.fish,age:.7,phase:0,stroke:0,muscle1:0,muscle2:0,muscle3:0,speed:0,forwardSpeed:0,
      yawRate:0,pitchRate:0,bankRate:0,pitchFin:0,rollFin:0,bank:0,finSteer:0};
    const matrices=sourceSkin.skeleton.bones.map(()=>new Matrix4());
    function verify(packet,age){
      const before=packetJSON(packet);bridge.pose(packet,{age,visualFish:visual});assert.equal(packetJSON(packet),before,'the render bridge cannot mutate the physical packet');
      assert.deepEqual(instance.asset.scale.toArray(),[scale,scale,scale],'render mapping preserves physical size');
      for(let i=0;i<34;i++){
        desiredP.copy(packet.positions[i]).multiplyScalar(scale).applyMatrix4(lane.matrixWorld);instance.joints[i].getWorldPosition(actualP);
        const error=actualP.distanceTo(desiredP);report.maximumBonePositionError=Math.max(report.maximumBonePositionError,error);
        assert.ok(error<1e-7,'actual GLB bone origins reproduce every physical solver position, including joint drift');
        desiredQ.copy(laneRotation).multiply(packet.rotations[i]);instance.joints[i].getWorldQuaternion(actualQ);
        const angular=quaternionError(actualQ,desiredQ);report.maximumBoneOrientationError=Math.max(report.maximumBoneOrientationError,angular);
        assert.ok(angular<1e-6,'the complete physical quaternion reaches the actual GLB bone');
      }
      for(let i=0;i<sourceSkin.skeleton.bones.length;i++){
        const j=boneByName.get(sourceSkin.skeleton.bones[i].name);if(j===undefined)continue;
        matrices[i].compose(packet.positions[j].clone().multiplyScalar(scale),packet.rotations[j],localScale)
          .premultiply(lane.matrixWorld).multiply(sourceSkin.skeleton.boneInverses[i]);
      }
      const skin=instance.asset.getObjectByName('Koi_skin'),a=sourceSkin.geometry.attributes;skin.skeleton.update();
      for(let i=0;i<a.position.count;i++){
        if([0,1,2,3].some(slot=>a.skinWeight.getComponent(i,slot)>0&&!boneByName.has(sourceSkin.skeleton.bones[a.skinIndex.getComponent(i,slot)].name)))continue;
        bind.fromBufferAttribute(a.position,i).applyMatrix4(sourceSkin.bindMatrix);sum.set(0,0,0);
        for(let slot=0;slot<4;slot++){
          const weight=a.skinWeight.getComponent(i,slot);if(!weight)continue;
          const joint=a.skinIndex.getComponent(i,slot);assert.ok(boneByName.has(sourceSkin.skeleton.bones[joint].name));
          sum.addScaledVector(point.copy(bind).applyMatrix4(matrices[joint]),weight);
        }
        skin.getVertexPosition(i,actualP).applyMatrix4(skin.matrixWorld);const error=actualP.distanceTo(sum);
        report.maximumSkinPositionError=Math.max(report.maximumSkinPositionError,error);
        assert.ok(error<2e-6,'actual uploaded skin uses the independent physical target palette, not a second kinematic pose');report.vertices++;
      }
      const cranial=instance.joints.slice(0,5);
      for(let i=0;i<5;i++)for(let j=i+1;j<5;j++){
        const actual=cranial[i].getWorldPosition(new Vector3()).distanceTo(cranial[j].getWorldPosition(new Vector3()));
        const expected=packet.positions[i].distanceTo(packet.positions[j])*scale;
        const error=Math.abs(actual-expected);report.maximumCranialDistanceError=Math.max(report.maximumCranialDistanceError,error);
        assert.ok(error<1e-7,'all five cranial stations share the actual physical rigid-head placement');
      }
    }
    verify(rigidPacket(sampler),.7);
    const actualSkin=instance.asset.getObjectByName('Koi_skin'),neutralSkin=new Float64Array(actualSkin.geometry.attributes.position.count*3);
    actualSkin.skeleton.update();for(let i=0;i<actualSkin.geometry.attributes.position.count;i++)actualSkin.getVertexPosition(i,point).applyMatrix4(actualSkin.matrixWorld).toArray(neutralSkin,i*3);
    const faceNeutral=facialNames.map(name=>{const bone=instance.asset.getObjectByName(name);return {name,p:bone.getWorldPosition(new Vector3()),q:bone.getWorldQuaternion(new Quaternion())};});
    const rigidQ=new Quaternion().setFromAxisAngle(new Vector3(0,1,0),.71),rigidT=new Vector3(.13,-.07,.1);
    verify(rigidPacket(sampler,rigidQ,rigidT),.7);
    const displayRigidMatrix=new Matrix4().compose(rigidT.clone().multiplyScalar(scale),rigidQ,new Vector3(1,1,1)).premultiply(lane.matrixWorld).multiply(lane.matrixWorld.clone().invert());
    actualSkin.skeleton.update();
    for(let i=0;i<actualSkin.geometry.attributes.position.count;i++){
      desiredP.fromArray(neutralSkin,i*3).applyMatrix4(displayRigidMatrix);actualSkin.getVertexPosition(i,actualP).applyMatrix4(actualSkin.matrixWorld);
      const error=actualP.distanceTo(desiredP);report.maximumWholeSkinRigidCovarianceError=Math.max(report.maximumWholeSkinRigidCovarianceError,error);
      assert.ok(error<2e-6,'the entire actual skin, including facial-weighted tissue, follows physical cranial rigid motion once');
    }
    const displayRigidQ=laneRotation.clone().multiply(rigidQ).multiply(laneRotation.clone().invert());
    for(const neutral of faceNeutral){
      const bone=instance.asset.getObjectByName(neutral.name);
      desiredP.copy(neutral.p).applyMatrix4(lane.matrixWorld.clone().invert()).multiplyScalar(1/scale)
        .applyQuaternion(rigidQ).add(rigidT).multiplyScalar(scale).applyMatrix4(lane.matrixWorld);
      bone.getWorldPosition(actualP);const pe=actualP.distanceTo(desiredP);report.maximumFacialPositionError=Math.max(report.maximumFacialPositionError,pe);
      desiredQ.copy(displayRigidQ).multiply(neutral.q);bone.getWorldQuaternion(actualQ);const qe=quaternionError(actualQ,desiredQ);
      report.maximumFacialOrientationError=Math.max(report.maximumFacialOrientationError,qe);
      assert.ok(pe<1e-7&&qe<1e-6,'breathing jaws, opercula and eyes follow the real cranial frame');
    }
    for(const {age,packet} of physicalPackets)verify(packet,age);
    const drift=copyPacket(physicalPackets.at(-1).packet);drift.positions[16].z+=.012;verify(drift,1.1);
    const observed=bridge.measure();assert.ok(observed.physicsMaximumAnchorVectorError>.01,'the deliberately supplied joint gap is reported rather than projected away');
    report.scales.push({scale,bridge:structuredClone(observed)});
    }finally{bridge?.dispose();instance.dispose();}
  }
  assert.deepEqual(sampler.joints.map(b=>({p:b.position.toArray(),q:b.quaternion.toArray()})),sourceBoneState,'rendering clones never mutates the pristine source rig');
  return report;
  }finally{chain.free();}
}

export async function assertArticulatedRenderMutations(){
  const mutations={
    extraCentering(instance,options){
      const bridge=createRapierRenderBridge(instance,options),pose=bridge.pose;
      bridge.pose=(packet,args)=>{const result=pose(packet,args);instance.asset.position.x+=.023;instance.asset.updateMatrixWorld(true);return result;};return bridge;
    },
    doubleHeadRotation(instance,options){
      const bridge=createRapierRenderBridge(instance,options),pose=bridge.pose;
      bridge.pose=(packet,args)=>{
        const result=pose(packet,args);instance.asset.quaternion.multiply(packet.rotations[0].clone().multiply(options.sampler.rest[0].clone().invert()));
        instance.asset.updateMatrixWorld(true);return result;
      };return bridge;
    },
    projectedJointGap(instance,options){
      const bridge=createRapierRenderBridge(instance,options),pose=bridge.pose,s=options.sampler;
      bridge.pose=(packet,args)=>{
        const fixed=copyPacket(packet);
        for(let i=1;i<34;i++){
          const rotation=fixed.rotations[i-1].clone().multiply(s.rest[i-1].clone().invert());
          fixed.positions[i].copy(s.centers[i]).sub(s.centers[i-1]).applyQuaternion(rotation).add(fixed.positions[i-1]);
        }
        return pose(fixed,args);
      };return bridge;
    },
    wrongFacialFrame(instance,options){
      const bridge=createRapierRenderBridge(instance,options),pose=bridge.pose;
      bridge.pose=(packet,args)=>{
        const result=pose(packet,args),get=instance.asset.getWorldQuaternion;
        try{instance.asset.getWorldQuaternion=out=>out.identity();instance.facialRig.pose(result.sample);}
        finally{instance.asset.getWorldQuaternion=get;}
        instance.asset.updateMatrixWorld(true);return result;
      };return bridge;
    },
    sourceSkinPalette(instance,options){
      const bridge=createRapierRenderBridge(instance,options),pose=bridge.pose;
      let source=options.sampler.joints[0];while(source.parent)source=source.parent;
      const sourceSkin=source.getObjectByName('Koi_skin');assert.ok(sourceSkin);
      bridge.pose=(packet,args)=>{const result=pose(packet,args);instance.asset.getObjectByName('Koi_skin').skeleton=sourceSkin.skeleton;return result;};return bridge;
    },
  };
  const rejected=[];
  for(const [name,create] of Object.entries(mutations)){
    await assert.rejects(()=>assertArticulatedRender(create),{name:'AssertionError'},`${name} must fail the actual-geometry oracle`);rejected.push(name);
  }
  return rejected;
}

if(process.argv[1]&&pathToFileURL(process.argv[1]).href===import.meta.url){
  const report=await assertArticulatedRender(),mutations=await assertArticulatedRenderMutations();
  console.log(JSON.stringify({physicsFrames:report.physicsFrames,vertices:report.vertices,
    maximumBonePositionError:report.maximumBonePositionError,maximumBoneOrientationError:report.maximumBoneOrientationError,
    maximumSkinPositionError:report.maximumSkinPositionError,maximumWholeSkinRigidCovarianceError:report.maximumWholeSkinRigidCovarianceError,
    maximumFacialPositionError:report.maximumFacialPositionError,maximumFacialOrientationError:report.maximumFacialOrientationError,
    rejectedMutations:mutations}));
}
