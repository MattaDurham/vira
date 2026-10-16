import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Triangle,Vector3} from '../static/vendor/three.webgpu.js';
import {createCaudalFrame,createCaudalMotion,createCaudalController,bindCaudalGeometry,
  deformCaudalGeometry,deformCaudalPoint} from '../static/aquarium-caudal.js';

const tau=Math.PI*2;
const distance=(a,b)=>Math.hypot(...a.map((value,i)=>value-b[i]));
const vertex=(values,index)=>new Vector3().fromArray(values,index*3);
const outputPoint=(fin,index)=>Array.from(fin.output.slice(index*3,index*3+3));

async function geometry(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'CaudalGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const fins=[];
  scene.traverse(mesh=>{
    if(!mesh.isMesh||!mesh.name.startsWith('Caudal_'))return;
    const attributes=mesh.geometry.attributes;
    fins.push({mesh,name:mesh.name,base:Float32Array.from(attributes.position.array),
      uv:Float32Array.from(attributes.uv.array),index:mesh.geometry.index.array.slice(),
      weights:Float32Array.from(attributes.skinWeight.array),joints:Uint16Array.from(attributes.skinIndex.array)});
  });
  assert.equal(fins.length,2,'actual caudal tissue and its combined branched rays are both exercised');
  return fins;
}

function triangles(fin,values=fin.output){
  const result=[];
  for(let i=0;i<fin.index.length;i+=3){
    const triangle=new Triangle(...[0,1,2].map(k=>vertex(values,fin.index[i+k])));
    if(triangle.getArea()>1e-14)result.push({triangle,offset:i});
  }
  return result;
}

function derivative(point,frame,motion,deformPoint){
  return [0,1,2].map(axis=>{
    const plus=point.slice(),minus=point.slice(),h=1e-5;
    plus[axis]+=h;minus[axis]-=h;
    const a=deformPoint(plus,frame,motion),b=deformPoint(minus,frame,motion);
    return new Vector3(...a.map((value,i)=>(value-b[i])/(2*h)));
  });
}

function rayGap(rays,membrane){
  const surface=triangles(membrane).map(item=>item.triangle),point=new Vector3(),closest=new Vector3();
  let maximum=0;
  for(let i=0;i<rays.base.length/3;i+=37){
    point.fromArray(rays.output,i*3);let nearest=Infinity;
    for(const triangle of surface){
      triangle.closestPointToPoint(point,closest);
      nearest=Math.min(nearest,point.distanceToSquared(closest));
    }
    maximum=Math.max(maximum,Math.sqrt(nearest));
  }
  assert.ok(maximum<.001,`actual branched rays stay embedded in tissue; largest sampled gap ${maximum*1000} mm`);
  return maximum;
}

function height(fin){
  const ys=[];
  for(let i=0;i<fin.base.length/3;i++)if(fin.uv[i*2+1]<.001)ys.push(fin.output[i*3+1]);
  return Math.max(...ys)-Math.min(...ys);
}

const pathLength=points=>points.reduce((sum,point,i)=>sum+(i?distance(point,points[i-1]):0),0);

function materialPaths(membrane){
  const paths=new Map();
  for(let i=0;i<membrane.base.length/3;i++){
    const u=membrane.uv[i*2];if(!paths.has(u))paths.set(u,[]);
    paths.get(u).push(i);
  }
  const result=[...paths.values()].map(indices=>indices.sort((a,b)=>membrane.uv[b*2+1]-membrane.uv[a*2+1]));
  assert.equal(result.length,39,'all actual authored radial material trajectories are measured');
  assert.ok(result.every(indices=>indices.length===17),'each radial material trajectory retains all sixteen segments');
  return result;
}

function assertMaterialLengths(membrane,paths,report){
  // These coordinates are the actual Float32 GPU upload, not only the double
  // precision integration table. Quantization is allowed; tissue compression
  // of even a fraction of one percent is not hidden by endpoint/chord tests.
  const uploaded=Float32Array.from(membrane.output);
  for(const indices of paths){
    const original=indices.map(i=>Array.from(membrane.base.slice(i*3,i*3+3)));
    const changed=indices.map(i=>Array.from(uploaded.slice(i*3,i*3+3)));
    report.maximumRadialArcStrain=Math.max(report.maximumRadialArcStrain,Math.abs(pathLength(changed)/pathLength(original)-1));
    for(let j=1;j<indices.length;j++){
      const strain=Math.abs(distance(changed[j],changed[j-1])/distance(original[j],original[j-1])-1);
      report.maximumRadialSegmentStrain=Math.max(report.maximumRadialSegmentStrain,strain);
      assert.ok(strain<.00002,`each actual radial material segment keeps its length after GPU quantization (${strain})`);
    }
  }
}

function tubePaths(rays){
  const count=rays.base.length/3,parent=Array.from({length:count},(_,i)=>i);
  const find=i=>parent[i]===i?i:parent[i]=find(parent[i]);
  for(let i=0;i<rays.index.length;i+=3){
    const root=find(rays.index[i]);parent[find(rays.index[i+1])]=root;parent[find(rays.index[i+2])]=root;
  }
  const components=new Map();
  for(let i=0;i<count;i++){
    const root=find(i);if(!components.has(root))components.set(root,[]);
    components.get(root).push(i);
  }
  assert.equal(components.size,57,'the actual mesh contains nineteen ray shafts and thirty-eight branches');
  return [...components.values()].map((indices,component)=>{
    const rings=new Map();
    for(const index of indices){
      // v=1 duplicates the v=0 tube seam. Count its point only once.
      if(rays.uv[index*2+1]>.999)continue;
      const u=rays.uv[index*2];if(!rings.has(u))rings.set(u,[]);
      rings.get(u).push(index);
    }
    const path=[...rings].sort(([a],[b])=>a-b).map(([,indices])=>indices);
    assert.ok(path.every(ring=>ring.length===8),'each extracted centerline uses complete eight-point tube rings');
    const kind=path.length===25?'shaft':'branch';
    assert.equal(kind,component%3===0?'shaft':'branch','each authored ray tree retains one shaft and two branches');
    return {rings:path,kind,group:Math.floor(component/3)};
  });
}

const ringCenter=(ring,points)=>[0,1,2].map(axis=>ring.reduce((sum,i)=>sum+points[i*3+axis],0)/ring.length);

function segmentDistance(first,last,otherFirst,otherLast){
  const sub=(a,b)=>a.map((value,i)=>value-b[i]),dot=(a,b)=>a.reduce((sum,value,i)=>sum+value*b[i],0);
  const u=sub(last,first),v=sub(otherLast,otherFirst),w=sub(first,otherFirst);
  const a=dot(u,u),b=dot(u,v),c=dot(v,v),d=dot(u,w),e=dot(v,w),det=a*c-b*b;
  const pointSegment=(point,start,end)=>{
    const along=sub(end,start),offset=sub(point,start),denominator=dot(along,along);
    const t=denominator?Math.max(0,Math.min(1,dot(offset,along)/denominator)):0;
    return dot(offset.map((value,i)=>value-t*along[i]),offset.map((value,i)=>value-t*along[i]));
  };
  let squared=Math.min(pointSegment(first,otherFirst,otherLast),pointSegment(last,otherFirst,otherLast),
    pointSegment(otherFirst,first,last),pointSegment(otherLast,first,last));
  if(det>1e-20){
    const s=(b*e-c*d)/det,t=(a*e-b*d)/det;
    if(s>=0&&s<=1&&t>=0&&t<=1){
      const delta=w.map((value,i)=>value+s*u[i]-t*v[i]);squared=Math.min(squared,dot(delta,delta));
    }
  }
  return Math.sqrt(squared);
}

function assertTubeClearance(rays,paths){
  const tubes=paths.map(({rings,group})=>{
    const centers=rings.map(ring=>ringCenter(ring,rays.base));
    const radius=Math.max(...rings.flatMap((ring,j)=>ring.map(i=>distance(Array.from(rays.base.slice(i*3,i*3+3)),centers[j]))));
    return {centers,radius,group};
  });
  let minimum=Infinity;
  for(let i=0;i<tubes.length;i++)for(let j=0;j<i;j++){
    const first=tubes[i],second=tubes[j];
    // Branches must join their own shaft; those intentional contacts are not
    // collisions between unrelated ray trees.
    if(first.group===second.group)continue;
    for(let a=1;a<first.centers.length;a++)for(let b=1;b<second.centers.length;b++){
      const clearance=segmentDistance(first.centers[a-1],first.centers[a],second.centers[b-1],second.centers[b])-first.radius-second.radius;
      minimum=Math.min(minimum,clearance);
    }
  }
  assert.ok(minimum>.0005,`unrelated actual three dimensional ray tubes retain positive clearance (${minimum})`);
  return minimum;
}

function assertTubeLengths(rays,paths,report){
  const uploaded=Float32Array.from(rays.output);
  for(const {rings,kind} of paths){
    const original=rings.map(ring=>ringCenter(ring,rays.base)),changed=rings.map(ring=>ringCenter(ring,uploaded));
    const arcStrain=Math.abs(pathLength(changed)/pathLength(original)-1);
    report[kind].maximumArcStrain=Math.max(report[kind].maximumArcStrain,arcStrain);
    assert.ok(arcStrain<.001,'rendered ray shafts and branches retain their actual centerline arc length within one tenth of a percent');
    for(let j=1;j<rings.length;j++){
      const strain=Math.abs(distance(changed[j],changed[j-1])/distance(original[j],original[j-1])-1);
      report[kind].maximumSegmentStrain=Math.max(report[kind].maximumSegmentStrain,strain);
      assert.ok(strain<(kind==='shaft'?.0015:.003),'local tube centerline segments stay close to their material length under the shared interpolated field');
    }
  }
}

// Injection mutation-checks real asset effects without modifying production
// files: a flat sway, a rigid fan or disconnected ray field cannot pass.
export async function assertCaudalGeometry(createFrame=createCaudalFrame,createMotion=createCaudalMotion,
  createController=createCaudalController,bind=bindCaudalGeometry,deform=deformCaudalGeometry,deformPoint=deformCaudalPoint){
  const fins=await geometry(),membrane=fins.find(fin=>fin.name.includes('membrane'));
  const rays=fins.find(fin=>fin.name.includes('rays')),frame=createFrame(membrane.base,membrane.uv);
  const report={vertices:fins.reduce((sum,fin)=>sum+fin.base.length/3,0),minimumJacobian:Infinity,
    minimumAreaRatio:Infinity,maximumAreaRatio:0,minimumNormalAgreement:Infinity,maximumRayGap:0,maximumStep:0,
    maximumRadialArcStrain:0,maximumRadialSegmentStrain:0,
    shaft:{maximumArcStrain:0,maximumSegmentStrain:0},branch:{maximumArcStrain:0,maximumSegmentStrain:0}};
  const radialPaths=materialPaths(membrane),rayPaths=tubePaths(rays);
  report.minimumRestTubeClearance=assertTubeClearance(rays,rayPaths);
  const allJoints=new Set();
  for(const fin of fins){
    fin.binding=bind(fin.base,frame);fin.output=new Float64Array(fin.base.length);
    for(let i=0;i<fin.weights.length;i+=4){
      assert.ok(Math.abs(fin.weights[i]-1)<1e-7,'caudal tissue preserves its rigid basal joint weighting');
      assert.ok(fin.weights[i+1]+fin.weights[i+2]+fin.weights[i+3]<1e-7,'no new joint influence is introduced');
      allJoints.add(fin.joints[i]);
    }
  }
  assert.equal(allJoints.size,1,'the actual membrane and all embedded ray branches share one skeletal transform');
  const quiet={age:1,phase:0,speed:.24,forwardSpeed:.24,stroke:0,muscle3:0,brake:0};
  const modes=[quiet,{...quiet,stroke:1,muscle3:1},{...quiet,brake:1}];
  const heights=[];
  for(let mode=0;mode<modes.length;mode++){
    for(let phase=0;phase<12;phase++){
      const motion=createMotion({...modes[mode],phase:phase*tau/12});
      for(const fin of fins){
        deform(fin.binding,motion,fin.output);
        assert.ok(fin.output.every(Number.isFinite),'every membrane and ray vertex remains finite');
        for(let i=0;i<fin.base.length/3;i+=223){
          const point=Array.from(fin.base.slice(i*3,i*3+3));
          assert.ok(distance(outputPoint(fin,i),deformPoint(point,frame,motion))<1e-10,
            'membrane, shaft and branch landmarks share one continuous deformation');
        }
      }
      assertMaterialLengths(membrane,radialPaths,report);
      assertTubeLengths(rays,rayPaths,report);
      if(phase===0)heights.push(height(membrane));
      for(let i=0;i<membrane.base.length/3;i++){
        if(membrane.uv[i*2+1]>.999)assert.ok(distance(outputPoint(membrane,i),Array.from(membrane.base.slice(i*3,i*3+3)))<1e-10,
          'the complete caudal basal line remains anchored, including both ends');
        if(i%29)continue;
        const point=Array.from(membrane.base.slice(i*3,i*3+3));
        assert.ok(distance(outputPoint(membrane,i),deformPoint(point,frame,motion))<1e-10,
          'bound membrane and individual landmarks use the same field');
        const columns=derivative(point,frame,motion,deformPoint);
        const det=columns[0].dot(columns[1].clone().cross(columns[2]));
        report.minimumJacobian=Math.min(report.minimumJacobian,det);
        assert.ok(det>.5&&det<1.5,`the full three dimensional field remains locally invertible (${mode}, ${phase}, ${i}: ${det})`);
      }
      if(phase===0||phase===3||phase===8){
        report.maximumRayGap=Math.max(report.maximumRayGap,rayGap(rays,membrane));
        for(const {triangle,offset} of triangles(membrane,membrane.base)){
          const changed=new Triangle(...[0,1,2].map(k=>vertex(membrane.output,membrane.index[offset+k])));
          const ratio=changed.getArea()/triangle.getArea();
          report.minimumAreaRatio=Math.min(report.minimumAreaRatio,ratio);report.maximumAreaRatio=Math.max(report.maximumAreaRatio,ratio);
          assert.ok(ratio>.5&&ratio<2,`caudal tessellation retains finite tissue area through kick, glide and brake (${mode}, ${phase}, ${offset}: ${ratio})`);
          const projected=points=>{
            const a=points.a,b=points.b,c=points.c;
            return (b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x);
          };
          assert.ok(projected(changed)/projected(triangle)>.5,'all actual radial triangles retain the planar orientation of the authored material atlas');
          const columns=derivative(triangle.getMidpoint(new Vector3()).toArray(),frame,motion,deformPoint);
          const transform=v=>columns.reduce((sum,column,i)=>sum.addScaledVector(column,v.getComponent(i)),new Vector3());
          const expected=transform(triangle.b.clone().sub(triangle.a)).cross(transform(triangle.c.clone().sub(triangle.a))).normalize();
          const normalAgreement=expected.dot(changed.getNormal(new Vector3()));
          report.minimumNormalAgreement=Math.min(report.minimumNormalAgreement,normalAgreement);
          // Some authored radial triangles are nearly collinear. Their finite
          // chord normals differ from the midpoint tangent under curvature;
          // they must still face the same side with a substantial margin.
          assert.ok(normalAgreement>.7,`no discrete triangle folds against the continuous tissue field (${mode}, ${phase}, ${offset}: ${normalAgreement})`);
        }
      }
    }
  }
  report.glideHeightRatio=heights[0]/heights[1];
  assert.ok(report.glideHeightRatio>.78&&report.glideHeightRatio<.9,'a moving glide narrows the actual fan while a loaded kick opens it');
  const shared={};
  deform(membrane.binding,createMotion(quiet,shared),membrane.output);
  const firstHeight=height(membrane);
  createMotion({...quiet,stroke:1,muscle3:1},shared);
  deform(membrane.binding,shared,membrane.output);
  assert.ok(height(membrane)>firstHeight*1.1,'mutating the same controller output object invalidates the shared tissue cache');
  const quietA=createMotion(quiet),quietB=createMotion({...quiet,phase:Math.PI,age:5});
  for(let i=0;i<membrane.base.length/3;i+=13){
    const point=Array.from(membrane.base.slice(i*3,i*3+3));
    assert.ok(distance(deformPoint(point,frame,quietA),deformPoint(point,frame,quietB))<1e-10,
      'a quiet glide has no perpetual caudal oscillator');
  }
  const braking=createMotion({...quiet,brake:1}),rest=createMotion({...quiet,speed:0,forwardSpeed:0});
  let upper=0,lower=0;
  for(let i=0;i<membrane.base.length/3;i++)if(membrane.uv[i*2+1]<.001){
    const point=Array.from(membrane.base.slice(i*3,i*3+3));
    const dz=deformPoint(point,frame,braking)[2]-deformPoint(point,frame,rest)[2];
    upper=Math.max(upper,dz);lower=Math.min(lower,dz);
  }
  assert.ok(upper>.012&&lower<-.012,'braking bends the dorsal and ventral lobes oppositely in the actual mesh');
  report.brakingLobes=[upper,lower];
  const turnA=createMotion({...quiet,stroke:1,muscle3:1,turnRecruitment3:.25,phase:0});
  const turnB=createMotion({...quiet,stroke:1,muscle3:1,turnRecruitment3:.25,phase:Math.PI});
  const tip=[frame.root.x+frame.length,frame.root.y+frame.upper,frame.root.z];
  assert.ok(distance(deformPoint(tip,frame,turnA),deformPoint(tip,frame,turnB))<1e-10,
    'a recruited finite turn does not regain the cancelled opposing carrier at its tail fan');
  const turnController=createController();
  turnController.sample({...quiet,age:0,muscle3:1,stroke:1});
  const loaded=turnController.sample({...quiet,age:1/120,muscle3:1,stroke:1,turnRecruitment3:.25});
  assert.ok(distance(deformPoint(tip,frame,loaded),deformPoint(tip,frame,{...loaded,phase:loaded.phase+Math.PI}))<1e-10,
    'stored compliance cannot restore a counter-wave after turn recruitment');
  for(const mode of modes){
    const first=createMotion({...mode,phase:0}),last=createMotion({...mode,phase:tau});
    for(let i=0;i<membrane.base.length/3;i+=19){
      const point=Array.from(membrane.base.slice(i*3,i*3+3));
      assert.ok(distance(deformPoint(point,frame,first),deformPoint(point,frame,last))<1e-10,
        'the complete caudal phase cycle wraps without a tissue seam');
    }
  }
  assert.deepEqual(createMotion({...quiet,poseHistory:false,muscle3:1}),createMotion({...quiet,poseHistory:false,muscle3:0}),
    'held anatomy samples ignore stale live muscle history');

  const controller=createController();let previous;
  for(let step=0;step<=480;step++){
    const age=step/120,active=age<.7||age>3.1;
    const fish={...quiet,age,phase:age*7,stroke:active?1:0,muscle3:active?1:0,brake:age>1.6&&age<2.3?1:0};
    const motion=controller.sample(fish);
    deform(membrane.binding,motion,membrane.output);
    if(previous)for(let i=0;i<membrane.output.length;i+=3){
      const delta=Math.hypot(...[0,1,2].map(axis=>membrane.output[i+axis]-previous[i+axis]));
      report.maximumStep=Math.max(report.maximumStep,delta);
      assert.ok(delta<.0025,`abrupt command changes settle through finite caudal tissue motion at 120 Hz (${delta})`);
    }
    const saved=Array.from(membrane.output),paused=controller.sample({...fish,brake:1-fish.brake});
    deform(membrane.binding,paused,membrane.output);
    assert.deepEqual(Array.from(membrane.output),saved,'paused observation is idempotent');
    previous=Float64Array.from(membrane.output);
  }
  const held={...quiet,poseHistory:false,age:4,phase:1,stroke:1};
  const reset=controller.sample(held,'held-power'),fresh=createController().sample(held,'held-power');
  assert.deepEqual(reset,fresh,'switching inspection poses resets stale caudal control history even at the same age');
  assert.deepEqual(controller.sample({...quiet,age:0},'live'),createController().sample({...quiet,age:0},'live'),
    'a deterministic replay rewind starts with fresh caudal state');
  const gap={...quiet,age:3,phase:2,brake:1};
  assert.deepEqual(controller.sample(gap,'live'),createController().sample(gap,'live'),
    'a missing observation interval does not invent intermediate fin commands');
  for(const fin of fins){
    const attributes=fin.mesh.geometry.attributes;
    assert.deepEqual(attributes.position.array,fin.base,'authoring vertices remain unchanged');
    assert.deepEqual(attributes.uv.array,fin.uv,'radial coordinates remain unchanged');
    assert.deepEqual(attributes.skinWeight.array,fin.weights,'skin weights remain unchanged');
    assert.deepEqual(Array.from(attributes.skinIndex.array),Array.from(fin.joints),'joint assignments remain unchanged');
    assert.deepEqual(fin.mesh.geometry.index.array,fin.index,'topology remains unchanged');
  }
  return report;
}

export async function assertCaudalMaterialLengths(deform=deformCaudalGeometry){
  const fins=await geometry(),membrane=fins.find(fin=>fin.name.includes('membrane'));
  const frame=createCaudalFrame(membrane.base,membrane.uv),paths=materialPaths(membrane);
  membrane.binding=bindCaudalGeometry(membrane.base,frame);membrane.output=new Float32Array(membrane.base.length);
  const report={maximumRadialArcStrain:0,maximumRadialSegmentStrain:0};
  for(const fish of [{speed:.24,stroke:0},{speed:.24,stroke:1},{speed:.24,stroke:0,brake:1}]){
    for(let phase=0;phase<12;phase++){
      deform(membrane.binding,createCaudalMotion({...fish,phase:phase*tau/12}),membrane.output);
      assertMaterialLengths(membrane,paths,report);
    }
  }
  return report;
}

export async function assertCaudalMutations(){
  const rejects=async(run,label)=>{
    await assert.rejects(run,{code:'ERR_ASSERTION'},`${label} must fail the actual-asset checks`);
  };
  await rejects(()=>assertCaudalGeometry(createCaudalFrame,fish=>({...createCaudalMotion(fish),adduction:0,braking:0})),
    'the former single flat sway');
  await rejects(()=>assertCaudalGeometry(createCaudalFrame,createCaudalMotion,createCaudalController,bindCaudalGeometry,
    (binding,motion,output)=>deformCaudalGeometry(binding,binding.values.length>10000?{...motion,adduction:0,braking:0,amplitude:0}:motion,output)),
    'a disconnected undeformed ray mesh');
  await rejects(()=>assertCaudalGeometry(createCaudalFrame,fish=>createCaudalMotion({...fish,turnRecruitment3:0})),
    'an independent carrier fighting the recruited turn');
  await rejects(()=>assertCaudalGeometry(createCaudalFrame,createCaudalMotion,()=>({sample:createCaudalMotion})),
    'instantaneous kick, glide and braking shape changes');
  await rejects(()=>assertCaudalMaterialLengths((binding,motion,output)=>{
    output.set(binding.frame.base);
    for(let i=0;i<output.length;i+=3){
      const s=Math.max(0,Math.min(1,(output[i]-binding.frame.root.x)/binding.frame.length));
      output[i+1]=binding.frame.root.y+(output[i+1]-binding.frame.root.y)*(1-motion.adduction*s*s*(3-2*s));
    }
    return output;
  }),'the former compressed material fan');
  await rejects(()=>assertCaudalMaterialLengths((binding,motion,output)=>{
    deformCaudalGeometry(binding,motion,output);
    for(const {indices} of binding.frame.paths)for(let j=1;j<indices.length-1;j++){
      // Keep every root and endpoint unchanged while straining its interior.
      output[indices[j]*3]+=.003*Math.sin(Math.PI*j/(indices.length-1));
    }
    return output;
  }),'endpoint preservation that hides interior ray strain');
  const fins=await geometry(),rays=fins.find(fin=>fin.name.includes('rays')),paths=tubePaths(rays);
  const overlapping={...rays,base:Float32Array.from(rays.base)},fixed=paths[0].rings,moved=paths[3].rings;
  for(let j=0;j<moved.length;j++){
    const source=ringCenter(moved[j],rays.base),target=ringCenter(fixed[j],rays.base);
    for(const i of moved[j])for(let axis=0;axis<3;axis++)overlapping.base[i*3+axis]+=target[axis]-source[axis];
  }
  assert.throws(()=>assertTubeClearance(overlapping,paths),{code:'ERR_ASSERTION'},
    'unrelated crossing tube centerlines must fail the actual three dimensional clearance guard');
  return 7;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const report=await assertCaudalGeometry();
  const mutations=await assertCaudalMutations();
  console.log(`Actual caudal geometry passed (${mutations} mutations rejected): ${JSON.stringify(report)}`);
}
