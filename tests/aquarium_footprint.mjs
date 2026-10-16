import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Matrix4,Box3} from '../static/vendor/three.webgpu.js';
import {createKoiInstance} from '../static/aquarium-fish-instance.js';
import {createManeuverFootprint} from '../static/aquarium-footprint.js';
import {createStudyRandom} from '../static/aquarium-study.js';
import {tankCenterZ,waterMin,waterMax} from '../static/aquarium-space.js';

async function pristine(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'FootprintGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  return (await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;
}
function sourceState(asset){
  const transforms=[],hash=createHash('sha256');
  asset.traverse(node=>{
    transforms.push({name:node.name,position:node.position.toArray(),quaternion:node.quaternion.toArray(),scale:node.scale.toArray(),children:node.children.map(child=>child.uuid),userData:JSON.stringify(node.userData)});
    if(node.isMesh)for(const name of Object.keys(node.geometry.attributes).sort()){
      const a=node.geometry.attributes[name].array;hash.update(node.name+' '+name);hash.update(new Uint8Array(a.buffer,a.byteOffset,a.byteLength));
    }
  });
  return {transforms,geometry:hash.digest('hex')};
}
function boxValues(box){return [...box.min.toArray(),...box.max.toArray()];}
const motors={pectoralPaddlePlus:.085,pectoralPaddleMinus:-.024,pectoralForeAftPlus:.06,pectoralForeAftMinus:-.06,
  pelvicPaddlePlus:-.008,pelvicPaddleMinus:.02975,pelvicForeAftPlus:.014,pelvicForeAftMinus:-.014};

// Reproduce the actual uploaded Float32 skeleton palette, rather than CPU
// getVertexPosition's separate double-matrix bone path. Inspect every vertex,
// including normally hidden anatomy, ray tubes and overlapping scale relief.
function actualVertices(instance,box,fish,report,enforce=true){
  const meshes=[];instance.asset.traverse(mesh=>{if(mesh.isSkinnedMesh)meshes.push(mesh);});
  const skeletons=new Set(meshes.map(mesh=>mesh.skeleton));skeletons.forEach(skeleton=>skeleton.update());
  const bind=new Vector3(),world=new Vector3(),post=new Matrix4(),scale=instance.asset.scale.x,tolerance=2e-6*scale;
  const horizontalLimit=.98*scale+.04,verticalLimit=(.27+.85*Math.abs(Math.sin(fish.pitch))+.85*Math.abs(Math.sin(fish.bank)))*scale+.04;
  let maximumOutside=0;
  for(const mesh of meshes){
    report.meshes.add(mesh.name);const a=mesh.geometry.attributes,palette=mesh.skeleton.boneMatrices;
    post.multiplyMatrices(mesh.matrixWorld,mesh.bindMatrixInverse);
    for(let i=0;i<a.position.count;i++){
      bind.fromBufferAttribute(a.position,i).applyMatrix4(mesh.bindMatrix);world.set(0,0,0);
      for(let slot=0;slot<4;slot++){
        const weight=a.skinWeight.getComponent(i,slot);if(!weight)continue;const e=a.skinIndex.getComponent(i,slot)*16;
        world.x+=weight*(palette[e]*bind.x+palette[e+4]*bind.y+palette[e+8]*bind.z+palette[e+12]);
        world.y+=weight*(palette[e+1]*bind.x+palette[e+5]*bind.y+palette[e+9]*bind.z+palette[e+13]);
        world.z+=weight*(palette[e+2]*bind.x+palette[e+6]*bind.y+palette[e+10]*bind.z+palette[e+14]);
      }
      world.applyMatrix4(post);
      if(!Number.isFinite(world.x)||!Number.isFinite(world.y)||!Number.isFinite(world.z))assert.fail('every actual forecast comparison vertex remains finite');
      const outside=Math.max(box.min.x-world.x,world.x-box.max.x,box.min.y-world.y,world.y-box.max.y,box.min.z-world.z,world.z-box.max.z,0);
      maximumOutside=Math.max(maximumOutside,outside);
      if(enforce&&outside>=tolerance)assert.fail(`actual updated ${mesh.name} vertex ${i} lies inside the supplied material bounds: ${outside}`);
      const horizontal=Math.hypot(world.x-fish.x,world.z-fish.z),vertical=Math.abs(world.y-fish.y);
      report.maximumHorizontalRadiusPerScale=Math.max(report.maximumHorizontalRadiusPerScale,horizontal/scale);
      report.maximumHorizontalBroadphaseExcess=Math.max(report.maximumHorizontalBroadphaseExcess,horizontal-horizontalLimit);
      report.maximumVerticalBroadphaseExcess=Math.max(report.maximumVerticalBroadphaseExcess,vertical-verticalLimit);
      report.vertices++;
    }
  }
  if(enforce)report.maximumOutside=Math.max(report.maximumOutside,maximumOutside);
  return maximumOutside;
}

// Exercise the renderer's actual factory-to-navigation join. The instrumented
// callback uses this pristine asset, while a separately created instance keeps
// the factory's automatically installed callback. Their states must agree,
// including near a pane where the forecast is actually consulted. The live
// instance observes real fin histories at120Hz and performs deformations and
// final containment at60Hz display cadence. Selected checkpoints additionally
// inspect every uploaded point.
export async function assertFootprintNavigation(create=createManeuverFootprint,report={poses:0,vertices:0,meshes:new Set(),maximumOutside:0,
  maximumHorizontalRadiusPerScale:0,maximumHorizontalBroadphaseExcess:-Infinity,maximumVerticalBroadphaseExcess:-Infinity}){
  const source=await pristine(),before=sourceState(source),routes=[];
  const water=new Box3(new Vector3(...waterMin).addScalar(.007),new Vector3(...waterMax).addScalar(-.007));
  for(const offset of [0,.40]){
    const random=createStudyRandom(913),referenceRandom=createStudyRandom(913);
    const instance=createKoiInstance(source,{random}),reference=createKoiInstance(source,{random:referenceRandom});
    const footprint=create(source);let callbacks=0,maximumBend=0,maximumHeading=0,warmX=0;
    instance.swimmer.footprint(pose=>{
      callbacks++;
      for(const field of ['x','y','z','yaw','pitch','bank','phase','age','turnAnterior','turnTrunk','turnPosterior'])
        assert.ok(Number.isFinite(pose[field]),`navigation supplies finite complete ${field} to the actual asset forecast`);
      const bounds=footprint.bounds(pose);
      assert.ok(boxValues(bounds).every(Number.isFinite),'navigation receives a finite actual-asset forecast');
      return bounds;
    });
    for(const [live,rng] of [[instance,random],[reference,referenceRandom]]){
      rng.reset();live.swimmer.reset();Object.assign(live.fish,{x:.55,y:.84,z:tankCenterZ+offset,yaw:0});
      live.swimmer.navigate({x:-.85,y:.84,z:tankCenterZ+offset});
    }
    for(let frame=0;frame<(offset?600:480);frame++){
      if(frame===120)for(const live of [instance,reference])live.swimmer.navigate({x:.85,y:.84,z:tankCenterZ+offset+.04});
      instance.step(1/60);reference.swimmer.step(1/60);
      const fish=instance.fish;
      assert.equal(instance.behavior.contacts,0,'normal and close-pane navigation do not rely on corrective geometry teleports');
      for(const [field,value] of Object.entries(fish)){
        if(typeof value==='number')assert.ok(Number.isFinite(value),`actual navigation ${field} remains finite`);
        assert.equal(value,reference.fish[field],`the factory-installed pristine callback agrees with the independently instrumented forecast for ${field}`);
      }
      maximumBend=Math.max(maximumBend,Math.abs(fish.turnAnterior),Math.abs(fish.turnTrunk),Math.abs(fish.turnPosterior));
      maximumHeading=Math.max(maximumHeading,Math.abs(fish.yaw));
      if(frame===119)warmX=fish.x;
      if((frame+1)%60===0||[132,140,150,165].includes(frame+1)){
        actualVertices(instance,footprint.bounds(fish),fish,report);
        actualVertices(instance,water,fish,report);report.poses++;
      }
    }
    assert.ok(maximumBend>1,'the actual navigation join issues a substantial distributed body turn');
    assert.ok(maximumHeading>2.5,'the warm reversal changes actual heading by more than a right angle');
    assert.ok(instance.fish.x>warmX+(offset?.10:.18),'a safe actual-asset forecast permits useful travel toward the reversed destination');
    if(offset)assert.ok(callbacks>0,'close-pane navigation must consult the actual asset forecast');
    assert.deepEqual(sourceState(source),before,'live navigation and forecast queries leave the pristine source isolated');
    routes.push({offset,callbacks,maximumBend,maximumHeading,afterWarmX:warmX,endX:instance.fish.x,endYaw:instance.fish.yaw,contacts:instance.behavior.contacts});
    instance.dispose();reference.dispose();
  }
  return routes;
}

// A slightly tighter route reaches the final inset guard during recovery.
// The final guard resolves integrated overlap before render. Record its
// pre-solver overlap, and inspect every actual vertex after each display frame.
export async function assertFootprintEdge(report={poses:0,vertices:0,meshes:new Set(),maximumOutside:0,
  maximumHorizontalRadiusPerScale:0,maximumHorizontalBroadphaseExcess:-Infinity,maximumVerticalBroadphaseExcess:-Infinity}){
  const source=await pristine(),before=sourceState(source),random=createStudyRandom(913),instance=createKoiInstance(source,{random});
  const physicalWater=new Box3(new Vector3(...waterMin),new Vector3(...waterMax));
  const correction=instance.containment.correction.bind(instance.containment),lastSign={x:0,y:0,z:0},total={x:0,y:0,z:0};
  let events=0,maximumCorrection=0,maximumFrameCorrection=0,maximumPreSolverOverlap=0;
  const penetratingVelocities=new Set(),frameCorrection={x:0,y:0,z:0};
  instance.containment.correction=()=>{
    const next=correction();
    assert.ok(['x','y','z'].every(axis=>Number.isFinite(next[axis])),'every edge correction remains finite even when no fallback is needed');
    if(['x','y','z'].some(axis=>Math.abs(next[axis])>1e-6)){
      events++;
      maximumPreSolverOverlap=Math.max(maximumPreSolverOverlap,actualVertices(instance,physicalWater,instance.fish,report,false));report.poses++;
      for(const axis of ['x','y','z']){
        const amount=Math.abs(next[axis]);maximumCorrection=Math.max(maximumCorrection,amount);
        assert.ok(amount<=.020+2e-6,'the edge fallback resolves a finite sub-20 mm integrated overlap before render');
        if(amount>1e-6){
          const sign=Math.sign(next[axis]);assert.ok(!lastSign[axis]||lastSign[axis]===sign,'a short edge recovery cannot bounce between opposing corrections');
          lastSign[axis]=sign;total[axis]+=amount;frameCorrection[axis]+=amount;
          const velocity={x:'vx',y:'vy',z:'vz'}[axis];
          if(instance.fish[velocity]*next[axis]<0)penetratingVelocities.add(velocity);
        }
      }
    }
    return next;
  };
  random.reset();instance.swimmer.reset();Object.assign(instance.fish,{x:.55,y:.84,z:tankCenterZ+.48,yaw:0});
  instance.swimmer.navigate({x:-.85,y:.84,z:tankCenterZ+.48});
  for(let frame=0;frame<198;frame++){
    if(frame===120)instance.swimmer.navigate({x:.85,y:.84,z:tankCenterZ+.52});
    penetratingVelocities.clear();for(const axis of ['x','y','z'])frameCorrection[axis]=0;
    instance.step(1/60);
    for(const axis of ['x','y','z']){
      maximumFrameCorrection=Math.max(maximumFrameCorrection,frameCorrection[axis]);
      assert.ok(frameCorrection[axis]<=.020+2e-6,'all containment passes in one display frame stay within the 20 mm fallback bound');
    }
    actualVertices(instance,physicalWater,instance.fish,report);report.poses++;
    for(const velocity of penetratingVelocities)assert.equal(instance.fish[velocity],0,'the corrected root removes its penetrating velocity instead of bouncing');
    const probes=instance.containment.measure();
    for(const [i,axis] of ['x','y','z'].entries())assert.ok(probes.min[axis]>=waterMin[i]+.007-2e-6&&probes.max[axis]<=waterMax[i]-.007+2e-6,
      'the final actual-geometry guard leaves its sampled skin and fin surfaces fully within the inset');
  }
  assert.deepEqual(sourceState(source),before,'edge recovery and guard inspection preserve the pristine source');
  instance.dispose();return {seconds:3.3,events,maximumCorrection,maximumFrameCorrection,maximumPreSolverOverlap,total};
}

export async function assertManeuverFootprint({create=createManeuverFootprint}={}){
  const report={poses:0,vertices:0,meshes:new Set(),maximumOutside:0,maximumHorizontalRadiusPerScale:0,
    maximumHorizontalBroadphaseExcess:-Infinity,maximumVerticalBroadphaseExcess:-Infinity,shapes:[]};
  const scaleSamples=[];
  for(const scale of [.43,1]){
    const source=await pristine();source.scale.setScalar(scale);source.updateMatrixWorld(true);const before=sourceState(source);
    const footprint=create(source);assert.equal(footprint.scale,scale,'the immutable source scale is the physical prediction scale');
    assert.ok(footprint.primitiveCount>0,'the forecast captures real material primitives');
    assert.deepEqual(sourceState(source),before,'constructing a private footprint leaves source transforms and material buffers untouched');
    const instance=createKoiInstance(source,{random:()=>.5});
    const initial={...instance.fish,x:.24,y:.92,z:tankCenterZ+.03,bodyScale:scale};
    let age=1;
    const prepare=overrides=>({...initial,age:age++,speed:.23,forwardSpeed:.23,brake:.8,stroke:1,muscle1:1,muscle2:1,muscle3:1,
      ...motors,pitchFin:.15,rollFin:-.15,finSteer:.8,...overrides});
    function compare(fish){
      instance.pose(fish);instance.updateVisual(fish);
      const box=footprint.bounds(fish);assert.ok(boxValues(box).every(Number.isFinite),'the reused forecast box stays finite');
      actualVertices(instance,box,fish,report);report.poses++;return box.clone();
    }
    for(const amplitude of [0,1.8,2.2,2.4])for(const sign of [-1,1])for(const axes of [
      {yaw:0,pitch:0,bank:0,phase:0},{yaw:.9,pitch:.34,bank:.11,phase:1.2},{yaw:-1.4,pitch:-.4,bank:-.12,phase:4.7},
    ]){
      compare(prepare({...axes,turnAnterior:amplitude*sign,turnTrunk:amplitude*sign,turnPosterior:amplitude*sign,
        turnRecruitment:0,turnRecruitment1:0,turnRecruitment2:0,turnRecruitment3:0}));
    }
    // Nonuniform regional loading and opposing-sign handoffs cannot borrow a
    // uniform-maximum C shape's footprint. Retain actual controller histories.
    const baseAge=age+1;
    for(let tick=0;tick<=120;tick++){
      const t=tick/120,fish={...initial,...motors,age:baseAge+t,yaw:.7+t*.5,pitch:.25*Math.sin(t*5),bank:.12*Math.cos(t*7),phase:t*8,
        speed:.18,forwardSpeed:.18,stroke:1,muscle1:1,muscle2:1,muscle3:1,brake:1,finSteer:-.7,pitchFin:-.15,rollFin:.15,
        turnAnterior:2.4*Math.sin(t*6),turnTrunk:2.4*Math.sin(t*6-.5),turnPosterior:2.4*Math.sin(t*6-1),
        turnRecruitment:1,turnRecruitment1:1,turnRecruitment2:1,turnRecruitment3:1};
      instance.pose(fish);instance.updateVisual(fish);
      if(tick%12===0){actualVertices(instance,footprint.bounds(fish),fish,report);report.poses++;}
    }
    const a=prepare({yaw:.4,pitch:.2,bank:-.1,turnAnterior:1.8,turnTrunk:.9,turnPosterior:-.6}),b=prepare({yaw:-1.1,pitch:-.3,bank:.12,turnAnterior:-2.4,turnTrunk:-1.8,turnPosterior:.5});
    const first=boxValues(footprint.bounds(a)),identity=footprint.bounds(a);
    for(let i=0;i<30;i++){assert.strictEqual(footprint.bounds(a),identity,'paused forecasts reuse the box without adding time');assert.deepEqual(boxValues(identity),first,'paused footprint queries remain idempotent');}
    footprint.bounds(b);assert.deepEqual(boxValues(footprint.bounds(a)),first,'A-to-B-to-A forecasts cannot retain another pose or facial state');
    const flat=prepare({yaw:0,pitch:0,bank:0,turnAnterior:0,turnTrunk:0,turnPosterior:0,stroke:0,muscle1:0,muscle2:0,muscle3:0});
    const straightBox=footprint.bounds(flat).clone(),curledBox=footprint.bounds({...flat,turnAnterior:2.4,turnTrunk:2.4,turnPosterior:2.4}).clone();
    const straight=straightBox.getSize(new Vector3()),curled=curledBox.getSize(new Vector3()),origin=new Vector3(flat.x,flat.y,flat.z);
    scaleSamples.push([straightBox,curledBox].map(box=>[...box.min.clone().sub(origin).toArray(),...box.max.clone().sub(origin).toArray()]));
    report.shapes.push({scale,straight:straight.toArray(),curled:curled.toArray()});
    assert.ok(curled.x<straight.x*.94,'the actual articulated C shape shortens the forecast longitudinal footprint');
    assert.ok(curled.z>straight.z*1.05,'the forecast follows the actual lateral bow instead of returning a constant safety cube');
    assert.deepEqual(sourceState(source),before,'forecasting and independently posing a clone leave every pristine source buffer and transform untouched');
    instance.dispose();
  }
  for(let shape=0;shape<2;shape++)for(let component=0;component<6;component++)
    assert.ok(Math.abs(scaleSamples[0][shape][component]-.43*scaleSamples[1][shape][component])<2e-6,
      'physical scale transforms every root-relative forecast extremum exactly once, including the articulated centering offset');
  report.navigation=await assertFootprintNavigation(create,report);
  report.edge=await assertFootprintEdge(report);
  for(const name of ['Koi_skin','Pectoral_branched_rays','Pelvic_branched_rays','Caudal_branched_rays','Dorsal_branched_rays','Anal_branched_rays'])assert.ok(report.meshes.has(name),`actual ${name} geometry participates in the footprint containment join`);
  assert.ok([...report.meshes].some(name=>name.startsWith('Overlapping_scales')),'overlapping scale relief participates in the actual containment join');
  report.meshCount=report.meshes.size;delete report.meshes;return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href)console.log(`Actual maneuver footprint passed: ${JSON.stringify(await assertManeuverFootprint())}`);
