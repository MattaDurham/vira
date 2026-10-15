import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Triangle,Vector3} from '../static/vendor/three.webgpu.js';
import {createFinFrame,createFinMotion,bindFinGeometry,deformFinGeometry,deformFinPoint} from '../static/aquarium-fins.js';

const tau=Math.PI*2;
const controls={speed:.14,forwardSpeed:.14,brake:1,stroke:.08,yawRate:.45,
  finSteer:.2,pitchRate:.08,pitch:.12,age:2};
const vertex=(values,index)=>new Vector3().fromArray(values,index*3);
const distance=(a,b)=>Math.hypot(...a.map((value,i)=>value-b[i]));

async function geometry(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  // Preserve the actual exported geometry, UVs, topology and skinning. Node
  // does not decode images; browser review separately checks fin shading.
  const loader=new GLTFLoader().register(()=>({name:'FinGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const fins=[];
  scene.traverse(mesh=>{
    if(!mesh.isMesh||!/^(Pectoral|Pelvic)_/.test(mesh.name))return;
    const attributes=mesh.geometry.attributes;
    fins.push({mesh,name:mesh.name,pectoral:mesh.name.startsWith('Pectoral'),
      base:Float32Array.from(attributes.position.array),uv:Float32Array.from(attributes.uv.array),
      index:mesh.geometry.index.array.slice(),
      weights:Float32Array.from(attributes.skinWeight.array),
      joints:Uint16Array.from(attributes.skinIndex.array)});
  });
  assert.equal(fins.length,6,'the actual asset has four paired membranes and two combined ray meshes');
  return fins;
}

function triangles(values,index){
  const result=[];
  for(let i=0;i<index.length;i+=3){
    const triangle=new Triangle(...[0,1,2].map(k=>vertex(values,index[i+k])));
    // Radial fans repeat the attachment once per ray. Their initial root
    // triangle has zero area and cannot define a closest point or normal.
    if(triangle.getArea()>1e-14)result.push({triangle,offset:i});
  }
  return result;
}

function projectedArea(values,index){
  let area=0;
  for(const {triangle} of triangles(values,index)){
    const u=triangle.b.clone().sub(triangle.a),v=triangle.c.clone().sub(triangle.a);
    area+=Math.abs(u.y*v.z-u.z*v.y)/2;
  }
  return area;
}

function derivative(point,frame,motion,deformPoint){
  const columns=[];
  for(let axis=0;axis<3;axis++){
    const plus=point.slice(),minus=point.slice(),h=1e-5;
    plus[axis]+=h;minus[axis]-=h;
    const a=deformPoint(plus,frame,motion),b=deformPoint(minus,frame,motion);
    columns.push(new Vector3(...a.map((value,i)=>(value-b[i])/(2*h))));
  }
  return columns;
}

function assertSurface(original,deformed,index,frame,motion,deformPoint){
  for(const {triangle,offset} of triangles(original,index)){
    const changed=new Triangle(...[0,1,2].map(k=>vertex(deformed,index[offset+k])));
    const ratio=changed.getArea()/triangle.getArea();
    assert.ok(ratio>.2&&ratio<5,'loaded tissue retains finite, plausible triangle area');
    const center=triangle.getMidpoint(new Vector3()).toArray();
    const jacobian=derivative(center,frame,motion,deformPoint);
    const transform=vector=>jacobian.reduce((sum,column,i)=>sum.addScaledVector(column,vector.getComponent(i)),new Vector3());
    const first=transform(triangle.b.clone().sub(triangle.a));
    const second=transform(triangle.c.clone().sub(triangle.a));
    const expected=first.cross(second).normalize(),actual=changed.getNormal(new Vector3());
    assert.ok(expected.dot(actual)>.8,'the tessellated membrane does not reverse across a local deformation');
  }
}

function assertRayAlignment(rays,membranes){
  const surfaces=Object.fromEntries(membranes.map(fin=>[fin.frame.side,triangles(fin.output,fin.index).map(item=>item.triangle)]));
  const point=new Vector3(),closest=new Vector3();let maximum=0;
  // Tube tessellation provides many nearly identical neighbours. Spread the
  // sample over both combined sides, main shafts and smaller branches.
  const stride=Math.max(1,Math.floor(rays.base.length/3/192));
  for(let i=0;i<rays.base.length/3;i+=stride){
    point.fromArray(rays.output,i*3);
    const side=Math.sign(rays.base[i*3+2]);let nearest=Infinity;
    for(const triangle of surfaces[side]){
      triangle.closestPointToPoint(point,closest);
      nearest=Math.min(nearest,point.distanceToSquared(closest));
    }
    maximum=Math.max(maximum,Math.sqrt(nearest));
  }
  assert.ok(maximum<.001,`fin rays remain embedded in their tissue; maximum sampled gap is ${(maximum*1000).toFixed(3)} mm`);
  return maximum;
}

// Injectable functions let the caller mutation-check the same real-asset
// assertions without rewriting production files or changing the fixture.
export async function assertFinGeometry(createFrame=createFinFrame,createMotion=createFinMotion,
  bind=bindFinGeometry,deform=deformFinGeometry,deformPoint=deformFinPoint){
  const fins=await geometry(),report={maximumRayGap:0,minimumJacobian:Infinity,maximumJacobian:-Infinity,
    minimumAreaChange:Infinity,maximumNonrigidChange:0};
  for(const pectoral of [true,false]){
    const family=fins.filter(fin=>fin.pectoral===pectoral),membranes=family.filter(fin=>fin.name.includes('membrane'));
    const rays=family.find(fin=>fin.name.includes('rays')),frames={};
    for(const fin of membranes){
      fin.frame=createFrame(fin.base,fin.uv);frames[fin.frame.side]=fin.frame;
      assert.deepEqual([fin.frame.root.x,fin.frame.root.y,fin.frame.root.z],Array.from(fin.base.slice(0,3)),
        'each fin uses its actual exported basal attachment');
    }
    for(const fin of family){
      fin.binding=bind(fin.base,frames);fin.output=new Float64Array(fin.base.length);
      const skin=fin.mesh.geometry.attributes;
      const ids=new Set();
      for(let i=0;i<skin.skinWeight.count;i++){
        assert.ok(Math.abs(skin.skinWeight.getX(i)-1)<1e-7,'paired-fin vertices retain a rigid basal joint attachment');
        ids.add(skin.skinIndex.getX(i));
      }
      assert.equal(ids.size,1,'the tissue or combined rays follow one attachment joint');
      fin.joint=fin.mesh.skeleton.bones[[...ids][0]].name;
    }
    assert.equal(membranes[0].joint,rays.joint,'rays and tissue share the same skinned attachment');
    assert.equal(membranes[1].joint,rays.joint,'both sides share the authored attachment joint');

    const phaseCount=16,areas=new Map(membranes.map(fin=>[fin,[]]));
    for(let phase=0;phase<phaseCount;phase++){
      const fish={...controls,finPhase:phase*tau/phaseCount};
      const motions={[-1]:createMotion(fish,-1,pectoral),1:createMotion(fish,1,pectoral)};
      for(const fin of family){
        deform(fin.binding,motions,fin.output);
        assert.ok(fin.output.every(Number.isFinite),'all tissue and ray coordinates remain finite');
      }
      for(const fin of membranes){
        const frame=fin.frame,motion=motions[frame.side],root=[frame.root.x,frame.root.y,frame.root.z];
        assert.ok(distance(deformPoint(root,frame,motion),root)<1e-10,'the basal pivot remains anchored during power and recovery');
        for(let i=0;i<fin.base.length/3;i++)if(fin.uv[i*2+1]>.999){
          assert.ok(distance(Array.from(fin.output.slice(i*3,i*3+3)),root)<1e-8,'every duplicated fan root remains attached');
        }
        for(let i=24;i<fin.base.length/3;i+=113){
          const point=Array.from(fin.base.slice(i*3,i*3+3));
          assert.ok(distance(deformPoint(point,frame,motion),Array.from(fin.output.slice(i*3,i*3+3)))<1e-10,
            'the bound geometry uses the same tissue deformation as individual points');
        }
        areas.get(fin).push(projectedArea(fin.output,fin.index));
        // Physical interior points exercise both sides, not just the pivot.
        for(let i=24;i<fin.base.length/3;i+=113){
          const jacobian=derivative(Array.from(fin.base.slice(i*3,i*3+3)),frame,motion,deformPoint);
          const determinant=jacobian[0].dot(jacobian[1].clone().cross(jacobian[2]));
          report.minimumJacobian=Math.min(report.minimumJacobian,determinant);
          report.maximumJacobian=Math.max(report.maximumJacobian,determinant);
          assert.ok(determinant>.98&&determinant<1.02,'fin cupping and feathering preserve a positive local volume');
        }
        if([0,3,11].includes(phase))assertSurface(fin.base,fin.output,fin.index,frame,motion,deformPoint);
        const samples=[8,Math.floor(fin.base.length/3/2),fin.base.length/3-1];
        for(let i=0;i<samples.length;i++)for(let j=i+1;j<samples.length;j++){
          const before=vertex(fin.base,samples[i]).distanceTo(vertex(fin.base,samples[j]));
          const after=vertex(fin.output,samples[i]).distanceTo(vertex(fin.output,samples[j]));
          report.maximumNonrigidChange=Math.max(report.maximumNonrigidChange,Math.abs(after-before));
        }
      }
      if([0,3,11].includes(phase))report.maximumRayGap=Math.max(report.maximumRayGap,assertRayAlignment(rays,membranes));
    }
    for(const fin of membranes){
      const a=areas.get(fin),change=(Math.max(...a)-Math.min(...a))/Math.max(...a);
      report.minimumAreaChange=Math.min(report.minimumAreaChange,change);
      assert.ok(change>(pectoral?.05:.025),'power and feathered recovery visibly change the head-on presented fin area');
      const quiet={...controls,speed:.24,forwardSpeed:.24,brake:0,stroke:0,yawRate:0,finSteer:0,pitchRate:0,pitch:0};
      const first=createMotion({...quiet,finPhase:0},fin.frame.side,pectoral);
      const second=createMotion({...quiet,age:5,finPhase:Math.PI},fin.frame.side,pectoral);
      for(let i=0;i<fin.base.length/3;i+=17){
        const point=Array.from(fin.base.slice(i*3,i*3+3));
        assert.ok(distance(deformPoint(point,fin.frame,first),deformPoint(point,fin.frame,second))<.0025,
          'an undisturbed glide does not continuously row or flutter its paired fins');
      }
      // This shared quiet interval falls between the independently timed
      // hover corrections. Advancing stroke phase alone must not row a rest.
      const resting={...quiet,speed:0,forwardSpeed:0,brake:1,age:3.4};
      const restA=createMotion({...resting,finPhase:1},fin.frame.side,pectoral);
      const restB=createMotion({...resting,finPhase:4},fin.frame.side,pectoral);
      for(let i=8;i<fin.base.length/3;i+=34){
        const point=Array.from(fin.base.slice(i*3,i*3+3));
        assert.ok(distance(deformPoint(point,fin.frame,restA),deformPoint(point,fin.frame,restB))<.00005,
          'a quiet hover interval remains still when only the unused stroke phase changes');
      }
    }
    const studyFin=membranes.find(fin=>fin.frame.side===1),frame=studyFin.frame;
    const point=Array.from(studyFin.base.slice(Math.floor(studyFin.base.length/6)*3,Math.floor(studyFin.base.length/6)*3+3));
    const paths={pectoral:[],pelvic:[]};let sideDifference=0;
    for(let phase=0;phase<phaseCount;phase++){
      const fish={...controls,finPhase:phase*tau/phaseCount};
      const left=deformPoint(point,frame,createMotion(fish,-1,pectoral));
      const right=deformPoint(point,frame,createMotion(fish,1,pectoral));
      sideDifference=Math.max(sideDifference,distance(left,right));
      paths.pectoral.push(deformPoint(point,frame,createMotion(fish,1,true)));
      paths.pelvic.push(deformPoint(point,frame,createMotion(fish,1,false)));
    }
    assert.ok(sideDifference>.0005,'the paired fins retain independently timed corrections');
    const excursion=path=>Math.max(...path.flatMap((a,i)=>path.slice(i+1).map(b=>distance(a,b))));
    assert.ok(excursion(paths.pelvic)<excursion(paths.pectoral)*.8,'pelvic deformation remains smaller than the pectoral control stroke');
    const held={...controls,poseHistory:false,finPhase:tau*.19};
    const before=deformPoint(point,frame,createMotion({...held,muscle3:0},1,pectoral));
    const after=deformPoint(point,frame,createMotion({...held,muscle3:1},1,pectoral));
    assert.ok(distance(before,after)<1e-10,'a held anatomy sample ignores stale live muscle history');
    for(const side of [-1,1])for(const index of [16,Math.floor(studyFin.base.length/6),studyFin.base.length/3-1]){
      const sample=Array.from(studyFin.base.slice(index*3,index*3+3));let first,previous;
      for(let step=0;step<=128;step++){
        const fish={...controls,finPhase:step*tau/128};
        const current=deformPoint(sample,frame,createMotion(fish,side,pectoral));
        first??=current;
        if(previous)assert.ok(distance(current,previous)<.003,'power, recovery and their transitions remain continuous over the complete cycle');
        if(step===128)assert.ok(distance(first,current)<1e-10,'the stroke wraps to its initial pose without a cycle seam');
        previous=current;
      }
    }
  }
  assert.ok(report.maximumNonrigidChange>.0002,'cupping and distal compliance change shape rather than only rotating a rigid sheet');
  for(const fin of fins){
    const attributes=fin.mesh.geometry.attributes;
    assert.deepEqual(attributes.position.array,fin.base,'deformation leaves the authored source vertices intact');
    assert.deepEqual(attributes.uv.array,fin.uv,'deformation leaves the authored radial tissue coordinates intact');
    assert.deepEqual(attributes.skinWeight.array,fin.weights,'deformation preserves the skin weights');
    assert.deepEqual(Array.from(attributes.skinIndex.array),Array.from(fin.joints),'deformation preserves the attachment indices');
    assert.deepEqual(fin.mesh.geometry.index.array,fin.index,'deformation preserves the mesh topology');
  }
  return report;
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const report=await assertFinGeometry();
  console.log(`Actual paired-fin geometry passed: ${JSON.stringify(report)}`);
}
