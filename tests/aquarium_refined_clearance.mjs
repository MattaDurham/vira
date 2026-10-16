/* Bounded actual-asset paired-fin/body clearance join for the refined koi.
 * The source-informed turn is authored animation, not calibrated biology.
 * Heavy triangle/occupancy checks run at fourteen physical checkpoints only;
 * every physical tick still observes the real independent fin histories. */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve} from 'node:path';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3} from '../static/vendor/three.webgpu.js';
import {createKoiInstance} from '../static/aquarium-fish-instance.js';
import {createFinController} from '../static/aquarium-fins.js';
import {finClearanceFactor} from '../static/aquarium-fin-clearance.js';
import {createSourceSteeringController} from '../static/aquarium-source-steering.js';
import {createStudyRandom} from '../static/aquarium-study.js';
import {tankCenterZ} from '../static/aquarium-space.js';

// BEGIN EXACT LEGACY TRIANGLE PREDICATES
// Test helper only: all arrays must be in the same current deformed space.
// Triangle predicates include transverse crossing and coplanar overlap with a numerical tolerance.
// Open-skin winding is diagnostic, not a watertight-solid certificate.
const EPS=1e-9;
const overlap=(a,n)=>a[0]<=n.hi[0]+EPS&&a[3]>=n.lo[0]-EPS&&a[1]<=n.hi[1]+EPS&&a[4]>=n.lo[1]-EPS&&a[2]<=n.hi[2]+EPS&&a[5]>=n.lo[2]-EPS;
function triangle(values,index,tri,out){for(let k=0;k<3;k++){const i=index[tri*3+k]*3;out[k*3]=values[i];out[k*3+1]=values[i+1];out[k*3+2]=values[i+2];}return out;}
function segmentTriangle(a,i,j,b){
 const x=a[i],y=a[i+1],z=a[i+2],dx=a[j]-x,dy=a[j+1]-y,dz=a[j+2]-z;
 const e1x=b[3]-b[0],e1y=b[4]-b[1],e1z=b[5]-b[2],e2x=b[6]-b[0],e2y=b[7]-b[1],e2z=b[8]-b[2];
 const px=dy*e2z-dz*e2y,py=dz*e2x-dx*e2z,pz=dx*e2y-dy*e2x,det=e1x*px+e1y*py+e1z*pz;
 if(Math.abs(det)<1e-14)return false;
 const tx=x-b[0],ty=y-b[1],tz=z-b[2],u=(tx*px+ty*py+tz*pz)/det;if(u< -EPS||u>1+EPS)return false;
 const qx=ty*e1z-tz*e1y,qy=tz*e1x-tx*e1z,qz=tx*e1y-ty*e1x,v=(dx*qx+dy*qy+dz*qz)/det;if(v< -EPS||u+v>1+EPS)return false;
 const t=(e2x*qx+e2y*qy+e2z*qz)/det;return t>=-EPS&&t<=1+EPS;
}
function coplanar(a,b){
 const x=a[3]-a[0],y=a[4]-a[1],z=a[5]-a[2],u=a[6]-a[0],v=a[7]-a[1],w=a[8]-a[2];
 const nx=y*w-z*v,ny=z*u-x*w,nz=x*v-y*u,n=Math.hypot(nx,ny,nz);if(n<1e-14)return false;
 for(let i=0;i<9;i+=3)if(Math.abs(nx*(b[i]-a[0])+ny*(b[i+1]-a[1])+nz*(b[i+2]-a[2]))>1e-8*n)return false;
 const drop=Math.abs(nx)>Math.abs(ny)?Math.abs(nx)>Math.abs(nz)?0:2:Math.abs(ny)>Math.abs(nz)?1:2;
 const k=drop===0?1:0,l=drop===2?1:2;
 const orient=(p,i,q,j,r,h)=>(q[j+k]-p[i+k])*(r[h+l]-p[i+l])-(q[j+l]-p[i+l])*(r[h+k]-p[i+k]);
 const on=(p,i,q,j,r,h)=>Math.min(p[i+k],q[j+k])-EPS<=r[h+k]&&r[h+k]<=Math.max(p[i+k],q[j+k])+EPS&&Math.min(p[i+l],q[j+l])-EPS<=r[h+l]&&r[h+l]<=Math.max(p[i+l],q[j+l])+EPS;
 for(let i=0;i<9;i+=3)for(let j=0;j<9;j+=3){const ni=(i+3)%9,nj=(j+3)%9,c=orient(a,i,a,ni,b,j),d=orient(a,i,a,ni,b,nj),e=orient(b,j,b,nj,a,i),f=orient(b,j,b,nj,a,ni);if(c*d<0&&e*f<0)return true;if(Math.abs(c)<1e-12&&on(a,i,a,ni,b,j)||Math.abs(d)<1e-12&&on(a,i,a,ni,b,nj)||Math.abs(e)<1e-12&&on(b,j,b,nj,a,i)||Math.abs(f)<1e-12&&on(b,j,b,nj,a,ni))return true;}
 const inside=(p,q)=>{const c=orient(q,0,q,3,p,0),d=orient(q,3,q,6,p,0),e=orient(q,6,q,0,p,0);return c>=-1e-12&&d>=-1e-12&&e>=-1e-12||c<=1e-12&&d<=1e-12&&e<=1e-12;};
 return inside(a,b)||inside(b,a);
}
function trianglesIntersect(a,b){
  for(const q of [a,b]){
    const x=q[3]-q[0],y=q[4]-q[1],z=q[5]-q[2],u=q[6]-q[0],v=q[7]-q[1],w=q[8]-q[2];
    if(Math.hypot(y*w-z*v,z*u-x*w,x*v-y*u)<1e-14)return false;
  }
  return segmentTriangle(a,0,3,b)||segmentTriangle(a,3,6,b)||segmentTriangle(a,6,0,b)||segmentTriangle(b,0,3,a)||segmentTriangle(b,3,6,a)||segmentTriangle(b,6,0,a)||coplanar(a,b);
}

function createDistalPatch(index,uv,minSpan=.25){
 const retained=[],parent=new Int32Array(uv.length/2);for(let i=0;i<parent.length;i++)parent[i]=i;
 const root=i=>{while(parent[i]!==i){parent[i]=parent[parent[i]];i=parent[i];}return i;};
 const join=(a,b)=>{parent[root(a)]=root(b);};
 for(let t=0;t<index.length/3;t++){const a=index[t*3],b=index[t*3+1],c=index[t*3+2];if(Math.max(uv[a*2+1],uv[b*2+1],uv[c*2+1])>1-minSpan+1e-8)continue;retained.push(t);join(a,b);join(a,c);}
 const components=new Map();for(const t of retained){const id=root(index[t*3]),score=3-uv[index[t*3]*2+1]-uv[index[t*3+1]*2+1]-uv[index[t*3+2]*2+1],old=components.get(id);if(!old||score>old.score)components.set(id,{tri:t,score});}
 return {triangles:Uint32Array.from(retained),representatives:[...components.values()].map(c=>c.tri)};
}

function createBodyOracle(index,initial,{leafSize=12}={}){
 let positions=initial;const total=index.length/3,order=Uint32Array.from({length:total},(_,i)=>i),centroid=new Float64Array(total*3),nodes=[],a=new Float64Array(9),b=new Float64Array(9),box=new Float64Array(6),stack=[];
 for(let t=0;t<total;t++)for(let axis=0;axis<3;axis++)centroid[t*3+axis]=(initial[index[t*3]*3+axis]+initial[index[t*3+1]*3+axis]+initial[index[t*3+2]*3+axis])/3;
 function build(start,end){const id=nodes.length,node={start,end,left:-1,right:-1,lo:[Infinity,Infinity,Infinity],hi:[-Infinity,-Infinity,-Infinity]};nodes.push(node);if(end-start>leafSize){const lo=[Infinity,Infinity,Infinity],hi=[-Infinity,-Infinity,-Infinity];for(let i=start;i<end;i++)for(let k=0;k<3;k++){const c=centroid[order[i]*3+k];lo[k]=Math.min(lo[k],c);hi[k]=Math.max(hi[k],c);}let axis=0;for(let k=1;k<3;k++)if(hi[k]-lo[k]>hi[axis]-lo[axis])axis=k;order.subarray(start,end).sort((x,y)=>centroid[x*3+axis]-centroid[y*3+axis]);const mid=(start+end)>>1;node.left=build(start,mid);node.right=build(mid,end);}return id;}
 build(0,total);
 function update(values){positions=values;for(let n=nodes.length-1;n>=0;n--){const node=nodes[n];for(let k=0;k<3;k++){node.lo[k]=Infinity;node.hi[k]=-Infinity;}if(node.left<0){for(let i=node.start;i<node.end;i++){const t=order[i];for(let v=0;v<3;v++){const offset=index[t*3+v]*3;for(let k=0;k<3;k++){const value=positions[offset+k];if(value<node.lo[k])node.lo[k]=value;if(value>node.hi[k])node.hi[k]=value;}}}}else{const l=nodes[node.left],r=nodes[node.right];for(let k=0;k<3;k++){node.lo[k]=Math.min(l.lo[k],r.lo[k]);node.hi[k]=Math.max(l.hi[k],r.hi[k]);}}}return api;}
 function crossing(values,finIndex,patch){let candidates=0;for(const t of patch.triangles){triangle(values,finIndex,t,a);for(let k=0;k<3;k++){box[k]=Math.min(a[k],a[k+3],a[k+6]);box[k+3]=Math.max(a[k],a[k+3],a[k+6]);}stack.length=0;stack.push(0);while(stack.length){const node=nodes[stack.pop()];if(!overlap(box,node))continue;if(node.left>=0){stack.push(node.left,node.right);continue;}for(let i=node.start;i<node.end;i++){const bodyTriangle=order[i];candidates++;triangle(positions,index,bodyTriangle,b);if(trianglesIntersect(a,b))return {kind:'crossing',finTriangle:t,bodyTriangle,candidates};}}}return {kind:'clear',candidates};}
 function winding(point){const [px,py,pz]=point,root=nodes[0];if(px<root.lo[0]||px>root.hi[0]||py<root.lo[1]||py>root.hi[1]||pz<root.lo[2]||pz>root.hi[2])return 0;let sum=0;for(let t=0;t<total;t++){const ia=index[t*3]*3,ib=index[t*3+1]*3,ic=index[t*3+2]*3;const ax=positions[ia]-px,ay=positions[ia+1]-py,az=positions[ia+2]-pz,bx=positions[ib]-px,by=positions[ib+1]-py,bz=positions[ib+2]-pz,cx=positions[ic]-px,cy=positions[ic+1]-py,cz=positions[ic+2]-pz;const la=Math.hypot(ax,ay,az),lb=Math.hypot(bx,by,bz),lc=Math.hypot(cx,cy,cz);if(Math.min(la,lb,lc)<1e-10)return NaN;const numerator=ax*(by*cz-bz*cy)+ay*(bz*cx-bx*cz)+az*(bx*cy-by*cx),denominator=la*lb*lc+(ax*bx+ay*by+az*bz)*lc+(bx*cx+by*cy+bz*cz)*la+(cx*ax+cy*ay+cz*az)*lb;sum+=2*Math.atan2(numerator,denominator);}return sum/(4*Math.PI);}
 function occupancy(values,finIndex,patch){return patch.representatives.map(t=>{triangle(values,finIndex,t,a);const point=[(a[0]+a[3]+a[6])/3,(a[1]+a[4]+a[7])/3,(a[2]+a[5]+a[8])/3],value=winding(point);return {finTriangle:t,point,winding:value,classification:Math.abs(value)>.8?'inside':Math.abs(value)<.2?'outside':'ambiguous'};});}
 const api={update,crossing,winding,occupancy,get nodeCount(){return nodes.length;}};return update(initial);
}
// END EXACT LEGACY TRIANGLE PREDICATES

const predicateStart='// Test helper only: all arrays must be in the same current deformed space.',
  predicateEnd='\n\n{\n  const cube=',
  ownStart='// BEGIN EXACT LEGACY TRIANGLE PREDICATES\n',
  ownEnd='// END EXACT LEGACY TRIANGLE PREDICATES';
async function assertPredicateProvenance(){
  const original=(await readFile(new URL('./aquarium_fin_clearance.mjs',import.meta.url),'utf8')).replace(/\r\n/g,'\n'),
    own=(await readFile(new URL(import.meta.url),'utf8')).replace(/\r\n/g,'\n'),start=original.indexOf(predicateStart),end=original.indexOf(predicateEnd,start),
    copiedStart=own.indexOf(ownStart)+ownStart.length,copiedEnd=own.indexOf(ownEnd,copiedStart);
  assert.ok(start>=0&&end>start&&copiedStart>=ownStart.length&&copiedEnd>copiedStart,'the exact independent predicate block has explicit source boundaries');
  assert.equal(own.slice(copiedStart,copiedEnd).trimEnd(),original.slice(start,end).trimEnd(),
    'the copied pure triangle BVH, distal patch, and occupancy predicates must remain identical to the legacy criterion after newline normalization');
}

function parseOptions(args){
  const paths={asset:new URL('../static/aquarium/refined/koi.glb',import.meta.url),mass:new URL('../static/aquarium/refined/body-mass.json',import.meta.url)};
  for(let i=0;i<args.length;i+=2){
    const key=args[i]?.slice(2);if(!['--asset','--mass'].includes(args[i])||!args[i+1])throw new Error('Usage: node tests/aquarium_refined_clearance.mjs [--asset path] [--mass path]');
    paths[key]=resolve(args[i+1]);
  }
  return paths;
}
const cloneMotion=motion=>({...motion,samples:Float64Array.from(motion.samples)});
const clonePacket=packet=>({...packet,...Object.fromEntries(['b','v','a','activity','load'].map(key=>[key,Array.from(packet[key])]))});
const magnitude=values=>Math.max(...Array.from(values,Math.abs));
const finKeys=['pectoral:1','pectoral:-1','pelvic:1','pelvic:-1'];
function assertCommittedSource(fish,packet){
  for(const [key,value] of [['turnAnterior',packet.b[0]],['turnTrunk',packet.b[1]],['turnPosterior',packet.b[2]],
    ['turnSourceWeight',packet.sourceReplacementWeight],['turnFinClearance',packet.finClearance]])
    assert.ok(Math.abs(fish[key]-value)<1e-13,`${key}: the actual pose consumes the committed source packet`);
  assert.equal(fish.turnGestureSerial,packet.serial,'checkpoint phase belongs to the actual committed gesture');
  if(packet.active)assert.ok(Math.abs(Math.atan2(Math.sin(fish.yaw-packet.headHeading),Math.cos(fish.yaw-packet.headHeading)))<2e-10,
    'actual head and regional body consume the same committed source clock');
}
function observeFinHistory(instance){
  const observed=new Map(),references=new Map(),uniqueAges=new Map();
  for(const family of ['pectoral','pelvic'])for(const side of [-1,1]){
    const key=`${family}:${side}`,controller=instance.pairedControllers[family][side],sample=controller.sample;
    references.set(key,createFinController(side,family==='pectoral'));uniqueAges.set(key,[]);
    controller.sample=(fish,poseKey)=>{
      const output=sample(fish,poseKey),ages=uniqueAges.get(key);
      if(ages.at(-1)!==fish.age)ages.push(fish.age);
      observed.set(key,cloneMotion(output));return output;
    };
  }
  assert.equal(new Set(references.values()).size,4,'each fin has its own independent command-history oracle');
  let maximumDifference=0;
  function compare(fish){
    for(const key of finKeys){
      const actual=observed.get(key),expected=references.get(key).sample(fish);
      assert.ok(actual,`${key} observes the actual physical tick`);
      for(const name of ['passive','flap','trail','incidence',...Array.from({length:66},(_,i)=>i)]){
        const difference=typeof name==='number'?Math.abs(actual.samples[name]-expected.samples[name]):Math.abs(actual[name]-expected[name]);
        maximumDifference=Math.max(maximumDifference,difference);
        assert.ok(difference<1e-13,`${key}: the rendered fin uses every independently observed 120 Hz command (${name})`);
      }
    }
  }
  function snapshot(){return Object.fromEntries(finKeys.map(key=>{
    const samples=observed.get(key)?.samples??[],ages=uniqueAges.get(key),distal=Array.from(samples).slice(-16);
    return [key,{observations:ages.length,span:ages.length?ages.at(-1)-ages[0]:0,maximum:magnitude(samples),distalMaximum:magnitude(distal)}];
  }));}
  return {compare,snapshot,get maximumDifference(){return maximumDifference;}};
}

// Read the actual uploaded Float32 skinning palette after live pose/deformation.
// All triangles share asset-local current space; no posed rebind is performed.
function uploadedPositions(asset,mesh){
  mesh.skeleton.update();const a=mesh.geometry.attributes,palette=mesh.skeleton.boneMatrices,
    out=new Float64Array(a.position.count*3),bind=new Vector3(),point=new Vector3(),
    post=asset.matrixWorld.clone().invert().multiply(mesh.matrixWorld).multiply(mesh.bindMatrixInverse);
  for(let i=0;i<a.position.count;i++){
    bind.fromBufferAttribute(a.position,i).applyMatrix4(mesh.bindMatrix);point.set(0,0,0);
    for(let slot=0;slot<4;slot++){
      const weight=a.skinWeight.getComponent(i,slot);if(!weight)continue;const offset=a.skinIndex.getComponent(i,slot)*16;
      point.x+=weight*(palette[offset]*bind.x+palette[offset+4]*bind.y+palette[offset+8]*bind.z+palette[offset+12]);
      point.y+=weight*(palette[offset+1]*bind.x+palette[offset+5]*bind.y+palette[offset+9]*bind.z+palette[offset+13]);
      point.z+=weight*(palette[offset+2]*bind.x+palette[offset+6]*bind.y+palette[offset+10]*bind.z+palette[offset+14]);
    }
    point.applyMatrix4(post).toArray(out,i*3);
    assert.ok(Number.isFinite(point.x)&&Number.isFinite(point.y)&&Number.isFinite(point.z),'actual uploaded body/paired material remains finite');
  }
  return out;
}
function pairedPatches(instance){
  const paired=instance.fins.filter(record=>record.paired).map(record=>{
    // Exact legacy material-span criterion: tube UVs are deliberately unused.
    // Only the intentional bind-derived proximal 25 percent is exempted.
    const uv=new Float64Array(record.binding.sides.length*2);
    for(let i=0;i<record.binding.sides.length;i++)uv[i*2+1]=1-Math.sqrt(record.binding.values[i*7+3]);
    const patch=createDistalPatch(record.mesh.geometry.index.array,uv,.25);
    assert.ok(patch.triangles.length>100,`${record.mesh.name}: the full substantial distal surface is retained`);
    return {...record,patch};
  });
  assert.equal(paired.length,6,'all four actual paired membranes and both complete embedded ray meshes are inspected');
  assert.equal(paired.filter(record=>record.mesh.name.includes('membrane')).length,4,'all four connected paired membranes participate in occupancy checks');
  return paired;
}
function reportFailure(context,mesh,result){
  const failure={...context,mesh,...result};
  console.error(JSON.stringify({refinedClearanceFailure:failure}));
  assert.fail(`${mesh}: actual distal fin/body selfcontact at time ${context.time}, phase ${context.phase}, sign ${context.sign}: ${JSON.stringify(result)}`);
}
function createInspector(instance,report){
  const skin=instance.asset.getObjectByName('Koi_skin'),paired=pairedPatches(instance);assert.ok(skin?.isSkinnedMesh,'the actual exterior skin supplies body triangles');
  let oracle;
  return (label,sign,packet)=>{
    instance.asset.updateMatrixWorld(true);const body=uploadedPositions(instance.asset,skin);
    if(!oracle)oracle=createBodyOracle(skin.geometry.index.array,body);else oracle.update(body);
    const context={label,sign,time:instance.fish.age,phase:packet.age,phaseClock:packet.phaseClock,serial:packet.serial,
      bend:Array.from(packet.b),preparing:packet.preparing,finClearance:packet.finClearance,sourceReplacementWeight:packet.sourceReplacementWeight};
    const checkpoint={...context,meshes:[]};
    for(const record of paired){
      const positions=uploadedPositions(instance.asset,record.mesh),index=record.mesh.geometry.index.array,
        result=oracle.crossing(positions,index,record.patch);
      if(result.kind!=='clear')reportFailure(context,record.mesh.name,result);
      const occupancy=record.mesh.name.includes('membrane')?oracle.occupancy(positions,index,record.patch):[];
      for(const component of occupancy){
        report.maximumOccupancy=Math.max(report.maximumOccupancy,Math.abs(component.winding));
        if(component.classification!=='outside')reportFailure(context,record.mesh.name,{kind:'occupancy',...component,bodyTriangle:null});
      }
      report.triangles+=record.patch.triangles.length;report.candidates+=result.candidates;
      checkpoint.meshes.push({name:record.mesh.name,distalTriangles:record.patch.triangles.length,components:occupancy.length,
        maximumOccupancy:occupancy.length?Math.max(...occupancy.map(component=>Math.abs(component.winding))):null});
    }
    report.checkpoints.push(checkpoint);
  };
}

function sourceFactory(store,data){return()=>{
  store.factories++;const controller=createSourceSteeringController(data),commit=controller.commit,request=controller.request;
  controller.commit=proposal=>{
    assert.ok(Math.abs(proposal.dt-1/120)<1e-15,'the live factory commits one 120 Hz physical interval');
    store.commits++;store.packet=clonePacket(commit(proposal));return controller.sample();
  };
  controller.request=(angle,age,options)=>{
    const accepted=request(angle,age,options);
    if(accepted&&Math.abs(angle)>2.5)store.requests.push({angle,age,urgent:Boolean(options?.urgent),
      headJet:{yaw:options?.headYaw,rate:options?.headRate,acceleration:options?.headAcceleration},history:store.history?.snapshot()});
    if(accepted)store.acceptedRequests.push({angle,age,urgent:Boolean(options?.urgent),serial:controller.sample().serial});
    return accepted;
  };
  store.controller=controller;store.packet=clonePacket(controller.sample());return controller;
};}
async function route(source,mass,data,sign){
  const store={factories:0,commits:0,requests:[],acceptedRequests:[]},random=createStudyRandom(913),
    instance=createKoiInstance(source,{massProfile:mass,random,turnControllerFactory:sourceFactory(store,data)}),history=observeFinHistory(instance);
  store.history=history;random.reset();instance.reset();Object.assign(instance.fish,{x:.3,y:.84,z:tankCenterZ,yaw:0,pitch:0,bank:0});
  // A real small fin-steering episode populates measured command histories.
  // No hand-written fin commands or held-pose substitute enters this route.
  instance.swimmer.navigate({x:-.85,y:.84,z:tankCenterZ+sign*.13});
  history.compare(instance.fish);const report={sign,checkpoints:[],triangles:0,candidates:0,maximumOccupancy:0,
    maximumBend:0,maximumHeadingChange:0,minimumFinClearance:1,minimumPlannedClearance:1,maximumLoadedReplacementWeight:0,contacts:0};
  const inspect=createInspector(instance,report);let ticks=0,warmHistory,warmYaw;
  for(;ticks<240;ticks++){
    instance.step(1/120);history.compare(instance.fish);assertCommittedSource(instance.fish,store.packet);warmHistory=history.snapshot();
    const values=Object.values(warmHistory);
    if(instance.fish.age>.32&&values.every(value=>value.span>.24&&value.distalMaximum>1e-5)
      &&warmHistory['pectoral:1'].maximum>.02&&warmHistory['pectoral:-1'].maximum>.02){ticks++;break;}
  }
  assert.ok(ticks<240,'the actual preceding fin-steering episode must populate all four distal histories before the reverse request');
  warmYaw=instance.fish.yaw;inspect('warm-before-request',sign,store.packet);report.warmHistory=warmHistory;
  const wanted=warmYaw+sign*(Math.PI-.08),flick={x:instance.fish.x+.3*Math.cos(wanted),y:instance.fish.y,z:instance.fish.z-.3*Math.sin(wanted)};
  instance.swimmer.flick(flick);assert.equal(store.requests.length,1,'the real close flick issues one meaningful source U-turn');
  assert.ok(store.requests[0].urgent&&Math.sign(store.requests[0].angle)===sign,'the observed urgent reverse has the requested mirror sign');
  for(const value of Object.values(store.requests[0].history))assert.ok(value.distalMaximum>1e-5&&value.span>.24,'request observes the already populated real distal history');
  const requestedAt=instance.fish.age,requestedSerial=store.controller.sample().serial,seen=new Set(['warm-before-request']);
  let historyClearAt,bodyLoadAt,firstBodyLoad,firstHistoryClear,firstPreparation;
  function checkpoint(label){if(seen.has(label))return;inspect(label,sign,store.packet);seen.add(label);}
  for(let after=0;after<480;after++){
    instance.step(1/120);ticks++;history.compare(instance.fish);assertCommittedSource(instance.fish,store.packet);const packet=store.packet,fish=instance.fish,
      current=history.snapshot(),load=Math.max(...packet.b.map(Math.abs)),historyMagnitude=Math.max(...Object.values(current).map(value=>value.maximum));
    assert.equal(packet.serial,requestedSerial,'all U-turn checkpoints belong to the same actual committed gesture');
    report.maximumBend=Math.max(report.maximumBend,load);report.maximumHeadingChange=Math.max(report.maximumHeadingChange,Math.abs(packet.headHeading-warmYaw));
    report.minimumFinClearance=Math.min(report.minimumFinClearance,finClearanceFactor(fish));
    report.minimumPlannedClearance=Math.min(report.minimumPlannedClearance,packet.finClearance);
    if(load>2)report.maximumLoadedReplacementWeight=Math.max(report.maximumLoadedReplacementWeight,packet.sourceReplacementWeight);
    const phase={time:fish.age,phase:packet.age,phaseClock:packet.phaseClock,serial:packet.serial,load,
      preparing:packet.preparing,b:Array.from(packet.b),headHeading:packet.headHeading,headRate:packet.headRate,headAcceleration:packet.headAcceleration,
      plannedClearance:packet.finClearance,historyMagnitude,historyByFin:current};
    if(!firstPreparation&&packet.preparing)firstPreparation=phase;
    if(historyClearAt===undefined&&packet.finClearance===0&&historyMagnitude<1e-10){historyClearAt=fish.age;firstHistoryClear=phase;}
    if(bodyLoadAt===undefined&&load>1e-5){bodyLoadAt=fish.age;firstBodyLoad=phase;}
    if(bodyLoadAt!==undefined&&historyClearAt===undefined&&historyMagnitude>=1e-10){
      console.error(JSON.stringify({refinedClearanceFixtureFailure:{sign,requestedAt,requestedSerial,request:store.requests[0],firstPreparation,
        firstBodyLoad,firstHistoryClear:null,historyClearAt:null,bodyLoadAt,acceptedRequests:store.acceptedRequests}}));
      assert.fail('loaded distal histories must clear before the first issued source body load');
    }
    if(packet.preparing&&packet.finClearance<=.5&&packet.finClearance>0)checkpoint('pre-tuck');
    if(packet.finClearance===0&&historyMagnitude<1e-10&&load<1e-5&&packet.age>.12)checkpoint('tucked-history-drained');
    if(load>4&&packet.sourceReplacementWeight>1-1e-10)checkpoint('deep-C');
    if(seen.has('deep-C')&&load<1&&!packet.preparing&&packet.age>1)checkpoint('body-recovery');
    if(seen.has('deep-C')&&packet.finClearance>.2&&packet.finClearance<.8&&packet.age>1)checkpoint('fin-release');
    if(seen.has('fin-release')&&!packet.active&&packet.age>1.8){checkpoint('settled-after-turn');break;}
  }
  if(!(historyClearAt<=bodyLoadAt))console.error(JSON.stringify({refinedClearanceFixtureFailure:{sign,requestedAt,requestedSerial,firstBodyLoad,firstHistoryClear,
    acceptedRequests:store.acceptedRequests,completedCheckpoints:report.checkpoints.map(({label,time,phase,serial})=>({label,time,phase,serial}))}}));
  assert.ok(historyClearAt<=bodyLoadAt,'loaded distal histories clear before the first issued source body load');
  for(const label of ['pre-tuck','tucked-history-drained','deep-C','body-recovery','fin-release','settled-after-turn'])assert.ok(seen.has(label),`the actual route reaches checkpoint ${label}`);
  assert.equal(report.checkpoints.length,7,'each mirror inspects seven bounded actual physical checkpoints');
  assert.ok(report.maximumBend>4&&report.maximumHeadingChange>2.8,'the route includes a complete meaningful tight source U-turn');
  assert.equal(report.minimumPlannedClearance,0,'the actual source gesture issues its full planned pre-tuck');
  assert.ok(report.maximumLoadedReplacementWeight>1-1e-10,'meaningful source body loading reaches full source replacement');
  assert.equal(store.factories,1,'the live fish constructs exactly one injected source controller');
  assert.equal(store.commits,ticks,'every actual 120 Hz physical tick commits the source heading/body clock exactly once');
  report.ticks=ticks;report.requestedAt=requestedAt;report.requestedSerial=requestedSerial;report.requests=store.requests;
  report.acceptedRequests=store.acceptedRequests;report.historyClearAt=historyClearAt;report.bodyLoadAt=bodyLoadAt;
  report.maximumIndependentHistoryDifference=history.maximumDifference;report.contacts=instance.behavior.contacts;
  instance.dispose();return report;
}

export async function assertRefinedClearance(args=process.argv.slice(2)){
  const startedAt=Date.now();await assertPredicateProvenance();const paths=parseOptions(args),bytes=await readFile(paths.asset),mass=JSON.parse(await readFile(paths.mass,'utf8')),
    assetSHA256=createHash('sha256').update(bytes).digest('hex');
  assert.equal(mass.assetSHA256,assetSHA256,'refined clearance must test the selected matching asset/mass pair');
  const loader=new GLTFLoader().register(()=>({name:'RefinedClearanceGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())})),
    {scene:source}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),''),
    data=JSON.parse(await readFile(new URL('../static/aquarium/reference-turn.json',import.meta.url),'utf8'));
  const routes=[];for(const sign of [-1,1])routes.push(await route(source,mass,data,sign));
  assert.equal(routes.reduce((sum,row)=>sum+row.checkpoints.length,0),14,'bounded joined check inspects fourteen physical checkpoints in total');
  return {definition:'Actual refined paired-fin/body selfcontact at bounded authored source-turn checkpoints; open-skin winding is diagnostic, not a watertight solid certificate.',
    assetSHA256,runtimeSeconds:(Date.now()-startedAt)/1000,epsilon:EPS,minSpan:.25,occupancyThresholds:{outside:.2,inside:.8},routes};
}

console.log(JSON.stringify(await assertRefinedClearance()));
