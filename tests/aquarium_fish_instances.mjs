import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Triangle,Vector3} from '../static/vendor/three.webgpu.js';
import {createKoiInstance} from '../static/aquarium-fish-instance.js';
import {createSwimmer} from '../static/aquarium-motion.js';
import {createFinController} from '../static/aquarium-fins.js';
import {createStudyRandom} from '../static/aquarium-study.js';
import {waterMin,waterMax,tankCenterZ} from '../static/aquarium-space.js';

const mutable=name=>/^(Caudal|Pectoral|Pelvic|Dorsal|Anal)_/.test(name)||name.includes('sensory_barbel')||name.startsWith('Anatomy_dorsal_pterygiophore');
const near=(a,b,tolerance=1e-10)=>Math.abs(a-b)<=tolerance;
const snapshot=asset=>{
  const values=[];asset.traverse(node=>{
    if(node.isBone)values.push(node.position.toArray(),node.quaternion.toArray());
    if(node.isMesh&&mutable(node.name))values.push(Array.from(node.geometry.attributes.position.array),Array.from(node.geometry.attributes.normal.array));
  });return values;
};
const meshMap=asset=>{const meshes=new Map();asset.traverse(mesh=>{if(mesh.isMesh)meshes.set(mesh.name,mesh);});return meshes;};
const vertex=(array,i)=>new Vector3().fromArray(array,i*3);

function assertPairedJoin(instance){
  let maximum=0;const closest=new Vector3();
  for(const pectoral of [true,false]){
    const family=instance.fins.filter(fin=>fin.paired&&fin.pectoral===pectoral),surfaces={};
    for(const fin of family.filter(fin=>fin.mesh.name.includes('_membrane'))){
      const base=fin.points.array,index=fin.geometry.index.array,side=Math.sign(fin.base[2]);
      const triangles=[];
      for(let i=0;i<index.length;i+=3){
        const triangle=new Triangle(...[0,1,2].map(k=>vertex(base,index[i+k])));
        if(triangle.getArea()>1e-14)triangles.push(triangle);
      }
      surfaces[side]=triangles;
      assert.ok(fin.points.array.slice(0,3).every((value,i)=>near(value,fin.base[i],1e-7)),
        'each independent paired membrane retains its actual material attachment');
    }
    const rays=family.find(fin=>fin.mesh.name.includes('rays'));
    for(let i=0;i<rays.points.count;i+=Math.ceil(rays.points.count/96)){
      const point=vertex(rays.points.array,i),side=Math.sign(rays.base[i*3+2]);let nearest=Infinity;
      for(const triangle of surfaces[side]){triangle.closestPointToPoint(point,closest);nearest=Math.min(nearest,point.distanceToSquared(closest));}
      maximum=Math.max(maximum,Math.sqrt(nearest));
    }
  }
  assert.ok(maximum<.001,`the cloned embedded paired rays stay in their tissue (${maximum*1000} mm)`);
  return maximum;
}

export function assertNeighborAwareness(create=createSwimmer){
  const dt=1/120,origin={x:0,y:.84,z:tankCenterZ,yaw:0},destination={x:-.85,y:.84,z:tankCenterZ};
  const make=(bodyScale=.42)=>{
    const swimmer=create(createStudyRandom(731),{bodyScale,id:'self'});
    Object.assign(swimmer.fish,origin);swimmer.navigate(destination);return swimmer;
  };
  const peer=(sign=1,distance=.22)=>({id:'other',x:0,y:.84,z:tankCenterZ+sign*distance,
    vx:0,vy:0,vz:0,radius:.22*.42,length:1.5*.42,yaw:0});
  const advance=(swimmer,ticks)=>{
    for(let i=0;i<ticks;i++){
      const previous={...swimmer.fish};swimmer.step(dt);
      for(const value of Object.values(swimmer.fish))if(typeof value==='number')assert.ok(Number.isFinite(value),'avoidance keeps every numeric motion state finite');
      assert.ok(near(swimmer.fish.age,previous.age+dt),'awareness preserves the physical clock');
      assert.ok(Math.hypot(swimmer.fish.x-previous.x,swimmer.fish.y-previous.y,swimmer.fish.z-previous.z)<.01,
        'neighbor avoidance accelerates the swimmer rather than teleporting its root');
    }
  };
  const baseline=make(),left=make(),right=make();
  for(const [swimmer,sign] of [[left,-1],[right,1]]){
    const before={...swimmer.fish};swimmer.neighbors([peer(sign)]);
    assert.deepEqual(swimmer.fish,before,'supplying a neighbor snapshot cannot directly move a fish');
  }
  advance(baseline,60);advance(left,60);advance(right,60);
  assert.ok(left.fish.yaw>.10&&right.fish.yaw<-.10,'a neighbor on either flank elicits a turn away from that flank');
  assert.ok(near(left.fish.yaw,-right.fish.yaw,1e-8)&&near(left.fish.yawRate,-right.fish.yawRate,1e-8),
    'mirrored neighbors produce mirrored heading and angular velocity');
  assert.ok(near(left.fish.drive,right.fish.drive,1e-8)&&left.fish.drive<baseline.fish.drive,
    'both flank encounters slow the fish by the same finite amount');
  assert.equal(left.behavior.intent,'navigate');assert.equal(right.behavior.intent,'navigate');
  assert.equal(left.behavior.decisions,1,'avoidance does not replace the intrinsic navigation decision');

  const copied=make(),control=make(),record=peer(-1),records=[record];
  copied.neighbors(records);control.neighbors([{...record}]);
  Object.assign(record,{x:NaN,y:-100,z:100,vx:1e6,vy:-1e6,vz:1e6,radius:99,length:99,yaw:Math.PI});records.length=0;
  advance(copied,60);advance(control,60);
  assert.deepEqual(copied.fish,control.fish,'neighbors copies the array and every observed record rather than following mutable caller state');
  assert.ok(copied.fish.yaw>.10,'the copied encounter remains an effective steering cue');

  // Removing the temporary encounter leaves the original goal available. Verify
  // the behavioral outcome, including arrival at that same point, not a private
  // variable or merely retaining a string named "navigate".
  left.neighbors([]);
  for(let i=0;i<12*120&&left.behavior.intent==='navigate';i++)advance(left,1);
  assert.equal(left.behavior.intent,'hover','the original destination is still reached after the encounter clears');
  assert.ok(Math.hypot(left.fish.x-destination.x,left.fish.y-destination.y,left.fish.z-destination.z)<.20,
    'arrival remains at the original navigation destination');

  const small=make(.2),large=make(1),distant=peer(1,.52);
  distant.radius=.22*.33;distant.length=1.5*.33;
  small.neighbors([distant]);large.neighbors([distant]);advance(small,1);advance(large,1);
  assert.ok(Math.abs(large.fish.turnHeadingBudget)>Math.abs(small.fish.turnHeadingBudget)+.30&&large.fish.drive<small.fish.drive,
    'the same clearance recruits a larger avoidance turn for the physically larger fish');
  const narrowPeer=make(),widePeer=make();
  narrowPeer.neighbors([{...peer(1,.52),radius:.22*.15,length:1.5*.15}]);
  widePeer.neighbors([{...peer(1,.52),radius:.22,length:1.5}]);
  advance(narrowPeer,1);advance(widePeer,1);
  assert.ok(Math.abs(widePeer.fish.turnHeadingBudget)>Math.abs(narrowPeer.fish.turnHeadingBudget)+.30,
    'observed neighbor radius affects actual lateral clearance');
  const shortPeer=make(),longPeer=make(),ahead={...peer(),x:-.9,z:tankCenterZ,radius:.22};
  shortPeer.neighbors([{...ahead,length:.225}]);longPeer.neighbors([{...ahead,length:1.5}]);
  advance(shortPeer,1);advance(longPeer,1);
  assert.ok(longPeer.fish.drive<shortPeer.fish.drive*.6,'observed neighbor length affects longitudinal clearance');
  const coincident=make(.15);
  coincident.neighbors([Object.freeze({...peer(),x:0,z:tankCenterZ,vx:NaN,vy:Infinity,vz:NaN,radius:-1,length:Infinity,yaw:NaN})]);
  advance(coincident,30);

  const selfOnly=make();selfOnly.neighbors([{...peer(),id:'self'}]);advance(selfOnly,60);
  assert.deepEqual(selfOnly.fish,baseline.fish,'a configured fish id cannot become its own avoidance target');
  copied.reset();Object.assign(copied.fish,origin);copied.navigate(destination);
  const afterReset=make();advance(copied,60);advance(afterReset,60);
  assert.equal(copied.fish.bodyScale,.42);assert.equal(copied.fish.id,'self');
  assert.deepEqual(copied.fish,afterReset.fish,'reset clears observed neighbors and their motor response');
  return {mirroredYawRadians:right.fish.yaw,arrivalSeconds:left.fish.age,scaledClearance:true};
}

export async function assertFishInstances(create=createKoiInstance){
  const neighbors=assertNeighborAwareness();
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'InstanceGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:source}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const sourceMeshes=meshMap(source),before=snapshot(source),initialPose={x:-.55,y:1,z:-.40,yaw:0};
  const materialCalls=[],returnedMaterials=[];
  const a=create(source,{scale:.42,initialPose,random:createStudyRandom(731),materialFor(original,mesh){
    materialCalls.push(mesh.name);const material=original.clone();returnedMaterials.push(material);return material;
  }});
  const b=create(source,{scale:.33,initialPose:{x:.6,y:.7,z:.05,yaw:0},random:createStudyRandom(192)});
  const aMeshes=meshMap(a.asset),bMeshes=meshMap(b.asset),untouchedB=snapshot(b.asset);
  assert.equal(a.joints.length,34);assert.equal(b.facialRig.jointCount,6);
  assert.equal(a.fish.bodyScale,.42);assert.equal(b.fish.bodyScale,.33);
  assert.notEqual(a.supports,b.supports);
  assert.equal(materialCalls.length,aMeshes.size,'the caller colors every companion mesh explicitly');
  assert.notEqual(a.swimmer,b.swimmer);assert.notEqual(a.facialRig,b.facialRig);
  assert.notEqual(a.pairedControllers.pectoral[1],b.pairedControllers.pectoral[1]);
  assert.notEqual(a.caudalController,b.caudalController);assert.notEqual(a.medianControllers.dorsal,b.medianControllers.dorsal);
  const sourceBones=new Set();source.traverse(node=>{if(node.isBone)sourceBones.add(node);});
  const bBones=new Set();b.asset.traverse(node=>{if(node.isBone)bBones.add(node);});
  for(const [name,mesh] of aMeshes){
    const original=sourceMeshes.get(name),other=bMeshes.get(name);
    assert.notEqual(mesh.material,original.material,'a pigment material belongs to its companion');
    assert.equal(mesh.material.map,original.material.map,'immutable texture images are shared');
    if(mesh.isSkinnedMesh){
      assert.notEqual(mesh.skeleton,original.skeleton);assert.notEqual(mesh.skeleton,other.skeleton);
      assert.notEqual(mesh.skeleton.boneInverses,original.skeleton.boneInverses);
      for(let i=0;i<mesh.skeleton.bones.length;i++){
        const bone=mesh.skeleton.bones[i];assert.ok(!sourceBones.has(bone)&&!bBones.has(bone),'bone palettes must address only this fish');
        assert.notEqual(mesh.skeleton.boneInverses[i],original.skeleton.boneInverses[i]);
        assert.deepEqual(mesh.skeleton.boneInverses[i].elements,original.skeleton.boneInverses[i].elements,'the exported inverse bind is preserved exactly');
      }
    }
    if(mutable(name)){
      assert.notEqual(mesh.geometry,original.geometry);assert.notEqual(mesh.geometry,other.geometry);
      for(const field of ['position','normal']){
        assert.notEqual(mesh.geometry.attributes[field].array,original.geometry.attributes[field].array);
        assert.notEqual(mesh.geometry.attributes[field].array,other.geometry.attributes[field].array);
      }
    }else assert.equal(mesh.geometry,original.geometry,'immutable body, scales and anatomical geometry are shared');
  }
  const length=instance=>instance.joints[0].getWorldPosition(new Vector3()).distanceTo(instance.joints.at(-1).getWorldPosition(new Vector3()));
  assert.ok(near(length(a)/length(b),.42/.33,1e-6),'uniform scale changes actual world skeleton length');
  const scaledA=create(source,{scale:.294,initialPose});
  source.scale.setScalar(.7);
  const scaledB=create(source,{scale:.42,initialPose});
  source.scale.setScalar(1);
  assert.ok(near(scaledA.fish.bodyScale,.294)&&near(scaledB.fish.bodyScale,.294),
    'neighbor avoidance receives the complete physical scale of the rendered fish');
  for(const instance of [scaledA,scaledB]){
    Object.assign(instance.fish,{turnBend:.22,turnBend1:.22,turnBend2:.22,turnBend3:.22});instance.pose();
  }
  for(let i=0;i<scaledA.joints.length;i++){
    const first=scaledA.joints[i].getWorldPosition(new Vector3()),second=scaledB.joints[i].getWorldPosition(new Vector3());
    assert.ok(first.distanceTo(second)<1e-8,'the mass-reaction correction uses the final physical scale, including the source root');
  }
  scaledA.dispose();scaledB.dispose();
  const reference=createSwimmer(createStudyRandom(731)),expectedFin=createFinController(1,true);
  Object.assign(reference.fish,initialPose,{bodyScale:.42});expectedFin.sample(reference.fish,1);
  const goal={x:.85,y:1.16,z:-.1};a.swimmer.navigate(goal);reference.navigate(goal);
  for(let frame=0;frame<6;frame++){
    a.step(.2);reference.step(.2,sample=>expectedFin.sample(sample,1));
    assert.ok(near(a.fish.age,reference.fish.age),'the companion owns its physical clock');
    const expected=expectedFin.sample(reference.fish,1),actual=a.pairedControllers.pectoral[1].sample(a.fish,1);
    assert.ok(actual.samples.every((value,i)=>near(value,expected.samples[i])),
      'fin history observes each completed physical tick, including between display frames');
  }
  assert.deepEqual(snapshot(source),before,'animating a clone cannot pose or deform the pristine source');
  assert.deepEqual(snapshot(b.asset),untouchedB,'animating the first koi cannot change the second koi face, bones or fin buffers');
  const firstAge=a.fish.age,still=snapshot(a.asset),stillFish={...a.fish};
  a.step(0);a.updateVisual();a.pose();a.updateVisual();
  assert.equal(a.fish.age,firstAge);assert.deepEqual(a.fish,stillFish);
  assert.deepEqual(snapshot(a.asset),still,'paused redraws do not invent motion or accumulate fin deformation');
  const maximumRayGap=assertPairedJoin(a);
  for(const fin of a.medianFins){
    assert.ok(fin.minimumSkinDeterminant>0,'cloned median skin remains invertible');
    assert.ok(Number.isFinite(fin.maximumTubeCorrection),'median tissue and rays are solved in the clone own posed frame');
  }
  b.step(.1,{follow:{x:-.4,y:.7,z:0},flick:{x:.6,y:.7,z:.05}});
  assert.equal(b.behavior.intent,'escape','the second koi independently interprets a nearby water flick');
  assert.notEqual(b.fish.age,a.fish.age,'fish clocks and decisions do not share state');
  assert.deepEqual(snapshot(source),before);
  // Collision measures the actual scaled, posed tissue rather than assuming
  // the full adult geometry size or merely correcting the fish's root point.
  a.fish.x=2;a.pose();a.updateVisual();assert.equal(a.contain(),true);
  const bounds=a.containment.measure();
  for(const [i,axis] of ['x','y','z'].entries()){
    assert.ok(bounds.min[axis]>=waterMin[i]+.0069&&bounds.max[axis]<=waterMax[i]-.0069,
      'the complete scaled fish remains inside the water volume');
  }
  a.setInspection(true);assert.ok(a.anatomy.every(mesh=>mesh.visible));assert.ok(a.scales.every(mesh=>!mesh.visible));
  a.setInspection(false);assert.ok(a.anatomy.every(mesh=>!mesh.visible));assert.ok(a.scales.every(mesh=>mesh.visible));
  assert.ok(b.anatomy.every(mesh=>!mesh.visible),'inspection is per fish');
  a.fish.bodyScale=9;a.reset();assert.equal(a.fish.bodyScale,.42,'reset restores the actual body size');
  let originalGeometryDisposals=0,textureDisposals=0,returnedMaterialDisposals=0;
  const geometries=new Set([...sourceMeshes.values()].map(mesh=>mesh.geometry));
  for(const geometry of geometries)geometry.addEventListener('dispose',()=>originalGeometryDisposals++);
  const textures=new Set([...sourceMeshes.values()].map(mesh=>mesh.material.map).filter(Boolean));
  for(const texture of textures)texture.addEventListener('dispose',()=>textureDisposals++);
  for(const material of returnedMaterials)material.addEventListener('dispose',()=>returnedMaterialDisposals++);
  let ownedDisposals=0;for(const geometry of a.ownedGeometries)geometry.addEventListener('dispose',()=>ownedDisposals++);
  a.dispose();a.dispose();
  assert.equal(ownedDisposals,a.ownedGeometries.size,'owned mutable geometry is disposed exactly once');
  assert.equal(originalGeometryDisposals,0);assert.equal(textureDisposals,0);assert.equal(returnedMaterialDisposals,0,
    'the caller keeps ownership of the callback materials');
  b.dispose();for(const material of returnedMaterials)material.dispose();
  return {fish:2,axialJoints:a.joints.length,facialJoints:a.facialRig.jointCount,
    independentMutableGeometries:a.ownedGeometries.size,maximumRayGapMM:maximumRayGap*1000,neighbors};
}

if(process.argv[1]&&pathToFileURL(process.argv[1]).href===import.meta.url)console.log(JSON.stringify(await assertFishInstances()));
