import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {finClearanceFactor,finClearanceFold} from '../static/aquarium-fin-clearance.js';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Matrix4} from '../static/vendor/three.webgpu.js';
import {createTurnController} from '../static/aquarium-turn.js';
import {applySwimPose} from '../static/aquarium-rig.js';
import {createSwimmer,createFinTaskController,finActuation,finPitchMoment,finRollMoment} from '../static/aquarium-motion.js';
import {createFinController,createFinFrame,bindFinGeometry,deformFinGeometry} from '../static/aquarium-fins.js';

const bend=(a,b=a,c=a)=>({turnBend1:a,turnBend2:b,turnBend3:c});
assert.equal(finClearanceFactor({}),1,'an unbent fish retains its paired-fin commands');
for(const value of [0,.2,1.2,1.6])for(const sign of [-1,1]){
  assert.equal(finClearanceFactor(bend(sign*value)),1,'ordinary smaller turns retain exact fin authority');
}
for(const value of [1.7,1.9,2.1,2.2,2.4]){
  const factor=finClearanceFactor(bend(value));
  assert.ok(Number.isFinite(factor)&&factor>=0&&factor<=1,'coordination remains a finite convex scale');
  assert.equal(factor,finClearanceFactor(bend(-value)),'mirrored body loading has identical clearance coordination');
}
assert.equal(finClearanceFactor(bend(2.4)),0,'a fully tight turn suppresses active paired-fin excursions');
assert.equal(finClearanceFactor({...bend(2.4),poseHistory:false}),1,'a held pose cannot inherit absent live turning histories');
assert.equal(finClearanceFactor({...bend(0),turnFinClearance:.3}),.3,'planned pre-tuck reaches the same command path before body loading');
assert.equal(finClearanceFactor({...bend(0),turnFinClearance:0}),0,'planned urgent clearance can settle the fins before loading');
assert.equal(finClearanceFold(1),0,'a clear body retains the authored neutral basal presentation');
assert.equal(finClearanceFold(0),-.30,'tight turns issue a bounded cranial pectoral fold');
assert.equal(finClearanceFold(0,false),-.30*.35,'pelvic presentation retains the family gain');

// Crossings of the most-loaded region must not introduce a selector cusp.
// A hard max would retain a nonvanishing one-sided slope jump as h shrinks.
const crossing=t=>finClearanceFactor(bend(1.9+t,1.9-t,1.7));
const slopeJump=h=>Math.abs((crossing(h)-crossing(0))/h-(crossing(0)-crossing(-h))/h);
assert.ok(slopeJump(1e-4)<slopeJump(2e-4)*.51,'regional load exchanges retain a continuous first derivative');
for(const boundary of [1.6,2.2]){
  const values=[];
  for(const h of [2e-4,1e-4]){
    const f=t=>finClearanceFactor(bend(boundary+t,0,0));
    values.push({velocity:Math.abs((f(h)-f(-h))/(2*h)),acceleration:Math.abs((f(h)-2*f(0)+f(-h))/(h*h))});
  }
  assert.ok(values[1].velocity<values[0].velocity*.27,'compact tuck has zero endpoint velocity');
  assert.ok(values[1].acceleration<values[0].acceleration*.53,'compact tuck has zero endpoint acceleration');
}
const plannedCommand=age=>{
  const turn=createTurnController();turn.request(Math.PI,0,{urgent:true});const gesture=turn.sample(age);
  const weight=finClearanceFactor({...bend(...gesture.b),turnFinClearance:gesture.finClearance});
  return [weight,finClearanceFold(weight)];
};
for(const boundary of [.12,.36+1.14*.72,.36+1.14*.72+.24]){
  const levels=[1/1920,1/3840].map(h=>{
    const [a,b,c]=[-1,0,1].map(offset=>plannedCommand(boundary+offset*h));
    return {velocity:Math.max(...a.map((value,i)=>Math.abs((c[i]-value)/(2*h)))),
      acceleration:Math.max(...a.map((value,i)=>Math.abs((c[i]-2*b[i]+value)/(h*h))))};
  });
  assert.ok(levels[1].velocity<levels[0].velocity*.28,'planned basal pre-tuck and release have zero endpoint velocity');
  assert.ok(levels[1].acceleration<levels[0].acceleration*.54,'planned basal pre-tuck and release have zero endpoint acceleration');
}

// The same factor reaches the visible basal angles, observed paddle history,
// and signed pitch/roll control moments. It cannot merely blank final output.
const physical={x:0,y:0,z:0,yaw:0,pitch:0,bank:0,yawRate:.4,pitchRate:0,bankRate:0,
  phase:0,age:0,drive:.2,stroke:.3,muscle1:.3,muscle2:.3,muscle3:.3,speed:.2,forwardSpeed:.2,
  finSteer:.2,pitchFin:.08,rollFin:.025,brake:0,verticalGain:0,twistGain:0};
for(const name of ['yawLag','pitchLag','bankLag'])for(let i=1;i<=3;i++)physical[name+i]=0;
for(const family of ['pectoral','pelvic'])for(const side of ['Plus','Minus']){
  physical[family+'Paddle'+side]=.06;physical[family+'ForeAft'+side]=.04;
}
const neutral={...physical,...bend(0)},partial={...physical,...bend(1.85,1.95,1.75)},factor=finClearanceFactor(partial);
for(const pectoral of [true,false])for(const side of [-1,1]){
  const reference=finActuation(neutral,side,pectoral),actual=finActuation(partial,side,pectoral);
  for(const key of ['spread','paddle','incidence'])assert.ok(Math.abs(actual[key]-reference[key]*factor)<1e-14,`${key} shares the issued clearance factor`);
  assert.ok(Math.abs(actual.foreAft-reference.foreAft*factor-finClearanceFold(factor,pectoral))<1e-14,'fore/aft combines the coordinated task and bounded basal fold');
}
assert.ok(Math.abs(finPitchMoment(partial)-finPitchMoment(neutral)*factor)<1e-14,'visible pitch incidence and control moment tuck together');
assert.ok(Math.abs(finRollMoment(partial)-finRollMoment(neutral)*factor)<1e-14,'visible roll incidence and control moment tuck together');
{
  const controller=createFinController(1,true);
  let last;
  for(let tick=0;tick<=36;tick++)last=controller.sample({...physical,...bend(2.4),age:tick/120});
  assert.ok(last.samples.every(value=>value===0),'history records the already coordinated paddle, leaving no hidden distal command');
  const frozen=Float64Array.from(last.samples),paused=controller.sample({...physical,...bend(0),age:.3});
  assert.equal(paused,last,'an unchanged simulation age remains an idempotent pause');
  assert.deepEqual(paused.samples,frozen,'a paused command cannot overwrite observed history');
  const released=controller.sample({...physical,...bend(0),age:37/120});
  assert.ok(released.samples[1]>0,'release can begin a newly observed proximal response');
  assert.equal(released.samples.at(-1),0,'release cannot invent earlier distal commands');
}

async function geometry(){
  const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
  const loader=new GLTFLoader().register(()=>({name:'FinClearanceGeometryOnly',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const joints=[],paired=[];
  asset.traverse(mesh=>{
    if(mesh.isBone&&mesh.name.startsWith('spine_'))joints.push(mesh);
    if(mesh.isSkinnedMesh&&/^(Pectoral|Pelvic)_/.test(mesh.name))paired.push({mesh,
      family:mesh.name.startsWith('Pectoral')?'pectoral':'pelvic',base:Float32Array.from(mesh.geometry.attributes.position.array)});
  });
  joints.sort((a,b)=>a.name.localeCompare(b.name));
  assert.equal(joints.length,34,'clearance retains the actual axial skeleton');
  const rest=joints.map(bone=>bone.quaternion.clone()),skin=asset.getObjectByName('Koi_skin');
  for(const family of ['pectoral','pelvic']){
    const members=paired.filter(record=>record.family===family);
    const frames=Object.fromEntries(members.filter(record=>record.mesh.name.includes('membrane')).map(record=>{
      const frame=createFinFrame(record.base,record.mesh.geometry.attributes.uv.array);return [frame.side,frame];
    }));
    for(const record of members)record.binding=bindFinGeometry(record.base,frames);
  }
  const vector=new Vector3(),post=new Matrix4();
  function positions(mesh){
    mesh.skeleton.update();const a=mesh.geometry.attributes,palette=mesh.skeleton.boneMatrices,out=new Float64Array(a.position.count*3);
    post.copy(asset.matrixWorld).invert().multiply(mesh.matrixWorld).multiply(mesh.bindMatrixInverse);
    for(let i=0;i<a.position.count;i++){
      vector.fromBufferAttribute(a.position,i).applyMatrix4(mesh.bindMatrix);const x=vector.x,y=vector.y,z=vector.z;let X=0,Y=0,Z=0;
      for(let j=0;j<4;j++){
        const weight=a.skinWeight.getComponent(i,j);if(!weight)continue;const o=a.skinIndex.getComponent(i,j)*16;
        X+=weight*(palette[o]*x+palette[o+4]*y+palette[o+8]*z+palette[o+12]);
        Y+=weight*(palette[o+1]*x+palette[o+5]*y+palette[o+9]*z+palette[o+13]);
        Z+=weight*(palette[o+2]*x+palette[o+6]*y+palette[o+10]*z+palette[o+14]);
      }
      vector.set(X,Y,Z).applyMatrix4(post).toArray(out,i*3);
    }
    return out;
  }
  function pose(fish,motions){
    applySwimPose(asset,joints,rest,fish);
    for(const record of paired){
      const output=record.mesh.geometry.attributes.position.array;
      deformFinGeometry(record.binding,motions[record.family],output);
      for(let i=0;i<record.binding.sides.length;i++){
        const root=record.binding.frames[record.binding.sides[i]].root;
        if(Math.hypot(record.base[i*3]-root.x,record.base[i*3+1]-root.y,record.base[i*3+2]-root.z)>1e-9)continue;
        for(let axis=0;axis<3;axis++)assert.ok(Math.abs(output[i*3+axis]-record.base[i*3+axis])<1e-8,'the fin tuck preserves its actual material attachment');
      }
    }
    return {body:positions(skin),fins:paired.map(record=>({...record,positions:positions(record.mesh)}))};
  }
  return {skin,paired,pose};
}

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

{
  const cube=Float64Array.from([0,0,0,1,0,0,0,1,0,0,0,1]),faces=Uint32Array.from([0,2,1,0,1,3,0,3,2,1,2,3]);
  const sheet=Float64Array.from([.1,.1,.1,.12,.1,.1,.1,.12,.1]),indices=Uint32Array.from([0,1,2]);
  const patch=createDistalPatch(indices,new Float64Array(6)),body=createBodyOracle(faces,cube);
  assert.equal(body.crossing(sheet,indices,patch).kind,'clear','an embedded sheet has no surface-edge crossing');
  assert.equal(body.occupancy(sheet,indices,patch)[0].classification,'inside','the separate occupancy oracle catches a wholly embedded sheet');
  assert.ok(trianglesIntersect(Float64Array.from([0,0,0,1,0,0,0,1,0]),Float64Array.from([.1,.1,0,.2,.1,0,.1,.2,0])),'coplanar contained triangles count as contact');
  assert.ok(!trianglesIntersect(Float64Array.from([0,0,0,1,0,0,0,1,0]),new Float64Array(9)),'a degenerate face cannot fabricate contact');
}

async function geometryCases(onPose,makeFinController=createFinController,cases=[-1,1].map(sign=>({urgent:true,sign}))){
  const model=await geometry(),report=[];
  for(const {urgent,sign} of cases){
    const turn=createTurnController(),tasks=createFinTaskController(),controls=Object.fromEntries(['pectoral','pelvic'].map(family=>
      [family,Object.fromEntries([-1,1].map(side=>[side,makeFinController(side,family==='pectoral')]))]));
    const row={urgent,sign,observations:0,minimumFactor:1,maximumBend:0};
    for(let tick=0;tick<=240;tick++){
      const age=tick/120;
      // First load real finite fin histories; an initially empty history is
      // insufficient evidence that a pre-tuck clears a travelling response.
      if(tick===48)turn.request(sign*Math.PI,age,{urgent});
      const gesture=turn.sample(age),command=tasks.sample({age,turn:tick<72?sign*.6:0,
        yawError:sign*Math.PI,yawRate:sign*.4,stopping:0,mode:'swim'});
      const fish={...physical,...command,age,phase:age*4.5,finPhase:age*4.2,yawRate:sign*.4,rollFin:sign*.025,
        turnBend:gesture.b[0],turnBend1:gesture.b[0],turnBend2:gesture.b[1],turnBend3:gesture.b[2],
        turnAnterior:gesture.b[0],turnTrunk:gesture.b[1],turnPosterior:gesture.b[2],
        turnFinClearance:gesture.finClearance,
        turnRecruitment:gesture.activity[0],turnRecruitment1:gesture.activity[0],turnRecruitment2:gesture.activity[1],turnRecruitment3:gesture.activity[2]};
      const motions=Object.fromEntries(['pectoral','pelvic'].map(family=>[family,
        Object.fromEntries([-1,1].map(side=>[side,controls[family][side].sample(fish)]))]));
      const posed=model.pose(fish,motions);
      await onPose?.({model,posed,fish,motions,row,tick});
      const history=Math.max(...Object.values(motions).flatMap(sides=>Object.values(sides).flatMap(motion=>Array.from(motion.samples,Math.abs))));
      if(age<.4)row.maximumWarmHistory=Math.max(row.maximumWarmHistory??0,history);
      if(row.historyClearAt===undefined&&gesture.finClearance===0&&history<1e-10)row.historyClearAt=age;
      if(row.bodyLoadAt===undefined&&Math.max(...gesture.b.map(Math.abs))>1e-5)row.bodyLoadAt=age;
      row.minimumFactor=Math.min(row.minimumFactor,finClearanceFactor(fish));
      row.maximumBend=Math.max(row.maximumBend,...gesture.b.map(Math.abs));row.observations++;
    }
    assert.equal(row.observations,241,'every actual managed 120 Hz turn/recovery sample is inspected');
    assert.ok(row.maximumWarmHistory>.1,'the urgent turn begins with an actual loaded distal response');
    assert.ok(row.historyClearAt<=row.bodyLoadAt,'warm fin history clears before the first issued urgent body load');
    assert.ok(Math.abs(row.maximumBend-(urgent?2.4:1.8))<1e-12,'the cases reach their actual issued normal and urgent C loads');
    report.push(row);
  }
  return report;
}

let oracle,patches;
function inspect({model,posed,fish,motions,row,tick}){
  if(!oracle){
    oracle=createBodyOracle(model.skin.geometry.index.array,posed.body);
    patches=posed.fins.map(record=>{
      // Tube UVs run around each ray. Use the same actual material-span
      // coordinates as the shared membrane/ray deformation to exclude only
      // the intentional proximal attachment band from crossing checks.
      const uv=new Float64Array(record.binding.sides.length*2);
      for(let i=0;i<record.binding.sides.length;i++)uv[i*2+1]=1-Math.sqrt(record.binding.values[i*7+3]);
      return createDistalPatch(record.mesh.geometry.index.array,uv);
    });
    assert.equal(patches.length,6,'all four actual paired membranes and both embedded ray meshes are checked');
    assert.ok(patches.every(patch=>patch.triangles.length>100),'every paired mesh retains a substantial distal surface');
  }else oracle.update(posed.body);
  row.maximumOccupancy??=0;
  for(let i=0;i<posed.fins.length;i++){
    const record=posed.fins[i],index=record.mesh.geometry.index.array,result=oracle.crossing(record.positions,index,patches[i]);
    assert.equal(result.kind,'clear',`${record.mesh.name}: actual distal fin crosses the body at ${fish.age}s (${JSON.stringify(result)})`);
    // The skin has authored facial apertures. Generalized winding is an
    // occupancy diagnostic, not a watertight solid certificate. Check every
    // connected distal sheet component in addition to all sheet/tube edges.
    for(const component of record.mesh.name.includes('membrane')?oracle.occupancy(record.positions,index,patches[i]):[]){
      row.maximumOccupancy=Math.max(row.maximumOccupancy,Math.abs(component.winding));
      assert.equal(component.classification,'outside',`${record.mesh.name}: distal connected patch is ${component.classification} at ${fish.age}s`);
    }
  }
}
async function actualRoutes(makeSwimmer=createSwimmer,makeFinController=createFinController,reverse=false){
  const model=await geometry(),report=[];
  for(const sign of [-1,1]){
    const swimmer=makeSwimmer(()=>.5),fish=swimmer.fish,controls=Object.fromEntries(['pectoral','pelvic'].map(family=>
      [family,Object.fromEntries([-1,1].map(side=>[side,makeFinController(side,family==='pectoral')]))]));
    Object.assign(fish,{x:0,y:.84,z:0,yaw:0,pitch:0,bank:0,vx:0,vy:0,vz:0});
    swimmer.flick({x:-.3,y:fish.y,z:-sign*.0001});
    const row={urgent:true,sign,reverse,observations:0,minimumFactor:1,maximumBend:0};
    for(let tick=0;tick<=(reverse?300:192);tick++){
      if(reverse&&tick===82){
        const target=fish.yaw-sign*2.6;
        swimmer.flick({x:fish.x+.3*Math.cos(target),y:fish.y,z:fish.z-.3*Math.sin(target)});
      }
      if(tick)swimmer.step(1/120);
      const motions=Object.fromEntries(['pectoral','pelvic'].map(family=>[family,
        Object.fromEntries([-1,1].map(side=>[side,controls[family][side].sample(fish)]))]));
      inspect({model,posed:model.pose(fish,motions),fish,motions,row,tick});
      const history=Math.max(...Object.values(motions).flatMap(sides=>Object.values(sides).flatMap(motion=>Array.from(motion.samples,Math.abs))));
      const load=Math.max(...[1,2,3].map(i=>Math.abs(fish['turnBend'+i])));
      if(row.historyClearAt===undefined&&fish.turnFinClearance===0&&history<1e-10)row.historyClearAt=fish.age;
      if(row.bodyLoadAt===undefined&&load>1e-5)row.bodyLoadAt=fish.age;
      row.minimumFactor=Math.min(row.minimumFactor,finClearanceFactor(fish));
      row.maximumBend=Math.max(row.maximumBend,...[1,2,3].map(i=>Math.abs(fish['turnBend'+i])));row.observations++;
    }
    assert.ok(row.maximumBend>2.0,'the actual close flick reaches a meaningful tight C before navigation may interrupt it');
    assert.ok(row.historyClearAt<=row.bodyLoadAt,'the actual observed distal response clears before urgent body loading begins');
    report.push(row);
  }
  return report;
}
const report={issued:await geometryCases(inspect),routes:await actualRoutes(),reversals:await actualRoutes(createSwimmer,createFinController,true)};
const data=source=>'data:text/javascript;base64,'+Buffer.from(source).toString('base64');
const absoluteImports=(source,parent)=>source.replace(/from '(\.\/[^']+)'/g,(_,relative)=>`from '${new URL(relative,parent)}'`);
async function alteredClearance(transform){
  const helperURL=new URL('../static/aquarium-fin-clearance.js',import.meta.url),motionURL=new URL('../static/aquarium-motion.js',import.meta.url),finURL=new URL('../static/aquarium-fins.js',import.meta.url);
  const source=await readFile(helperURL,'utf8'),changed=transform(source);
  assert.notEqual(changed,source,'the mutation must alter the current source');
  const helper=data(changed);
  const motionSource=(await readFile(motionURL,'utf8')).replace(/'\.\/aquarium-fin-clearance\.js(?:\?v=\d+)?'/,`'${helper}'`);
  assert.ok(motionSource.includes(helper),'the mutant reaches the actual command path');
  const motion=data(absoluteImports(motionSource,motionURL));
  const finSource=(await readFile(finURL,'utf8')).replace(/'\.\/aquarium-motion\.js(?:\?v=\d+)?'/,`'${motion}'`);
  return {motion:await import(motion),fins:await import(data(absoluteImports(finSource,finURL)))};
}
{
  const mutant=await alteredClearance(source=>source.replace('return -.30*(1-weight)*(pectoral?1:.35);','return 0;'));
  await assert.rejects(actualRoutes(mutant.motion.createSwimmer,mutant.fins.createFinController),
    error=>error.code==='ERR_ASSERTION'&&error.message.includes('actual distal fin crosses the body'),
    'removing the actual cranial fold must reproduce the rendered ray/body crossing');
}
{
  const mutant=await alteredClearance(source=>source.replace(/const planned=Number\.isFinite\(fish\.turnFinClearance\)[^;]+;/,'const planned=1;'));
  await assert.rejects(geometryCases(inspect,mutant.fins.createFinController),
    error=>error.code==='ERR_ASSERTION'&&(/crosses the body|history clears before/.test(error.message)),
    'ignoring pre-tuck must fail the warm real-history clearance regression');
}
report.mutations={missingCranialFoldRejected:true,ignoredPlannedTuckRejected:true};
console.log(JSON.stringify({definition:'Actual authored paired-fin clearance coordination, not a biological calibration.',report}));
