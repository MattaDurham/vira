/* Neutral skin-envelope integration for offline mass generation. Apertures are
 * closed by an explicit geometric convention; separate facial meshes are not
 * unioned, and rendered vertices are never treated as equal point masses. */
import assert from 'node:assert/strict';
import {GLTFLoader} from '../../static/vendor/GLTFLoader.js';
import {Texture,Vector3} from '../../static/vendor/three.webgpu.js';

const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const dot=(a,b)=>a.reduce((sum,value,i)=>sum+value*b[i],0),sub=(a,b)=>a.map((value,i)=>value-b[i]);
const vector=(points,i)=>Array.from(points.subarray(i*3,i*3+3));
const area=(a,b,c)=>Math.hypot(...cross(sub(b,a),sub(c,a)))/2;

/** Exact degree-two volume moments of an oriented, closed triangle surface. */
export function integrateVolume(vertices,faces,{origin=[0,0,0],density=1000}={}){
  let volume=0;const first=[0,0,0],second=Array(9).fill(0);
  for(let k=0;k<faces.length;k+=3){
    const [a,b,c]=[0,1,2].map(j=>sub(vector(vertices,faces[k+j]),origin));
    const v=dot(a,cross(b,c))/6;volume+=v;
    const sum=[0,1,2].map(i=>a[i]+b[i]+c[i]);
    for(let i=0;i<3;i++){
      first[i]+=v*sum[i]/4;
      for(let j=0;j<3;j++)second[i*3+j]+=v/20*(sum[i]*sum[j]+a[i]*a[j]+b[i]*b[j]+c[i]*c[j]);
    }
  }
  const localFirst=first.slice();
  for(let i=0;i<3;i++){
    first[i]+=volume*origin[i];
    for(let j=0;j<3;j++)second[i*3+j]+=origin[i]*localFirst[j]+localFirst[i]*origin[j]+volume*origin[i]*origin[j];
  }
  const com=first.map(value=>value/volume),central=second.map((value,k)=>value-volume*com[Math.floor(k/3)]*com[k%3]);
  const trace=central[0]+central[4]+central[8],inertia=central.map((value,k)=>density*((k%4===0?trace:0)-value));
  return {volume,mass:volume*density,com,inertia,firstVolumeMoment:first,secondVolumeMoment:second};
}

function weldedTopology(vertices,faces,tolerance=1e-7){
  const points=[],cells=new Map(),mapping=new Uint32Array(vertices.length/3);
  for(let i=0;i<vertices.length/3;i++){
    const point=vector(vertices,i),cell=point.map(value=>Math.floor(value/tolerance));let chosen;
    search:for(let dx=-1;dx<=1;dx++)for(let dy=-1;dy<=1;dy++)for(let dz=-1;dz<=1;dz++){
      for(const j of cells.get(`${cell[0]+dx},${cell[1]+dy},${cell[2]+dz}`)??[]){
        if(Math.hypot(...sub(points[j],point))<=tolerance){chosen=j;break search;}
      }
    }
    if(chosen===undefined){chosen=points.length;points.push(point);const key=cell.join(',');if(!cells.has(key))cells.set(key,[]);cells.get(key).push(chosen);}
    mapping[i]=chosen;
  }
  const index=[],edges=new Map(),faceSet=new Set();let duplicateFaces=0,degenerateTriangles=0;
  for(let k=0;k<faces.length;k+=3){
    const ids=[0,1,2].map(j=>mapping[faces[k+j]]),a=points[ids[0]],b=points[ids[1]],c=points[ids[2]];
    if(new Set(ids).size<3||area(a,b,c)<1e-14){degenerateTriangles++;continue;}
    const faceKey=ids.slice().sort((a,b)=>a-b).join(',');if(faceSet.has(faceKey))duplicateFaces++;faceSet.add(faceKey);index.push(...ids);
    for(let j=0;j<3;j++){
      const a=ids[j],b=ids[(j+1)%3],key=`${Math.min(a,b)},${Math.max(a,b)}`;
      if(!edges.has(key))edges.set(key,{count:0,orientation:0,a,b});
      const edge=edges.get(key);edge.count++;edge.orientation+=a<b?1:-1;
    }
  }
  const boundary=[...edges.values()].filter(edge=>edge.count===1),nonmanifold=[...edges.values()].filter(edge=>edge.count>2),
    inconsistent=[...edges.values()].filter(edge=>edge.count===2&&edge.orientation!==0);
  const outgoing=new Map();for(const edge of boundary){if(!outgoing.has(edge.a))outgoing.set(edge.a,[]);outgoing.get(edge.a).push(edge);}
  const branched=[...outgoing].filter(([,items])=>items.length!==1).length,visited=new Set(),loops=[];
  for(const edge of boundary){
    if(visited.has(edge))continue;const loop=[];let current=edge;
    while(current&&!visited.has(current)){visited.add(current);loop.push(current.a);current=outgoing.get(current.b)?.find(item=>!visited.has(item));}
    loops.push(loop);
  }
  return {vertices:Float64Array.from(points.flat()),faces:Uint32Array.from(index),loops,
    report:{boundaryEdges:boundary.length,boundaryLoops:loops.length,branchedBoundaryVertices:branched,nonmanifoldEdges:nonmanifold.length,
      inconsistentPairedEdges:inconsistent.length,duplicateFaces,degenerateTriangles}};
}

function closeBoundaries(mesh,label){
  assert.equal(mesh.report.nonmanifoldEdges,0,`${label} must have no nonmanifold edges`);
  assert.equal(mesh.report.inconsistentPairedEdges,0,`${label} must keep consistent face winding`);
  assert.equal(mesh.report.branchedBoundaryVertices,0,`${label} must have simple directed boundary loops`);
  const points=Array.from(mesh.vertices),faces=Array.from(mesh.faces);
  for(const loop of mesh.loops){
    const center=[0,0,0];for(const index of loop)for(let axis=0;axis<3;axis++)center[axis]+=mesh.vertices[index*3+axis]/loop.length;
    const middle=points.length/3;points.push(...center);
    for(let i=0;i<loop.length;i++)faces.push(loop[(i+1)%loop.length],loop[i],middle);
  }
  const result=weldedTopology(Float64Array.from(points),Uint32Array.from(faces));
  assert.equal(result.report.boundaryEdges,0,`${label} closure must remove every boundary edge`);
  assert.equal(result.report.nonmanifoldEdges,0,`${label} closure must remain manifold`);
  assert.equal(result.report.inconsistentPairedEdges,0,`${label} closure must keep opposite shared-edge orientation`);
  return result;
}

/** Clip each triangle once against both X planes, then close the two cuts.
 * Signed surface integration preserves a nonconvex cut's exact moments; this
 * cap convention does not promise a positive tetrahedral material mesh. */
export function clipClosedSkinSlab(mesh,minimum,maximum){
  const points=[],faces=[];
  for(let k=0;k<mesh.faces.length;k+=3){
    const ids=[mesh.faces[k],mesh.faces[k+1],mesh.faces[k+2]],xs=ids.map(i=>mesh.vertices[i*3]);
    if(Math.max(...xs)<minimum||Math.min(...xs)>maximum)continue;
    let polygon=ids.map(i=>vector(mesh.vertices,i));
    for(const [cut,greater] of [[minimum,true],[maximum,false]]){
      const output=[];
      for(let j=0;j<polygon.length;j++){
        const a=polygon[j],b=polygon[(j+1)%polygon.length],insideA=greater?a[0]>=cut:a[0]<=cut,insideB=greater?b[0]>=cut:b[0]<=cut;
        if(insideA)output.push(a);
        if(insideA!==insideB){const t=(cut-a[0])/(b[0]-a[0]);output.push([cut,a[1]+t*(b[1]-a[1]),a[2]+t*(b[2]-a[2])]);}
      }
      polygon=output;if(!polygon.length)break;
    }
    for(let j=1;j+1<polygon.length;j++){
      const start=points.length/3;points.push(...polygon[0],...polygon[j],...polygon[j+1]);faces.push(start,start+1,start+2);
    }
  }
  if(!faces.length)return null;
  return closeBoundaries(weldedTopology(Float64Array.from(points),Uint32Array.from(faces)),`slab ${minimum}..${maximum}`);
}

/** Capture only the actual uploaded neutral skin and axial origins. */
export async function loadNeutralSkinEnvelope(bytes){
  const loader=new GLTFLoader().register(()=>({name:'BodyMassImages',loadTexture:()=>Promise.resolve(new Texture())}));
  const {scene:asset}=await loader.parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'');
  const joints=[];asset.traverse(item=>{if(item.isBone&&item.name.startsWith('spine_'))joints.push(item);});
  joints.sort((a,b)=>a.name.localeCompare(b.name));assert.equal(joints.length,34,'mass generation requires all 34 axial origins');asset.updateMatrixWorld(true);
  assert.ok(joints.every((joint,i)=>joint.name===`spine_${String(i).padStart(2,'0')}`&&(i===0||joint.parent===joints[i-1])),'mass generation requires the ordered axial chain');
  const skin=asset.getObjectByName('Koi_skin');assert.ok(skin?.isSkinnedMesh,'mass generation requires the actual uploaded Koi_skin');
  const positions=new Float64Array(skin.geometry.attributes.position.array.length),point=new Vector3();
  skin.skeleton.update();for(let i=0;i<positions.length/3;i++){
    skin.getVertexPosition(i,point).applyMatrix4(skin.matrixWorld);asset.worldToLocal(point);point.toArray(positions,i*3);
  }
  const topology=weldedTopology(positions,skin.geometry.index.array),mesh=closeBoundaries(topology,'neutral skin envelope');
  assert.equal(topology.report.boundaryLoops,7,'expected one rostral, four narial and two opercular openings');
  assert.equal(topology.report.duplicateFaces,0,'neutral skin must have no duplicate faces');
  assert.equal(topology.report.degenerateTriangles,0,'neutral skin must have no degenerate triangles');
  const volume=integrateVolume(mesh.vertices,mesh.faces).volume;assert.ok(Number.isFinite(volume)&&volume!==0,'neutral envelope must have finite nonzero signed volume');
  const orientation=volume>0?1:-1;
  if(orientation<0)for(let k=0;k<mesh.faces.length;k+=3)[mesh.faces[k+1],mesh.faces[k+2]]=[mesh.faces[k+2],mesh.faces[k+1]];
  const boneX=joints.map(joint=>asset.worldToLocal(joint.getWorldPosition(new Vector3())).x);
  assert.ok(boneX.every((x,i)=>Number.isFinite(x)&&(i===0||x>boneX[i-1])),'axial origins must advance strictly along X');
  return {mesh,boneX,topology:topology.report};
}
