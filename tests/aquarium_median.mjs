import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Triangle,Matrix3,Matrix4} from '../static/vendor/three.webgpu.js';
import {createSwimmer} from '../static/aquarium-motion.js';
import {applySwimPose,createFacialRig} from '../static/aquarium-rig.js';
import {createContainment} from '../static/aquarium-collision.js';
import {createFinFrame,createFinController,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';
import {createCaudalFrame,createCaudalController,bindCaudalGeometry,deformCaudalGeometry} from '../static/aquarium-caudal.js';
import {createMedianFin,createMedianMotion,createMedianController,deformMedianFin} from '../static/aquarium-median.js';
import {createStudyRandom} from '../static/aquarium-study.js';
import {tankCenterZ,waterMin,waterMax} from '../static/aquarium-space.js';

const distance=(a,b)=>Math.hypot(...a.map((v,i)=>v-b[i]));
const point=(values,i)=>Array.from(values.slice(i*3,i*3+3));
const center=(indices,values)=>[0,1,2].map(axis=>indices.reduce((sum,i)=>sum+values[i*3+axis],0)/indices.length);
const pathLength=points=>points.reduce((sum,p,i)=>sum+(i?distance(p,points[i-1]):0),0);
const vector=(values,i)=>new Vector3().fromArray(values,i*3);
const neutral={x:0,y:.8,z:tankCenterZ,yaw:0,pitch:0,bank:0,bankRate:0,pitchRate:0,yawRate:0,
  pitchFin:0,rollFin:0,phase:0,age:0,drive:0,stroke:0,muscle1:0,muscle2:0,muscle3:0,speed:0,forwardSpeed:0};

function radialPaths(record){
  const rows=new Map();
  for(let i=0;i<record.base.length/3;i++){
    const u=record.uv[i*2];if(!rows.has(u))rows.set(u,[]);rows.get(u).push(i);
  }
  const paths=[...rows].sort(([a],[b])=>a-b).map(([,ids])=>ids.sort((a,b)=>record.uv[b*2+1]-record.uv[a*2+1]));
  assert.equal(paths.length,record.kind==='dorsal'?43:15,'every actual median radial material trajectory is retained');
  assert.ok(paths.every(path=>path.length===17),'each actual radial trajectory retains all sixteen material segments');
  return paths;
}

function tubePaths(record){
  const count=record.base.length/3,parent=Array.from({length:count},(_,i)=>i);
  const find=i=>parent[i]===i?i:parent[i]=find(parent[i]);
  for(let i=0;i<record.index.length;i+=3){
    const root=find(record.index[i]);parent[find(record.index[i+1])]=root;parent[find(record.index[i+2])]=root;
  }
  const components=new Map();
  for(let i=0;i<count;i++){const root=find(i);if(!components.has(root))components.set(root,[]);components.get(root).push(i);}
  assert.equal(components.size,record.kind==='dorsal'?63:21,'all actual median shafts and bifurcating branches are measured');
  return [...components.values()].map((ids,n)=>{
    const rings=new Map();
    for(const i of ids){
      if(record.uv[i*2+1]>.999)continue;
      const u=record.uv[i*2];if(!rings.has(u))rings.set(u,[]);rings.get(u).push(i);
    }
    const path=[...rings].sort(([a],[b])=>a-b).map(([,indices])=>indices);
    const kind=n%3===0?'shaft':'branch';
    assert.equal(path.length,kind==='shaft'?25:9,'the actual tube topology identifies shafts and branches independently');
    assert.ok(path.every(ring=>ring.length===8),'UV seam duplicates are excluded from eight-point tube centers');
    return {rings:path,kind,group:Math.floor(n/3)};
  });
}

function seamGroups(record){
  const groups=new Map();
  for(let i=0;i<record.base.length/3;i++){
    const key=[...point(record.base,i),...record.weights.slice(i*4,i*4+4),...record.joints.slice(i*4,i*4+4)].join(',');
    if(!groups.has(key))groups.set(key,[]);
    const candidates=groups.get(key),normal=vector(record.normals,i).normalize();
    let group=candidates.find(group=>normal.distanceTo(group.normal)<1e-5);
    if(!group){group={normal,indices:[]};candidates.push(group);}group.indices.push(i);
  }
  return [...groups.values()].flat().map(group=>group.indices).filter(ids=>ids.length>1);
}

async function geometry(create){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'MedianGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const joints=[],records=[],other=[];
  asset.traverse(mesh=>{
    if(mesh.isBone&&mesh.name.startsWith('spine_'))joints.push(mesh);
    if(!mesh.isSkinnedMesh)return;
    const a=mesh.geometry.attributes;
    if(/^(Dorsal|Anal)_/.test(mesh.name)){
      const record={mesh,kind:mesh.name.startsWith('Dorsal')?'dorsal':'anal',base:Float32Array.from(a.position.array),
        normals:Float32Array.from(a.normal.array),uv:Float32Array.from(a.uv.array),index:mesh.geometry.index.array.slice(),
        weights:Float32Array.from(a.skinWeight.array),joints:Uint16Array.from(a.skinIndex.array)};
      record.seams=seamGroups(record);records.push(record);
    }else if(/^(Caudal|Pectoral|Pelvic)_/.test(mesh.name))other.push({mesh,base:Float32Array.from(a.position.array),
      paired:!mesh.name.startsWith('Caudal'),pectoral:mesh.name.startsWith('Pectoral')});
  });
  joints.sort((a,b)=>a.name.localeCompare(b.name));
  const rest=joints.map(joint=>joint.quaternion.clone()),face=createFacialRig(asset);
  const families=['dorsal','anal'].map(kind=>{
    const membrane=records.find(record=>record.kind===kind&&record.mesh.name.includes('membrane'));
    const rays=records.find(record=>record.kind===kind&&record.mesh.name.includes('rays'));
    membrane.paths=radialPaths(membrane);rays.paths=tubePaths(rays);
    return {kind,membrane,rays,field:create(membrane.mesh,rays.mesh,{kind})};
  });
  for(const pectoral of [true,false]){
    const fins=other.filter(fin=>fin.paired&&fin.pectoral===pectoral);
    const frames=Object.fromEntries(fins.filter(fin=>fin.mesh.name.includes('membrane')).map(fin=>{
      const frame=createFinFrame(fin.base,fin.mesh.geometry.attributes.uv.array);return [frame.side,frame];
    }));fins.forEach(fin=>{fin.binding=bindFinGeometry(fin.base,frames);});
  }
  const caudal=other.find(fin=>fin.mesh.name.startsWith('Caudal_membrane'));
  const frame=createCaudalFrame(caudal.base,caudal.mesh.geometry.attributes.uv.array);
  other.filter(fin=>!fin.paired).forEach(fin=>{fin.binding=bindCaudalGeometry(fin.base,frame);});
  const pose=fish=>{applySwimPose(asset,joints,rest,fish);face.pose(fish);};
  function snapshot(record){
    const mesh=record.mesh,a=mesh.geometry.attributes;mesh.skeleton.update();
    const palette=mesh.skeleton.boneMatrices,output=new Float64Array(record.base.length),source=new Vector3(),value=new Vector3();
    // Use the Float32 palette and uploaded Float32 position buffer consumed by
    // rendering. getVertexPosition() instead uses double-precision bone worlds.
    for(let i=0;i<output.length/3;i++){
      source.fromBufferAttribute(a.position,i).applyMatrix4(mesh.bindMatrix);
      const x=source.x,y=source.y,z=source.z;let px=0,py=0,pz=0;
      for(let slot=0;slot<4;slot++){
        const weight=a.skinWeight.getComponent(i,slot);if(!weight)continue;
        const offset=a.skinIndex.getComponent(i,slot)*16;
        px+=weight*(palette[offset]*x+palette[offset+4]*y+palette[offset+8]*z+palette[offset+12]);
        py+=weight*(palette[offset+1]*x+palette[offset+5]*y+palette[offset+9]*z+palette[offset+13]);
        pz+=weight*(palette[offset+2]*x+palette[offset+6]*y+palette[offset+10]*z+palette[offset+14]);
      }
      value.set(px,py,pz).applyMatrix4(mesh.bindMatrixInverse).applyMatrix4(mesh.matrixWorld).toArray(output,i*3);
    }
    return output;
  }
  function baseline(fish){
    records.forEach(record=>{record.mesh.geometry.attributes.position.array.set(record.base);record.mesh.geometry.attributes.normal.array.set(record.normals);});
    pose(fish);
    return new Map(records.map(record=>[record,snapshot(record)]));
  }
  return {asset,records,families,other,pose,snapshot,baseline,containment:createContainment(asset)};
}

function geometricNormals(record,positions){
  const normals=new Float64Array(positions.length);
  for(let i=0;i<record.index.length;i+=3){
    const ids=[0,1,2].map(k=>record.index[i+k]),[a,b,c]=ids.map(i=>vector(positions,i));
    const normal=b.sub(a).cross(c.sub(a));
    for(const index of ids)for(let axis=0;axis<3;axis++)normals[index*3+axis]+=normal.getComponent(axis);
  }
  for(const ids of record.seams){
    const sum=new Vector3();ids.forEach(i=>sum.add(vector(normals,i)));ids.forEach(i=>sum.toArray(normals,i*3));
  }
  return normals;
}

function renderedNormals(record,source){
  const mesh=record.mesh,a=mesh.geometry.attributes,output=new Float64Array(record.base.length);
  const skin=new Matrix4(),bone=new Matrix4(),matrix=new Matrix3();
  const world=new Matrix3().getNormalMatrix(mesh.matrixWorld);
  for(let i=0;i<a.position.count;i++){
    skin.elements.fill(0);
    for(let slot=0;slot<4;slot++){
      const weight=a.skinWeight.getComponent(i,slot);if(!weight)continue;
      bone.fromArray(mesh.skeleton.boneMatrices,a.skinIndex.getComponent(i,slot)*16);
      for(let k=0;k<16;k++)skin.elements[k]+=bone.elements[k]*weight;
    }
    skin.premultiply(mesh.bindMatrixInverse).multiply(mesh.bindMatrix);
    matrix.setFromMatrix4(skin);
    const normal=source?vector(source,i):new Vector3().fromBufferAttribute(a.normal,i);
    normal.applyMatrix3(matrix).applyMatrix3(world).normalize().toArray(output,i*3);
  }
  return output;
}

function alignmentGaps(family,positions){
  const {membrane,rays}=family,values=positions.get(membrane),triangles=[];
  for(let i=0;i<membrane.index.length;i+=3){
    const triangle=new Triangle(...[0,1,2].map(k=>vector(values,membrane.index[i+k])));
    if(triangle.getArea()>1e-14)triangles.push(triangle);
  }
  const points=positions.get(rays),closest=new Vector3(),gaps=[];
  for(const {rings} of rays.paths)for(let j=0;j<rings.length;j+=4){
    const p=new Vector3(...center(rings[j],points));let nearest=Infinity;
    for(const triangle of triangles){triangle.closestPointToPoint(p,closest);nearest=Math.min(nearest,p.distanceToSquared(closest));}
    gaps.push(Math.sqrt(nearest));
  }
  return gaps;
}

function assertFamily(family,baseline,positions,report,checkNormals=true){
  const {membrane,rays}=family,changed=positions.get(membrane);
  for(const path of membrane.paths){
    const before=path.map(i=>point(membrane.base,i)),after=path.map(i=>point(changed,i));
    const rootError=distance(after[0],point(baseline.get(membrane),path[0]));
    report.maximumRootError=Math.max(report.maximumRootError,rootError);
    assert.ok(rootError<2e-6,'every actual median basal anchor follows its original posed skeleton point');
    const arc=Math.abs(pathLength(after)/pathLength(before)-1);report.maximumRadialArcStrain=Math.max(report.maximumRadialArcStrain,arc);
    assert.ok(arc<.0005,'the actually skinned median radial arc retains its rest material length');
    for(let j=1;j<path.length;j++){
      const length=distance(before[j],before[j-1]),error=Math.abs(distance(after[j],after[j-1])-length);
      report.maximumRadialSegmentError=Math.max(report.maximumRadialSegmentError,error);
      // A half-micrometre covers Float32 inverse-skin round trips throughout
      // this metre-scaled tank, including the shortest dorsal edge segments.
      assert.ok(error<5e-7+length*.00002,'each rendered radial material segment keeps its length after inverse skinning and upload');
    }
  }
  for(let n=0;n<rays.paths.length;n++){
    const {rings,kind,group}=rays.paths[n],before=rings.map(ids=>center(ids,rays.base)),after=rings.map(ids=>center(ids,positions.get(rays)));
    const arc=Math.abs(pathLength(after)/pathLength(before)-1);report[kind].maximumArcStrain=Math.max(report[kind].maximumArcStrain,arc);
    assert.ok(arc<.0015,`actual median ray shafts and branches retain their centerline material arc (${family.kind} ${kind} ${group}: ${arc})`);
    for(let j=1;j<rings.length;j++){
      const strain=Math.abs(distance(after[j],after[j-1])/distance(before[j],before[j-1])-1);
      report[kind].maximumSegmentStrain=Math.max(report[kind].maximumSegmentStrain,strain);
      assert.ok(strain<.005,`local median tube centerline segments remain close to their material length (${family.kind} ${kind} ${group}, ${j}: ${strain})`);
    }
    for(const ring of rings)for(let i=0;i<ring.length;i++)for(const step of [1,4]){
      const a=ring[i],b=ring[(i+step)%ring.length];
      const before=distance(point(rays.base,a),point(rays.base,b)),after=distance(point(positions.get(rays),a),point(positions.get(rays),b));
      const error=Math.abs(after-before);
      report.maximumTubeSectionError=Math.max(report.maximumTubeSectionError,error);
      assert.ok(error<2e-7+before*.00002,'every actual median tube cross-section retains its captured annulus, including strong body turns');
    }
    if(kind==='shaft'){
      const anchor=point(changed,membrane.paths[group*2+1][0]);
      assert.ok(distance(after[0],anchor)<2e-6,'each actual median shaft begins at its corresponding material attachment');
    }else {
      const shaft=rays.paths[group*3],joint=center(shaft.rings[16],positions.get(rays));
      assert.ok(distance(after[0],joint)<2e-6,'each actual branch stays joined to its parent shaft');
    }
  }
  for(const record of [membrane,rays]){
    const values=positions.get(record);
    assert.ok(values.every(Number.isFinite),'actual posed median geometry remains finite');
    for(const ids of record.seams)for(const i of ids)assert.ok(distance(point(values,i),point(values,ids[0]))<2e-7,
      'the actual median tube UV seam cannot tear under the shared posed field');
  }
  for(const record of [membrane,rays]){
    const original=baseline.get(record),deformed=positions.get(record);
    for(let i=0;i<record.index.length;i+=3){
      const ids=[0,1,2].map(k=>record.index[i+k]),[a,b,c]=ids.map(i=>vector(original,i));
      const normal=b.sub(a).cross(c.sub(a)),area=normal.length();if(area<1e-12)continue;
      const [x,y,z]=ids.map(i=>vector(deformed,i)),next=y.sub(x).cross(z.sub(x));
      const jacobian=next.dot(normal)/(area*area);
      report.minimumSurfaceJacobian=Math.min(report.minimumSurfaceJacobian,jacobian);
      assert.ok(jacobian>.2&&jacobian<5,`actual posed median tissue and tube walls retain a positive local surface Jacobian in their original winding (${record.mesh.name}, triangle ${i/3}: ${jacobian})`);
    }
  }
  if(checkNormals)for(const record of [membrane,rays]){
    const expected=geometricNormals(record,positions.get(record)),actual=renderedNormals(record),authored=renderedNormals(record,record.normals);
    assert.ok(actual.every(Number.isFinite),'even zero-area anal root normals remain finite after actual GPU skinning');
    for(let i=0;i<actual.length/3;i++){
      const normal=vector(expected,i);
      assert.ok(Math.abs(vector(actual,i).length()-1)<1e-6,'all actual rendered median normals retain a usable unit direction');
      if(normal.lengthSq()<1e-20){
        report.degenerateNormalChecks++;
        assert.ok(vector(actual,i).dot(vector(authored,i))>.999,'zero-area anal roots preserve their captured authored normal in the actual pose');
        continue;
      }
      const agreement=normal.normalize().dot(vector(actual,i));
      report.minimumNormalAgreement=Math.min(report.minimumNormalAgreement,agreement);
      assert.ok(agreement>.94,'uploaded normals describe the actually posed surface after the GPU normal skin map');
    }
    for(const ids of record.seams)for(const i of ids)assert.ok(distance(point(actual,i),point(actual,ids[0]))<1e-4,
      'rendered normal directions remain smooth across exact authored median UV seams');
  }
}

export async function assertMedianGeometry({create=createMedianFin,motion=createMedianMotion,controller=createMedianController,deform=deformMedianFin}={}){
  const model=await geometry(create),report={maximumRootError:0,maximumRadialArcStrain:0,maximumRadialSegmentError:0,
    shaft:{maximumArcStrain:0,maximumSegmentStrain:0},branch:{maximumArcStrain:0,maximumSegmentStrain:0},
    minimumSurfaceJacobian:Infinity,minimumNormalAgreement:Infinity,maximumGapIncrease:0,maximumCorrection:0,degenerateNormalChecks:0,
    maximumTubeSectionError:0,
    intrinsic:{},causal:{},maximumRigidTransformError:0,minimumFinBoundaryClearance:Infinity};
  const swimmer=createSwimmer(()=>.5);Object.assign(swimmer.fish,{x:0,y:.8,z:tankCenterZ,yaw:0});
  swimmer.navigate({x:.8,y:1.15,z:tankCenterZ+.2});
  const poses=[{...neutral}];
  for(let i=1;i<=132;i++){swimmer.step(1/120);if([60,87,132].includes(i))poses.push({...swimmer.fish});}
  poses.push({...swimmer.fish,yaw:1.3,pitch:.4,bank:.1,bankRate:.1,bankLag1:-.007,bankLag2:-.015,bankLag3:-.026,
    yawRate:.7,pitchRate:.3,torsionDrive:.16,stroke:1,phase:1.4});
  for(const sign of [-1,1])poses.push({...neutral,age:.7,yaw:.8,pitch:.25,bank:sign*.12,
    yawRate:sign*.9,pitchRate:.2,finSteer:sign*.3,stroke:1,drive:.4,muscle1:.8,muscle2:.8,muscle3:.8,phase:1.4,
    turnAnterior:sign*2.4,turnTrunk:sign*2.4,turnPosterior:sign*2.4,
    turnRecruitment1:.25,turnRecruitment2:.25,turnRecruitment3:.25});
  let restGaps;
  for(const [n,fish] of poses.entries()){
    const baseline=model.baseline(fish);
    if(n===0)restGaps=model.families.map(family=>alignmentGaps(family,baseline));
    for(const family of model.families)deform(family.field,motion(fish,family.kind));
    const positions=new Map(model.records.map(record=>[record,model.snapshot(record)]));
    for(const [i,family] of model.families.entries()){
      assertFamily(family,baseline,positions,report);
      // Compare each tube sample with its OWN authored baseline discrepancy;
      // the largest existing gap elsewhere cannot hide a new separation.
      const increase=Math.max(...alignmentGaps(family,positions).map((gap,j)=>gap-restGaps[i][j]));
      report.maximumGapIncrease=Math.max(report.maximumGapIncrease,increase);
      assert.ok(increase<.00015,'shared posed median tissue and branched rays retain their baseline material alignment');
    }
    // Repeated rendering at a paused pose must always start from immutable
    // authored coordinates, never skin or deform yesterday's output again.
    for(const family of model.families)deform(family.field,motion(fish,family.kind));
    for(const record of model.records){
      const repeated=model.snapshot(record),previous=positions.get(record);
      assert.ok(repeated.every((v,i)=>Math.abs(v-previous[i])<1e-10),'repeated same-pose median deformation is idempotent');
    }
  }
  function shape(fish,commands){
    model.baseline(fish);
    for(const family of model.families)deform(family.field,commands(family.kind));
    const inverse=model.asset.matrixWorld.clone().invert();
    const normals=new Map(),normalMatrix=new Matrix3().getNormalMatrix(inverse);
    const result=new Map(model.records.map(record=>{
      const positions=model.snapshot(record);
      for(let i=0;i<positions.length/3;i++)vector(positions,i).applyMatrix4(inverse).toArray(positions,i*3);
      const directions=renderedNormals(record);
      for(let i=0;i<directions.length/3;i++)vector(directions,i).applyMatrix3(normalMatrix).normalize().toArray(directions,i*3);
      normals.set(record,directions);
      return [record,positions];
    }));
    result.normals=normals;return result;
  }
  // Hold the SAME actual skeleton still while changing only the independent
  // median command. Correcting skin strain alone cannot satisfy this oracle.
  const quietShape=shape(neutral,()=>({incline:0,curve:0}));
  const commands=sign=>kind=>motion({...neutral,yawRate:sign*.8,finSteer:sign*.2},kind);
  const positiveShape=shape(neutral,commands(1)),negativeShape=shape(neutral,commands(-1));
  const curveShape=shape(neutral,()=>({incline:0,curve:.08}));
  for(const {kind,membrane} of model.families){
    const quiet=quietShape.get(membrane),positive=positiveShape.get(membrane),negative=negativeShape.get(membrane);
    const gains=membrane.paths.map(path=>{
      const tip=path.at(-1),length=pathLength(path.map(i=>point(membrane.base,i)));
      assert.ok(positive[tip*3+2]>quiet[tip*3+2]&&negative[tip*3+2]<quiet[tip*3+2],
        'signed dorsal and anal commands move their actual distal tissue toward the same animal-local side');
      return (positive[tip*3+2]-quiet[tip*3+2])/length;
    });
    const average=values=>values.reduce((sum,value)=>sum+value,0)/values.length;
    const quarter=Math.ceil(gains.length/4),leading=average(gains.slice(0,quarter)),posterior=average(gains.slice(-quarter));
    assert.ok(average(gains)>.025,'intrinsic median command changes actual distal shape independently of root and spine motion');
    assert.ok(leading<posterior*.6,'the actual leading support stays quieter than posterior material rays');
    const bend=membrane.paths.slice(-quarter).map(path=>{
      const angle=j=>{
        const a=vector(quiet,path[j]).sub(vector(quiet,path[j-1])).normalize();
        const b=vector(curveShape.get(membrane),path[j]).sub(vector(curveShape.get(membrane),path[j-1])).normalize();
        return Math.acos(Math.max(-1,Math.min(1,a.dot(b))));
      };
      const basal=angle(1),distal=angle(path.length-1);
      assert.ok(distal>basal+.02,'distal curvature bends actual material directions instead of only rotating a rigid fin');
      return distal;
    });
    const normalChange=membrane.paths.slice(-quarter).map(path=>distance(
      point(quietShape.normals.get(membrane),path.at(-1)),point(positiveShape.normals.get(membrane),path.at(-1))));
    assert.ok(average(normalChange)>.01,'actual uploaded membrane normals respond to the independent material deformation');
    report.intrinsic[kind]={meanTipTravelPerArc:average(gains),leading,posterior,meanDistalBend:average(bend),meanNormalChange:average(normalChange)};
  }
  const movedShape=shape({...neutral,x:.3,y:1.1,z:tankCenterZ-.1,yaw:1.1,pitch:.3,bank:-.1},commands(1));
  for(const record of model.records){
    const a=positiveShape.get(record),b=movedShape.get(record);
    for(let i=0;i<a.length/3;i++)report.maximumRigidTransformError=Math.max(report.maximumRigidTransformError,distance(point(a,i),point(b,i)));
  }
  assert.ok(report.maximumRigidTransformError<5e-7,'a root-only rigid transform preserves the independently deformed body-local median surface');
  // Controller observations are causal, bounded and quiet without a separate
  // phase oscillator. Frozen-mode changes and rewinds discard previous loads.
  for(const kind of ['dorsal','anal']){
    const membrane=model.families.find(family=>family.kind===kind).membrane;
    const travel=command=>{
      const positions=shape(neutral,family=>family===kind?command:{incline:0,curve:0}).get(membrane);
      const original=quietShape.get(membrane);
      return Math.sqrt(positions.reduce((sum,value,i)=>sum+(value-original[i])**2,0)/(positions.length/3));
    };
    const control=controller(kind),quiet={...neutral,speed:.2,forwardSpeed:.2};
    const first={...control.sample(quiet,'live')};
    const active={...quiet,age:1,yawRate:.6,finSteer:.2};
    control.sample({...quiet,age:.99},'live');const onset={...control.sample(active,'live')};
    const target=motion(active,kind);
    assert.ok(Math.abs(onset.curve/target.curve)<Math.abs(onset.incline/target.incline)*.1,
      'new distal curvature follows observed deployment instead of copying its filter output immediately');
    let loaded;
    for(let i=1;i<=120;i++)loaded={...control.sample({...active,age:1+i/120},'live')};
    assert.ok(Math.abs(loaded.incline)>.01&&Math.abs(loaded.curve)>.01,'actual turn demand loads separate median deployment and distal bend');
    const fullTravel=travel(motion(active,kind)),onsetTravel=travel(onset),loadedTravel=travel(loaded);
    assert.ok(onsetTravel>0&&onsetTravel<fullTravel*.04,'actual tissue begins a finite response before settling into the commanded turn shape');
    assert.ok(loadedTravel>fullTravel*.95,'the observed median controller really loads the independently deformed tissue');
    const paused=control.sample({...active,age:2},'live');
    assert.strictEqual(control.sample({...quiet,age:2},'live'),paused,'paused same-mode median output keeps identity and state');
    const release={...control.sample({...quiet,age:2+1/120},'live')},releaseTravel=travel(release);
    assert.ok(releaseTravel>loadedTravel*.98,'actual tissue does not instantly unload at the turn-to-glide boundary');
    let settled;
    for(let i=2;i<=120;i++)settled={...control.sample({...quiet,age:2+i/120},'live')};
    const settledTravel=travel(settled);
    assert.ok(settledTravel<loadedTravel*.03,'actual quiet-glide tissue releases its previous maneuvering load');
    report.causal[kind]={onsetFraction:onsetTravel/fullTravel,loadedFraction:loadedTravel/fullTravel,
      releaseFraction:releaseTravel/loadedTravel,settledFraction:settledTravel/loadedTravel};
    const reset={...control.sample({...quiet,age:3,poseHistory:false},'vertical')};
    const fresh={...controller(kind).sample({...quiet,age:3,poseHistory:false},'vertical')};
    assert.deepEqual(reset,fresh,'paused inspection changes cannot inherit the previous live median loading');
    assert.deepEqual({...control.sample(quiet,'live')},first,'rewinding the median clock clears stale loading');
    for(let i=1;i<=120;i++){
      const value=control.sample({...quiet,age:i/120,phase:i*.1},'live');
      assert.ok(Math.abs(value.incline)<1e-12&&Math.abs(value.curve)<1e-12,'quiet glides have no decorative median oscillator');
    }
  }
  for(const seed of [913,481,33]){
    const swim=createSwimmer(createStudyRandom(seed));Object.assign(swim.fish,{x:.65,y:.58,z:tankCenterZ,yaw:0});
    swim.navigate({x:-.85,y:1.17,z:tankCenterZ-.1});
    const controls={pectoral:{'-1':createFinController(-1,true),'1':createFinController(1,true)},
      pelvic:{'-1':createFinController(-1,false),'1':createFinController(1,false)},caudal:createCaudalController(),
      dorsal:controller('dorsal'),anal:controller('anal')};
    for(let frame=0;frame<60*120;frame++){
      if(frame===12*120)swim.navigate({x:.85,y:.5,z:tankCenterZ+.1});
      if(frame===32*120)swim.navigate({x:-.8,y:.98,z:tankCenterZ-.2});
      if(frame===48*120)swim.navigate({x:.8,y:.61,z:tankCenterZ+.1});
      swim.step(1/120);const fish=swim.fish;
      const paired=Object.fromEntries(['pectoral','pelvic'].map(kind=>[kind,{'-1':controls[kind][-1].sample(fish),'1':controls[kind][1].sample(fish)}]));
      const tail=controls.caudal.sample(fish),median={dorsal:controls.dorsal.sample(fish),anal:controls.anal.sample(fish)};
      if(frame%30)continue;
      model.baseline(fish);
      for(const fin of model.other){
        const output=fin.mesh.geometry.attributes.position.array;
        if(fin.paired)deformFinGeometry(fin.binding,paired[fin.pectoral?'pectoral':'pelvic'],output);
        else deformCaudalGeometry(fin.binding,tail,output);
      }
      for(const family of model.families)deform(family.field,median[family.kind]);
      const correction=model.containment.correction(),magnitude=Math.hypot(correction.x,correction.y,correction.z);
      report.maximumCorrection=Math.max(report.maximumCorrection,magnitude);
      assert.ok(magnitude<1e-6,`actual median, paired and caudal surfaces clear the tank during combined seed ${seed}`);
      // Production containment deliberately omits branched-ray tubes. Inspect
      // every rendered fin vertex independently, including those tube walls.
      for(const record of [...model.records,...model.other]){
        const positions=model.snapshot(record);
        for(let i=0;i<positions.length;i++){
          const axis=i%3,clearance=Math.min(positions[i]-waterMin[axis],waterMax[axis]-positions[i]);
          report.minimumFinBoundaryClearance=Math.min(report.minimumFinBoundaryClearance,clearance);
          assert.ok(clearance>=-1e-6,`every actual fin vertex stays within the water volume in seed ${seed}`);
        }
      }
      assert.deepEqual(model.asset.scale.toArray(),[1,1,1],'median deformation never changes the fish scale');
    }
  }
  return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const report=await assertMedianGeometry();
  const moduleURL=new URL('../static/aquarium-median.js',import.meta.url),source=(await readFile(moduleURL,'utf8')).replace(/\r\n/g,'\n');
  const load=async text=>{
    const absolute=text.replace(/from '(\.[^']+)'/g,(_,path)=>`from '${new URL(path,moduleURL)}'`);
    return import(`data:text/javascript;base64,${Buffer.from(absolute).toString('base64')}`);
  };
  const sectionStart=source.indexOf('      const ring=rings[j],center=j*3,guide=ring.unique[0]*3;');
  const sectionEnd=source.indexOf('\n    }\n  }\n  family.maximumTubeCorrection',sectionStart);
  assert.ok(sectionStart>=0&&sectionEnd>sectionStart,'the former translated-annulus mutation has a precise production seam');
  const squeezed=await load(source.slice(0,sectionStart)+
    '      for(const i of rings[j].all){target[i*3]+=dx;target[i*3+1]+=dy;target[i*3+2]+=dz;}'+source.slice(sectionEnd));
  await assert.rejects(()=>assertMedianGeometry({create:squeezed.createMedianFin,deform:squeezed.deformMedianFin}),
    /median tube cross-section retains its captured annulus/,'actual exported tube walls reject the former squeezed cross-section field');
  const detachedSource=source.replace('if(!tube.shaft&&tube.border){','if(false&&tube.border){');
  assert.notEqual(detachedSource,source,'the finite branch-contact correction was disconnected');
  const detached=await load(detachedSource);
  await assert.rejects(()=>assertMedianGeometry({create:detached.createMedianFin,deform:detached.deformMedianFin}),
    /retain their baseline material alignment/,'actual exported branches reject a length-only free tip outside the tissue');
  console.log(`Actual median fins passed: ${JSON.stringify(report)}; squeezed-wall and detached-tip mutations rejected`);
}
