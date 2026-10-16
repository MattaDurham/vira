/* Refined-asset attachment joins. These compare actual exported material and
 * bounded actual runtime poses; they do not run a navigation/fluid route. */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Matrix3,Matrix4,Quaternion,Texture,Triangle,Vector3} from '../static/vendor/three.webgpu.js';
import {createKoiInstance} from '../static/aquarium-fish-instance.js';
import {neutralMechanicsFish} from '../static/aquarium-mechanics-study.js';
import {createSourceSteeringController} from '../static/aquarium-source-steering.js';
import {createFinFrame,bindFinGeometry} from '../static/aquarium-fins.js';

const defaults={assetPath:new URL('../static/aquarium/refined/koi.glb',import.meta.url),massPath:new URL('../static/aquarium/refined/body-mass.json',import.meta.url),baselinePath:new URL('../static/aquarium/koi.glb',import.meta.url),baselineMassPath:new URL('../static/aquarium/body-mass.json',import.meta.url)};
const file=value=>value instanceof URL?value:pathToFileURL(resolve(value));
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const near=(a,b,tolerance,message)=>assert.ok(Number.isFinite(a)&&Math.abs(a-b)<=tolerance,`${message}: ${a} / ${b}`);
const paired=name=>/^(Pectoral|Pelvic)_/.test(name),family=name=>name.startsWith('Pectoral')?'pectoral':'pelvic';
const intended=kind=>kind==='pectoral'?'spine_03':'spine_18';
async function parse(bytes){return (await new GLTFLoader().register(()=>({name:'RefinedAttachmentGeometry',loadTexture:()=>Promise.resolve(new Texture())})).parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;}
function meshes(asset){const out=new Map();asset.traverse(mesh=>{if(mesh.isMesh)out.set(mesh.name,mesh);});return out;}
function bones(asset){asset.updateMatrixWorld(true);const inv=asset.matrixWorld.clone().invert(),q=asset.getWorldQuaternion(new Quaternion()).invert(),out=new Map();asset.traverse(bone=>{if(bone.isBone)out.set(bone.name,{p:bone.getWorldPosition(new Vector3()).applyMatrix4(inv),q:bone.getWorldQuaternion(new Quaternion()).premultiply(q),world:bone.matrixWorld.clone()});});return out;}
function skinPositions(mesh){
 mesh.updateWorldMatrix(true,false);mesh.skeleton.update();const a=mesh.geometry.attributes,p=mesh.skeleton.boneMatrices,b=mesh.bindMatrix.elements,inv=mesh.bindMatrixInverse.elements,w=mesh.matrixWorld.elements,out=new Float64Array(a.position.count*3);
 for(let i=0;i<a.position.count;i++){
  const px=a.position.getX(i),py=a.position.getY(i),pz=a.position.getZ(i),bx=b[0]*px+b[4]*py+b[8]*pz+b[12],by=b[1]*px+b[5]*py+b[9]*pz+b[13],bz=b[2]*px+b[6]*py+b[10]*pz+b[14];let x=0,y=0,z=0,h=0;
  for(let s=0;s<4;s++){const weight=a.skinWeight.getComponent(i,s);if(!weight)continue;const j=a.skinIndex.getComponent(i,s)*16;h+=weight;x+=weight*(p[j]*bx+p[j+4]*by+p[j+8]*bz+p[j+12]);y+=weight*(p[j+1]*bx+p[j+5]*by+p[j+9]*bz+p[j+13]);z+=weight*(p[j+2]*bx+p[j+6]*by+p[j+10]*bz+p[j+14]);}
  const lx=inv[0]*x+inv[4]*y+inv[8]*z+inv[12]*h,ly=inv[1]*x+inv[5]*y+inv[9]*z+inv[13]*h,lz=inv[2]*x+inv[6]*y+inv[10]*z+inv[14]*h;
  out[i*3]=w[0]*lx+w[4]*ly+w[8]*lz+w[12];out[i*3+1]=w[1]*lx+w[5]*ly+w[9]*lz+w[13];out[i*3+2]=w[2]*lx+w[6]*ly+w[10]*lz+w[14];
 }return out;
}
function records(asset){
 asset.updateMatrixWorld(true);const result=new Map();
 for(const [name,mesh] of meshes(asset)){if(!paired(name))continue;assert.ok(mesh.isSkinnedMesh,`${name}: actual skinned paired material`);const a=mesh.geometry.attributes;
  result.set(name,{name,kind:family(name),membrane:name.includes('membrane'),positions:Float32Array.from(a.position.array),uv:Float32Array.from(a.uv.array),indices:mesh.geometry.index.array.slice(),weights:Float32Array.from(a.skinWeight.array),joints:Array.from(a.skinIndex.array),boneNames:mesh.skeleton.bones.map(b=>b.name),restPositions:skinPositions(mesh),restWorld:mesh.matrixWorld.clone()});
 }assert.equal(result.size,6,'four paired membranes and both complete ray meshes participate');return result;
}
function assertRigid(record){
 const target=intended(record.kind);
 for(let i=0;i<record.positions.length/3;i++){
  let total=0,influences=0;
  for(let s=0;s<4;s++){const k=i*4+s,weight=record.weights[k];total+=weight;if(!weight)continue;influences++;near(weight,1,1e-7,`${record.name}: rigid basal weight`);assert.equal(record.boneNames[record.joints[k]],target,`${record.name}: intended basal bone`);}
  assert.equal(influences,1,`${record.name}: one rigid basal influence`);near(total,1,1e-7,`${record.name}: normalized rigid basal weights`);
 }
}
function assertPairedTranslation(oldRecords,newRecords,report={}){
 const shifts={pectoral:{},pelvic:{}};report.maximumTranslationError=0;report.vertices=0;
 for(const [name,now] of newRecords){const old=oldRecords.get(name);assert.ok(old,`${name}: original paired counterpart`);assert.equal(now.positions.length,old.positions.length,`${name}: whole material correspondence`);for(let k=0;k<16;k++)near(now.restWorld.elements[k],old.restWorld.elements[k],2e-7,`${name}: common exported primitive frame`);assert.deepEqual(now.indices,old.indices,`${name}: unchanged topology`);assert.deepEqual(now.uv,old.uv,`${name}: unchanged radial/tube coordinates`);assert.deepEqual(now.weights,old.weights,`${name}: unchanged rigid weights`);assertRigid(now);
  if(!now.membrane)continue;
  const side=Math.sign(old.positions[2]),delta=[0,1,2].map(k=>now.positions[k]-old.positions[k]);assert.ok(side===1||side===-1);near(delta[0],0,2e-7,`${name}: longitudinal pivot retained`);near(delta[1],0,2e-7,`${name}: vertical pivot retained`);assert.ok(Math.abs(delta[2])>.0005,`${name}: real nonzero surface refit`);shifts[now.kind][side]=delta;
 }
 for(const [name,now] of newRecords){const old=oldRecords.get(name);
  for(let i=0;i<now.positions.length/3;i++){const side=Math.sign(old.positions[i*3+2]),delta=shifts[now.kind][side];assert.ok(delta,`${name}: each ray belongs to an actual membrane side`);assert.equal(Math.sign(now.positions[i*3+2]),side,`${name}: no material side crossing`);
   for(let k=0;k<3;k++){const error=Math.abs(now.positions[i*3+k]-old.positions[i*3+k]-delta[k]);report.maximumTranslationError=Math.max(report.maximumTranslationError,error);assert.ok(error<2e-6,`${name}: complete translated fin, including distal material`);}report.vertices++;
  }
 }
 for(const kind of ['pectoral','pelvic']){
  near(shifts[kind][1][2],-shifts[kind][-1][2],2e-7,`${kind}: mirrored refit`);
  const frames={};
  for(const now of newRecords.values())if(now.kind===kind&&now.membrane){const old=oldRecords.get(now.name),a=createFinFrame(old.positions,old.uv),b=createFinFrame(now.positions,now.uv);frames[b.side]=b;
   for(const key of ['span','chord','normal'])for(let k=0;k<3;k++)near(b[key][k],a[key][k],2e-6,`${now.name}: translated ${key}`);
   for(const key of ['length','angleMin','angleMax'])near(b[key],a[key],2e-6,`${now.name}: translated material ${key}`);
  }
  for(const now of newRecords.values())if(now.kind===kind){const bound=bindFinGeometry(now.positions,frames);assert.ok(bound.values.every(Number.isFinite),'new membrane/ray bindings remain finite');}
 }
 report.shifts=shifts;return report;
}
function cloneRecords(input){return new Map([...input].map(([name,r])=>[name,{...r,positions:r.positions.slice(),uv:r.uv.slice(),weights:r.weights.slice(),joints:r.joints.slice()}]));}
function mutations(oldRecords,newRecords){
 const membrane=[...newRecords.values()].find(r=>r.membrane),rays=[...newRecords.values()].find(r=>!r.membrane),flags={};
 function reject(name,change,pattern){const bad=cloneRecords(newRecords);change(bad);assert.throws(()=>assertPairedTranslation(oldRecords,bad),pattern,`${name} must fail the actual exported-material guard`);flags[name]=true;}
 reject('rootOnlyTranslation',bad=>{const r=bad.get(membrane.name),old=oldRecords.get(r.name);for(let i=0;i<r.positions.length/3;i++)if(r.uv[i*2+1]<.999)r.positions.set(old.positions.slice(i*3,i*3+3),i*3);},/complete translated fin/);
 reject('staleDistalRay',bad=>{const r=bad.get(rays.name),old=oldRecords.get(r.name),i=r.positions.length-3;r.positions.set(old.positions.slice(i,i+3),i);},/complete translated fin/);
 reject('neighborBoneRay',bad=>{const r=bad.get(rays.name),i=r.positions.length/3-1;r.joints[i*4]=r.boneNames.indexOf('spine_19');},/intended basal bone/);
 reject('extraSkinInfluence',bad=>{const r=bad.get(rays.name);r.weights[1]=.1;r.joints[1]=r.boneNames.indexOf('spine_19');},/unchanged rigid weights|rigid basal weight|one rigid basal/);
 reject('wrongRadialUV',bad=>{bad.get(membrane.name).uv[1]=0;},/unchanged radial/);
 return flags;
}
function irisRecords(asset){
 const inverse=asset.matrixWorld.clone().invert(),result=new Map(),point=new Vector3();
 for(const [name,mesh] of meshes(asset)){if(!name.startsWith('Iris_radial_stroma'))continue;const a=mesh.geometry.attributes,joint=mesh.skeleton.bones[a.skinIndex.getX(0)].name,positions=skinPositions(mesh);
  for(let i=0;i<positions.length/3;i++)point.fromArray(positions,i*3).applyMatrix4(inverse).toArray(positions,i*3);
  assert.ok(!result.has(joint),'one continuous iris belongs to each eye');result.set(joint,{positions,uv:Float32Array.from(a.uv.array)});
 }assert.equal(result.size,2,'both actual iris geometries register the authored eye pivots');return result;
}
function eyeAssemblies(asset){
 const inverse=asset.matrixWorld.clone().invert(),result=new Map(),point=new Vector3();
 for(const [name,mesh] of meshes(asset)){if(!['Iris_radial_stroma','Recessed_pupil','Cornea'].some(prefix=>name.startsWith(prefix)))continue;const a=mesh.geometry.attributes,joint=mesh.skeleton.bones[a.skinIndex.getX(0)].name;assert.ok(joint==='eye_L'||joint==='eye_R',`${name}: intended eye assembly bone`);const positions=skinPositions(mesh);
  for(let i=0;i<positions.length/3;i++){let count=0;for(let slot=0;slot<4;slot++){const weight=a.skinWeight.getComponent(i,slot);if(!weight)continue;count++;near(weight,1,1e-7,`${name}: rigid eye assembly weight`);assert.equal(mesh.skeleton.bones[a.skinIndex.getComponent(i,slot)].name,joint);}assert.equal(count,1,`${name}: one intended eye influence`);point.fromArray(positions,i*3).applyMatrix4(inverse).toArray(positions,i*3);}
  result.set(name,{joint,positions});
 }assert.equal(result.size,6,'both complete iris, pupil and cornea assemblies participate');return result;
}
function assertEyeAssemblies(oldAssembly,newAssembly,shifts){
 let maximum=0,vertices=0;
 for(const [name,old] of oldAssembly){const now=newAssembly.get(name);assert.ok(now,`${name}: refined eye counterpart`);assert.equal(now.joint,old.joint);assert.equal(now.positions.length,old.positions.length);const delta=shifts[old.joint];assert.ok(delta);
  for(let i=0;i<old.positions.length/3;i++){const error=Math.hypot(...[0,1,2].map(k=>now.positions[i*3+k]-old.positions[i*3+k]-delta[k]));maximum=Math.max(maximum,error);assert.ok(error<2e-6,`${name}: complete eye assembly follows the iris-derived translation`);vertices++;}
 }return {maximumEyeAssemblyTranslationError:maximum,eyeAssemblyVertices:vertices};
}
function assertEyeRegistration(oldIris,newIris,oldBones,newBones){
 const report={maximumEyeRefitError:0,minimumEyeShift:Infinity,maximumEyeShapeError:0,eyeShifts:{}};
 for(const [joint,old] of oldIris){const now=newIris.get(joint);assert.ok(now,`${joint}: refined iris counterpart`);assert.equal(now.positions.length,old.positions.length,'whole iris material correspondence');assert.deepEqual(now.uv,old.uv,'eye-local radial coordinates retain their meaning');const delta=new Vector3().fromArray(now.positions).sub(new Vector3().fromArray(old.positions));near(delta.x,0,2e-7,`${joint}: eye longitudinal center retained`);near(delta.y,0,2e-7,`${joint}: eye vertical center retained`);assert.ok(Math.abs(delta.z)>.001,`${joint}: widened cranial fixture exercises a real eye pivot refit`);report.minimumEyeShift=Math.min(report.minimumEyeShift,Math.abs(delta.z));report.eyeShifts[joint]=delta.toArray();
  for(let i=0;i<old.positions.length/3;i++){const error=Math.hypot(now.positions[i*3]-old.positions[i*3]-delta.x,now.positions[i*3+1]-old.positions[i*3+1]-delta.y,now.positions[i*3+2]-old.positions[i*3+2]-delta.z);report.maximumEyeShapeError=Math.max(report.maximumEyeShapeError,error);assert.ok(error<2e-6,'complete iris geometry moves with its new orbital base');}
  const a=oldBones.get(joint),b=newBones.get(joint),error=a.p.clone().add(delta).distanceTo(b.p);report.maximumEyeRefitError=Math.max(report.maximumEyeRefitError,error);assert.ok(error<2e-6,`${joint}: eye pivot follows actual eye-base translation`);assert.ok(1-Math.abs(a.q.dot(b.q))<2e-7,`${joint}: translating both eye-axis endpoints retains its direction`);
 }return report;
}
function widthStations(asset){
 const mesh=asset.getObjectByName('Koi_skin'),p=mesh.geometry.attributes.position,map=new Map();
 for(let i=0;i<p.count;i++){const x=Math.round(p.getX(i)*1e7)/1e7;map.set(x,Math.max(map.get(x)??0,Math.abs(p.getZ(i))));}
 return [...map].sort(([a],[b])=>a-b);
}
function stationRatio(oldStations,newStations,x){
 const old=new Map(oldStations),rows=newStations.filter(([px])=>old.has(px)).map(([px,w])=>[px,w/old.get(px)]);let i=0;while(i+1<rows.length&&rows[i+1][0]<x)i++;const a=rows[i],b=rows[Math.min(i+1,rows.length-1)],t=b[0]===a[0]?0:(x-a[0])/(b[0]-a[0]);return a[1]+t*(b[1]-a[1]);
}
function assertOpercularRegistration(oldWidth,newWidth,oldBones,newBones){
 const axis=new Vector3(0,1,0),report={maximumFacialPivotError:0,maximumOpercularAxisError:0};
 // Cross-section ratios come from the actual exported body, not profile_width.
 // Linear station interpolation and head relief set the 0.2 mm / 3 mrad floors.
 for(const name of ['operculum_L','operculum_R']){
  const a=oldBones.get(name),b=newBones.get(name),sign=name.endsWith('L')?-1:1;
  const head=new Vector3(-.50,.028,sign*.067*stationRatio(oldWidth,newWidth,-.50)),tail=new Vector3(-.375,.010,sign*.112*stationRatio(oldWidth,newWidth,-.375));
  const pe=b.p.distanceTo(head),angle=axis.clone().applyQuaternion(b.q).angleTo(tail.sub(head).normalize());report.maximumFacialPivotError=Math.max(report.maximumFacialPivotError,pe);report.maximumOpercularAxisError=Math.max(report.maximumOpercularAxisError,angle);assert.ok(pe<.0002,`${name}: head refits the actual new width ratio`);assert.ok(angle<.003,`${name}: axis follows the refitted head and tail`);assert.ok(b.p.distanceTo(a.p)>.00005,`${name}: fixture exercises a real pivot refit`);
 }
 return report;
}
function barbelRecords(asset){
 const inverse=asset.matrixWorld.clone().invert(),result=new Map(),point=new Vector3();
 for(const [name,mesh] of meshes(asset)){if(!name.includes('sensory_barbel'))continue;const a=mesh.geometry.attributes,positions=skinPositions(mesh);
  for(let i=0;i<positions.length/3;i++){let total=0;for(let slot=0;slot<4;slot++){const weight=a.skinWeight.getComponent(i,slot);assert.ok(Number.isFinite(weight)&&weight>=0);total+=weight;if(weight)assert.ok(['spine_00','jaw_upper'].includes(mesh.skeleton.bones[a.skinIndex.getComponent(i,slot)].name),`${name}: documented original cranial/jaw attachment`);}near(total,1,1e-7,`${name}: normalized original barbel weights`);point.fromArray(positions,i*3).applyMatrix4(inverse).toArray(positions,i*3);}
  // The pinned original rostral barbels blend skull and upper-jaw weights;
  // maxillary barbels are rigid on the upper jaw. Registration preserves both.
  result.set(name,{positions,weights:Float32Array.from(a.skinWeight.array),bindingNames:Array.from(a.skinIndex.array,id=>mesh.skeleton.bones[id].name)});
 }assert.equal(result.size,4,'all four actual sensory barbels participate');return result;
}
function assertBarbelTranslation(oldRecords,newRecords){
 const report={maximumBarbelTranslationError:0,barbelVertices:0,barbelShifts:{}};
 for(const [name,old] of oldRecords){const now=newRecords.get(name);assert.ok(now,`${name}: refined sensory counterpart`);assert.equal(now.positions.length,old.positions.length);assert.deepEqual(now.weights,old.weights,`${name}: original per-vertex barbel weights retained`);for(let i=0;i<old.weights.length;i++)if(old.weights[i])assert.equal(now.bindingNames[i],old.bindingNames[i],`${name}: original named barbel influence retained`);const delta=[0,1,2].map(k=>now.positions[k]-old.positions[k]);near(delta[0],0,2e-7,`${name}: barbel X retained`);near(delta[1],0,2e-7,`${name}: barbel vertical retained`);assert.ok(Math.abs(delta[2])>1e-6,`${name}: nonzero barbel registration is exercised`);report.barbelShifts[name]=delta;
  for(let i=0;i<old.positions.length/3;i++){const error=Math.hypot(...[0,1,2].map(k=>now.positions[i*3+k]-old.positions[i*3+k]-delta[k]));report.maximumBarbelTranslationError=Math.max(report.maximumBarbelTranslationError,error);assert.ok(error<2e-6,`${name}: complete barbel receives one lateral translation`);report.barbelVertices++;}
 }return report;
}
function assertBarbelJoin(oldAsset,newAsset){
 const old=barbelRecords(oldAsset),now=barbelRecords(newAsset),report=assertBarbelTranslation(old,now),bad=new Map([...now].map(([name,r])=>[name,{...r,positions:r.positions.slice()}])),name=[...bad.keys()][0];bad.get(name).positions.set(old.get(name).positions);assert.throws(()=>assertBarbelTranslation(old,bad),/nonzero barbel registration/,'a stale unregistered barbel must fail');
 const rootOnly=new Map([...now].map(([name,r])=>[name,{...r,positions:r.positions.slice()}]));for(let i=16*3;i<rootOnly.get(name).positions.length;i++)rootOnly.get(name).positions[i]=old.get(name).positions[i];assert.throws(()=>assertBarbelTranslation(old,rootOnly),/complete barbel receives/,'moving only the barbel root must fail');report.staleBarbelAndRootOnlyRejected=true;return report;
}
function assertBoneJoin(oldAsset,newAsset){
 const old=bones(oldAsset),now=bones(newAsset),report={maximumAxialHeadError:0,maximumAxialAxisError:0,maximumFacialPivotError:0,maximumOpercularAxisError:0};assert.deepEqual([...now.keys()].sort(),[...old.keys()].sort(),'the refined export retains every authored joint');
 for(const [name,a] of old){const b=now.get(name);if(!name.startsWith('spine_')&&!['jaw_lower','jaw_upper'].includes(name))continue;
  const pe=a.p.distanceTo(b.p),qe=1-Math.abs(a.q.dot(b.q));report.maximumAxialHeadError=Math.max(report.maximumAxialHeadError,pe);report.maximumAxialAxisError=Math.max(report.maximumAxialAxisError,qe);assert.ok(pe<2e-6&&qe<2e-7,`${name}: preserved axial/protected facial head and axis`);
 }
 const oldWidth=widthStations(oldAsset),newWidth=widthStations(newAsset);Object.assign(report,assertOpercularRegistration(oldWidth,newWidth,old,now));
 const staleOperculum=new Map([...now].map(([name,r])=>[name,{...r,p:r.p.clone(),q:r.q.clone()}]));staleOperculum.get('operculum_L').p.copy(old.get('operculum_L').p);assert.throws(()=>assertOpercularRegistration(oldWidth,newWidth,old,staleOperculum),/head refits the actual new width ratio/,'stale opercular pivot must fail actual-body registration');staleOperculum.get('operculum_L').p.copy(now.get('operculum_L').p);staleOperculum.get('operculum_L').q.copy(old.get('operculum_L').q);assert.throws(()=>assertOpercularRegistration(oldWidth,newWidth,old,staleOperculum),/axis follows the refitted head and tail/,'stale opercular axis must fail actual-body registration');report.staleOpercularPivotAndAxisRejected=true;
 const oldIris=irisRecords(oldAsset),newIris=irisRecords(newAsset);Object.assign(report,assertEyeRegistration(oldIris,newIris,old,now));
 const oldAssembly=eyeAssemblies(oldAsset),newAssembly=eyeAssemblies(newAsset);Object.assign(report,assertEyeAssemblies(oldAssembly,newAssembly,report.eyeShifts));
 for(const prefix of ['Recessed_pupil','Cornea']){const bad=new Map([...newAssembly].map(([name,r])=>[name,{...r,positions:r.positions.slice()}])),name=[...bad.keys()].find(n=>n.startsWith(prefix));bad.get(name).positions.set(oldAssembly.get(name).positions);assert.throws(()=>assertEyeAssemblies(oldAssembly,bad,report.eyeShifts),/complete eye assembly/,'a stale pupil or cornea must fail the independently iris-derived registration guard');}report.stalePupilAndCorneaRejected=true;
 const stale=new Map([...now].map(([name,r])=>[name,{...r,p:r.p.clone()}]));for(const joint of oldIris.keys())stale.get(joint).p.copy(old.get(joint).p);assert.throws(()=>assertEyeRegistration(oldIris,newIris,old,stale),/eye pivot follows actual eye-base translation/,'stale original eye pivots must fail the independent geometry registration guard');report.staleEyePivotRejected=true;
 return report;
}
// Bounded authored fin inputs exercise the shared runtime material path;
// neither their timing nor the carrier amplitude is a biological calibration.
function poseFish(g){
 const fish={...neutralMechanicsFish(),x:.12,y:.84,z:-.205,poseHistory:true,age:g.physicalAge,yaw:g.heading,pitch:.13,bank:-.07,yawRate:g.headRate,yawAcceleration:g.headAcceleration,
  drive:.2,stroke:1,muscle1:1,muscle2:1,muscle3:1,speed:.2,forwardSpeed:.2,phase:g.physicalAge*2*Math.PI*1.18,finPhase:g.physicalAge*2*Math.PI*.65,
  turnAnterior:g.b[0],turnTrunk:g.b[1],turnPosterior:g.b[2],turnAnteriorVelocity:g.v[0],turnTrunkVelocity:g.v[1],turnPosteriorVelocity:g.v[2],turnBend:g.b[0],turnBend1:g.b[0],turnBend2:g.b[1],turnBend3:g.b[2],
  turnRecruitment:g.activity[0],turnRecruitment1:g.activity[0],turnRecruitment2:g.activity[1],turnRecruitment3:g.activity[2],turnSourceWeight:g.sourceReplacementWeight,turnFinClearance:g.finClearance,turnPreparing:g.preparing};
 for(const kind of ['pectoral','pelvic'])for(const [word,side] of [['Plus',1],['Minus',-1]]){fish[kind+'Paddle'+word]=.04*Math.sin(fish.age*7+side*.4)*(kind==='pectoral'?1:.35);fish[kind+'ForeAft'+word]=side*.04;}
 return fish;
}
function controllers(model){return [...Object.values(model.pairedControllers).flatMap(f=>Object.values(f)),model.caudalController,...Object.values(model.medianControllers)];}
function observe(model,fish){for(const controller of controllers(model))controller.sample(fish);}
function rootBodyGap(model,record,root){
 const body=model.asset.getObjectByName('Koi_skin'),positions=skinPositions(body),index=body.geometry.index.array,material=body.geometry.attributes.position,rootX=record.positions[0],a=new Vector3(),b=new Vector3(),c=new Vector3(),closest=new Vector3(),triangle=new Triangle(a,b,c);let gap=Infinity,candidates=0;
 for(let t=0;t<index.length;t+=3){const ids=[index[t],index[t+1],index[t+2]],lo=Math.min(...ids.map(i=>material.getX(i))),hi=Math.max(...ids.map(i=>material.getX(i)));if(lo>rootX+.015||hi<rootX-.015)continue;a.fromArray(positions,ids[0]*3);b.fromArray(positions,ids[1]*3);c.fromArray(positions,ids[2]*3);if(triangle.getArea()<1e-14)continue;triangle.closestPointToPoint(root,closest);gap=Math.min(gap,root.distanceTo(closest));candidates++;}
 assert.ok(candidates>0,'actual body triangles cover the intended longitudinal attachment station');return gap;
}
function assertEyePose(source,model,fish,report){
 const sourceBones=bones(source),sourceInverse=source.matrixWorld.clone().invert(),actualMeshes=meshes(model.asset),up=new Vector3(0,1,0),forward=new Vector3(1,0,0);
 for(const [name,mesh] of meshes(source)){if(!name.startsWith('Iris_radial_stroma'))continue;
  const weights=mesh.geometry.attributes.skinWeight,indices=mesh.geometry.attributes.skinIndex,joint=mesh.skeleton.bones[indices.getX(0)].name;assert.ok(joint==='eye_L'||joint==='eye_R');for(let i=0;i<weights.count;i++){near(weights.getX(i),1,1e-7,'actual iris retains its rigid eye binding');assert.equal(mesh.skeleton.bones[indices.getX(i)].name,joint);}
  const pivot=sourceBones.get(joint).p,phase=joint.endsWith('L')?0:1.7,q=new Quaternion().setFromAxisAngle(up,.018*Math.sin(fish.age*.47+phase)-fish.yawRate*.035).multiply(new Quaternion().setFromAxisAngle(forward,.012*Math.sin(fish.age*.31+phase)-fish.bank*.45)),original=skinPositions(mesh),actual=skinPositions(actualMeshes.get(name)),point=new Vector3(),seen=new Vector3();
  const bone=model.asset.getObjectByName(joint),pivotError=bone.getWorldPosition(new Vector3()).distanceTo(pivot.clone().applyMatrix4(model.asset.matrixWorld));report.maximumEyePivotError=Math.max(report.maximumEyePivotError,pivotError);assert.ok(pivotError<2e-6,'authored eye pivot follows the actual root frame');
  for(const i of [0,Math.floor(weights.count/2),weights.count-1]){point.fromArray(original,i*3).applyMatrix4(sourceInverse).sub(pivot).applyQuaternion(q).add(pivot).applyMatrix4(model.asset.matrixWorld);const error=point.distanceTo(seen.fromArray(actual,i*3));report.maximumEyeVertexError=Math.max(report.maximumEyeVertexError,error);assert.ok(error<3e-6,'actual iris motion rotates around its captured authored pivot exactly once');}
 }
}
async function moving(oldSource,newSource,oldRecords,newRecords,oldMass,newMass,data){
 const neutral=poseFish(createSourceSteeringController(data).sample()),models=[createKoiInstance(oldSource,{massProfile:oldMass,initialPose:neutral,random:()=>.5}),createKoiInstance(newSource,{massProfile:newMass,initialPose:neutral,random:()=>.5})],sources=[oldSource,newSource],allRecords=[oldRecords,newRecords],restBones=[bones(oldSource),bones(newSource)],report={poses:0,vertices:0,maximumRigidSkinError:0,maximumRootBodyGap:0,maximumPosedFanDifference:0,maximumEyePivotError:0,maximumEyeVertexError:0};
 // Both constructors observed age zero. Every subsequent committed physical
 // tick advances the public history controllers, even between geometry checks.
 for(const model of models)for(const controller of controllers(model)){const sample=controller.sample.bind(controller);controller.sample=fish=>sample(fish,'refined-attachment');}
 const controller=createSourceSteeringController(data);controller.request(Math.PI,0,{headYaw:0,headRate:0,headAcceleration:0});const selected=new Set([0,14,64,136,264,312]),point=new Vector3(),delta=new Vector3(),expected=new Vector3(),actualPoint=new Vector3(),root=new Vector3();let packet=controller.sample();
 try{
  for(let tick=0;tick<=312;tick++){
   if(tick)packet=controller.commit(controller.propose(1/120));const fish=poseFish(packet);for(const model of models)observe(model,fish);if(!selected.has(tick))continue;const material=[];
   for(let which=0;which<2;which++){
    const model=models[which],records=allRecords[which],boneRest=restBones[which];model.pose(fish);model.updateVisual(fish);model.asset.updateMatrixWorld(true);const posedMeshes=meshes(model.asset),points=new Map(),roots={pectoral:{},pelvic:{}};
    for(const record of records.values()){
     const mesh=posedMeshes.get(record.name),positions=skinPositions(mesh),joint=model.asset.getObjectByName(intended(record.kind)),matrix=new Matrix4().multiplyMatrices(joint.matrixWorld,boneRest.get(intended(record.kind)).world.clone().invert()),linear=new Matrix3().setFromMatrix4(record.restWorld),current=mesh.geometry.attributes.position;points.set(record.name,positions);
     for(let i=0;i<current.count;i++){
      delta.set(current.getX(i)-record.positions[i*3],current.getY(i)-record.positions[i*3+1],current.getZ(i)-record.positions[i*3+2]).applyMatrix3(linear);expected.fromArray(record.restPositions,i*3).add(delta).applyMatrix4(matrix);const error=expected.distanceTo(actualPoint.fromArray(positions,i*3));report.maximumRigidSkinError=Math.max(report.maximumRigidSkinError,error);assert.ok(error<3e-6,`${record.name}: current uploaded material follows its intended basal bone`);report.vertices++;
     }
     if(record.membrane){const side=Math.sign(record.positions[2]);roots[record.kind][side]=root.fromArray(positions,0).clone();const gap=rootBodyGap(model,record,roots[record.kind][side]);report.maximumRootBodyGap=Math.max(report.maximumRootBodyGap,gap);assert.ok(gap<.007,`${record.name}: actual posed root remains on the new local body surface (${gap})`);}
    }
    assertEyePose(sources[which],model,fish,report);material.push({points,roots,model});
   }
   for(const record of newRecords.values()){
    const old=oldRecords.get(record.name),a=material[0],b=material[1],ja=a.model.asset.getObjectByName(intended(record.kind)).matrixWorld.clone().invert(),jb=b.model.asset.getObjectByName(intended(record.kind)).matrixWorld.clone().invert();
    for(let i=0;i<record.positions.length/3;i++){
     const side=Math.sign(old.positions[i*3+2]),ra=a.roots[record.kind][side].clone().applyMatrix4(ja),rb=b.roots[record.kind][side].clone().applyMatrix4(jb);point.fromArray(a.points.get(record.name),i*3).applyMatrix4(ja).sub(ra);delta.fromArray(b.points.get(record.name),i*3).applyMatrix4(jb).sub(rb);const error=point.distanceTo(delta);report.maximumPosedFanDifference=Math.max(report.maximumPosedFanDifference,error);assert.ok(error<5e-6,`${record.name}: complete posed fan retains its root-relative material shape`);
    }
   }
   report.poses++;
  }
  assert.equal(report.poses,selected.size);assert.ok(controller.ready(),'the bounded selected poses include preparation, loading and full quiet recovery');return report;
 }finally{for(const model of models)model.dispose();}
}
export async function assertRefinedAttachments(options={}){
 const paths=Object.fromEntries(Object.entries({...defaults,...options}).map(([key,value])=>[key,file(value)])),[oldBytes,newBytes,originalMassBytes,newMassBytes,dataBytes]=await Promise.all([readFile(paths.baselinePath),readFile(paths.assetPath),readFile(paths.baselineMassPath),readFile(paths.massPath),readFile(new URL('../static/aquarium/reference-turn.json',import.meta.url))]),oldMass=JSON.parse(originalMassBytes.toString('utf8')),newMass=JSON.parse(newMassBytes.toString('utf8'));
 const originalHash=sha(oldBytes);assert.equal(originalHash,oldMass.assetSHA256,'fixed original GLB remains pinned by its original body-mass profile');assert.equal(sha(newBytes),newMass.assetSHA256,'the selected refined GLB and mass profile belong together');assert.notEqual(sha(newBytes),originalHash,'the selected fixture is the independent refined asset');
 const [oldSource,newSource]=await Promise.all([parse(oldBytes),parse(newBytes)]),oldRecords=records(oldSource),newRecords=records(newSource),report={originalSHA256:originalHash,refinedSHA256:sha(newBytes),static:assertPairedTranslation(oldRecords,newRecords),bones:assertBoneJoin(oldSource,newSource),barbels:assertBarbelJoin(oldSource,newSource),mutations:mutations(oldRecords,newRecords)};
 report.moving=await moving(oldSource,newSource,oldRecords,newRecords,oldMass,newMass,JSON.parse(dataBytes.toString('utf8')));assert.equal(sha(await readFile(paths.baselinePath)),originalHash,'focused refined poses never rewrite the original GLB');assert.equal(sha(await readFile(paths.baselineMassPath)),sha(originalMassBytes),'focused refined poses never rewrite the original mass profile');return report;
}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
 const options={};for(const argument of process.argv.slice(2)){const match=/^--(asset|mass|baseline|baseline-mass)=(.+)$/.exec(argument);if(!match)throw new Error('Expected --asset=path --mass=path [--baseline=path --baseline-mass=path]');options[{asset:'assetPath',mass:'massPath',baseline:'baselinePath','baseline-mass':'baselineMassPath'}[match[1]]]=match[2];}
 console.log(JSON.stringify(await assertRefinedAttachments(options)));
}
