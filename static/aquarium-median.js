/* Independent median-fin shape control. Bluegill observations motivate the
 * modes, not the authored gains: Standen & Lauder, doi:10.1242/jeb.01706.
 * Rays retain their material lengths after the actual skeletal deformation.
 * This reduced-order animation field does not calculate water forces. */
import {Matrix4} from './vendor/three.webgpu.js';
import {createFinNormalSmoother} from './aquarium-fin-normals.js?v=90';

const clamp=(value,low,high)=>Math.max(low,Math.min(high,value));
const smooth=value=>value*value*(3-2*value);
const dot=(a,b)=>a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const normalize=value=>{const length=Math.hypot(...value);return value.map(item=>item/length);};
const point=(values,i)=>[values[i*3],values[i*3+1],values[i*3+2]];
const subtract=(a,b)=>a.map((value,i)=>value-b[i]);

export function createMedianMotion(fish,kind='dorsal',output={}){
  if(kind!=='dorsal'&&kind!=='anal')throw new Error('Unknown median fin kind');
  const turn=clamp((fish.yawRate??0)*.9+(fish.finSteer??0)*.35,-1,1);
  // Quiet forward glides retain the neutral fin; there is no assumed universal
  // collapse or decorative oscillation. Maneuvering makes a small signed bias.
  return Object.assign(output,{incline:(kind==='anal'?.080:.10)*turn,curve:(kind==='anal'?.085:.10)*turn});
}

export function createMedianController(kind='dorsal'){
  let motion,target,age,poseKey;
  const velocity={incline:0,curve:0};
  const settle=(name,wanted,rate,dt)=>{
    const error=motion[name]-wanted,b=velocity[name]+rate*error,decay=Math.exp(-rate*dt);
    motion[name]=wanted+(error+b*dt)*decay;
    velocity[name]=(velocity[name]-rate*b*dt)*decay;
  };
  return {sample(fish,key=fish.poseHistory===false?'held':'live'){
    const now=fish.age??0;
    target=createMedianMotion(fish,kind,target);
    if(key!==poseKey||age===undefined||now<age||now-age>.25){
      motion={...target};velocity.incline=velocity.curve=0;
    }else if(now===age)return motion;
    else{
      const dt=now-age;
      settle('incline',target.incline,kind==='anal'?12:14,dt);
      // Distal shape follows an observed basal response, not an invented
      // command history. Reversal and unloading remain finite and continuous.
      settle('curve',motion.incline*(kind==='anal'?.085/.080:1),kind==='anal'?7:8,dt);
    }
    age=now;poseKey=key;return motion;
  }};
}

function inverse3(a,b,c,d,e,f,g,h,i,out,offset){
  const A=e*i-f*h,B=c*h-b*i,C=b*f-c*e;
  const D=f*g-d*i,E=a*i-c*g,F=c*d-a*f;
  const G=d*h-e*g,H=b*g-a*h,I=a*e-b*d,det=a*A+b*D+c*G;
  if(Math.abs(det)<1e-12)throw new Error('Median fin skin or material frame is singular');
  const values=[A,B,C,D,E,F,G,H,I];
  for(let j=0;j<9;j++)out[offset+j]=values[j]/det;
  return det;
}

function createBinding(mesh){
  if(!mesh?.isSkinnedMesh)throw new Error('Median fins require their actual skinned meshes');
  const attributes=mesh.geometry.attributes,count=attributes.position.count;
  if(!attributes.normal||!attributes.skinIndex||!attributes.skinWeight)throw new Error('Median fin skin attributes are missing');
  return {mesh,count,base:Float64Array.from(attributes.position.array),authoredNormals:Float64Array.from(attributes.normal.array),
    joints:Uint16Array.from(attributes.skinIndex.array),weights:Float64Array.from(attributes.skinWeight.array),
    matrices:new Float64Array(count*12),inverses:new Float64Array(count*9),
    posed:new Float64Array(count*3),target:new Float64Array(count*3),normals:new Float64Array(count*3),
    palette:new Float64Array(mesh.skeleton.bones.length*12),bone:new Matrix4(),skin:new Matrix4(),
    reconcile:createFinNormalSmoother(mesh.geometry)};
}

function tubeFrame(centers,ring,gx,gy,gz,output){
  const before=Math.max(0,ring-1)*3,after=Math.min(centers.length/3-1,ring+1)*3;
  let tx=centers[after]-centers[before],ty=centers[after+1]-centers[before+1],tz=centers[after+2]-centers[before+2];
  const length=Math.hypot(tx,ty,tz);
  if(length<1e-12)throw new Error('Median tube has a degenerate material tangent');
  tx/=length;ty/=length;tz/=length;
  const along=gx*tx+gy*ty+gz*tz;
  let rx=gx-along*tx,ry=gy-along*ty,rz=gz-along*tz;
  const radius=Math.hypot(rx,ry,rz);
  if(radius<1e-12)throw new Error('Median tube has a degenerate radial frame');
  rx/=radius;ry/=radius;rz/=radius;
  output.set([rx,ry,rz,ty*rz-tz*ry,tz*rx-tx*rz,tx*ry-ty*rx,tx,ty,tz]);
}

function createTubeTrees(binding,atlas){
  const {mesh,count,base}=binding,index=mesh.geometry.index.array,uv=mesh.geometry.attributes.uv;
  const parent=Array.from({length:count},(_,i)=>i),find=i=>parent[i]===i?i:parent[i]=find(parent[i]);
  for(let i=0;i<index.length;i+=3){const root=find(index[i]);parent[find(index[i+1])]=root;parent[find(index[i+2])]=root;}
  const components=new Map();
  for(let i=0;i<count;i++){const root=find(i);if(!components.has(root))components.set(root,[]);components.get(root).push(i);}
  const tubes=[];
  for(const vertices of components.values()){
    const groups=new Map();
    for(const i of vertices){const u=uv.getX(i);if(!groups.has(u))groups.set(u,[]);groups.get(u).push(i);}
    const rings=[...groups].sort(([a],[b])=>a-b).map(([,all])=>({all,unique:all.filter(i=>uv.getY(i)<.999)}));
    if(!rings.every(ring=>ring.unique.length===8))throw new Error('Median tube ring sampling is unsupported');
    const centers=new Float64Array(rings.length*3);
    for(let j=0;j<rings.length;j++)for(let k=0;k<3;k++)centers[j*3+k]=rings[j].unique.reduce((sum,i)=>sum+base[i*3+k],0)/8;
    const lengths=new Float64Array(rings.length-1);
    for(let j=1;j<rings.length;j++)lengths[j-1]=Math.hypot(...subtract(point(centers,j),point(centers,j-1)));
    for(let j=0;j<rings.length;j++){
      const ring=rings[j],guide=ring.unique[0]*3,center=j*3;
      ring.frame=new Float64Array(9);ring.coordinates=new Float64Array(ring.all.length*3);
      tubeFrame(centers,j,base[guide]-centers[center],base[guide+1]-centers[center+1],base[guide+2]-centers[center+2],ring.frame);
      for(let n=0;n<ring.all.length;n++){
        const index=ring.all[n]*3,x=base[index]-centers[center],y=base[index+1]-centers[center+1],z=base[index+2]-centers[center+2];
        for(let axis=0;axis<3;axis++)ring.coordinates[n*3+axis]=x*ring.frame[axis*3]+y*ring.frame[axis*3+1]+z*ring.frame[axis*3+2];
      }
    }
    const number=tubes.length,shaft=number%3===0;
    if(rings.length!==(shaft?25:9))throw new Error('Median tube tree order is unsupported');
    const tree=Math.floor(number/3),root=atlas.paths[tree*2+1]?.indices[0];
    if(root===undefined)throw new Error('Median tube has no corresponding material ray');
    let junction=0,relative;
    if(shaft)relative=subtract(point(centers,0),point(atlas.base,root));
    else{
      const primary=tubes[tree*3];let nearest=Infinity;
      for(let j=0;j<primary.rings.length;j++){
        const distance=Math.hypot(...subtract(point(centers,0),point(primary.centers,j)));
        if(distance<nearest){nearest=distance;junction=j;}
      }
      relative=subtract(point(centers,0),point(primary.centers,junction));
    }
    if(Math.hypot(...relative)>.000001)throw new Error('Median tube attachment does not match its material tree');
    let border;
    if(!shaft){
      const tip=atlasPoint(...point(centers,rings.length-1),atlas),uv=atlas.uv;
      const edge=tip.indices.filter(index=>uv.getY(index)<.001),inside=tip.indices.find(index=>uv.getY(index)>.001);
      if(edge.length===2&&inside!==undefined)border={edge,inside};
    }
    tubes.push({rings,centers,lengths,shaft,tree,root,junction,relative,
      border,radius:Math.max(...rings[0].coordinates.filter((_,i)=>i%3!==2).map(Math.abs)),
      original:new Float64Array(centers.length),corrected:new Float64Array(centers.length)});
  }
  return tubes;
}

function createAtlas(binding){
  const {mesh,base}=binding,uv=mesh.geometry.attributes.uv,rows=new Map();
  if(!uv)throw new Error('Median radial material coordinates are missing');
  for(let i=0;i<binding.count;i++){
    const u=uv.getX(i);if(!rows.has(u))rows.set(u,[]);
    rows.get(u).push(i);
  }
  const paths=[...rows].sort(([a],[b])=>a-b).map(([,indices])=>{
    indices.sort((a,b)=>uv.getY(b)-uv.getY(a));
    if(uv.getY(indices[0])<.999)throw new Error('Median material ray has no basal attachment');
    const lengths=new Float64Array(indices.length-1);
    for(let j=1;j<indices.length;j++)lengths[j-1]=Math.hypot(...subtract(point(base,indices[j]),point(base,indices[j-1])));
    return {indices:Uint32Array.from(indices),lengths};
  });
  const triangles=[],index=mesh.geometry.index.array;
  for(let i=0;i<index.length;i+=3){
    const indices=Array.from(index.subarray(i,i+3)),a=point(base,indices[0]),u=subtract(point(base,indices[1]),a),v=subtract(point(base,indices[2]),a);
    const det=u[0]*v[1]-u[1]*v[0],normal=cross(u,v),area=Math.hypot(...normal);
    // The anal radial fan repeats one root. Its zero-area root triangles
    // carry no material surface and cannot define an atlas frame.
    if(area<1e-14||Math.abs(det)<1e-14)continue;
    for(let k=0;k<3;k++)normal[k]/=area;
    const inverse=new Float64Array(9);
    inverse3(u[0],v[0],normal[0],u[1],v[1],normal[1],u[2],v[2],normal[2],inverse,0);
    triangles.push({indices,a,u,v,det,inverse,
      minX:Math.min(...indices.map(j=>base[j*3])),maxX:Math.max(...indices.map(j=>base[j*3])),
      minY:Math.min(...indices.map(j=>base[j*3+1])),maxY:Math.max(...indices.map(j=>base[j*3+1]))});
  }
  if(!triangles.length||paths.length<2)throw new Error('Median material atlas is empty');
  return {paths,triangles,base,uv};
}

function atlasPoint(x,y,z,atlas){
  let nearest=Infinity,chosen;
  for(const triangle of atlas.triangles){
    const dx=Math.max(triangle.minX-x,0,x-triangle.maxX),dy=Math.max(triangle.minY-y,0,y-triangle.maxY);
    if(dx*dx+dy*dy>nearest)continue;
    const px=x-triangle.a[0],py=y-triangle.a[1];
    const b=(px*triangle.v[1]-py*triangle.v[0])/triangle.det,c=(triangle.u[0]*py-triangle.u[1]*px)/triangle.det;
    if(b>=-1e-10&&c>=-1e-10&&b+c<=1+1e-10){chosen=triangle;break;}
    for(const [a,b] of [[0,1],[1,2],[2,0]]){
      const i=triangle.indices[a],j=triangle.indices[b],ux=atlas.base[j*3]-atlas.base[i*3],uy=atlas.base[j*3+1]-atlas.base[i*3+1];
      const t=clamp(((x-atlas.base[i*3])*ux+(y-atlas.base[i*3+1])*uy)/(ux*ux+uy*uy),0,1);
      const squared=(x-atlas.base[i*3]-t*ux)**2+(y-atlas.base[i*3+1]-t*uy)**2;
      if(squared<nearest){nearest=squared;chosen=triangle;}
    }
  }
  if(!chosen)throw new Error('Median ray has no material surface correspondence');
  const delta=[x-chosen.a[0],y-chosen.a[1],z-chosen.a[2]],q=chosen.inverse;
  return {indices:chosen.indices,coordinates:[dot(q.subarray(0,3),delta),dot(q.subarray(3,6),delta),dot(q.subarray(6,9),delta)]};
}

export function createMedianFin(membraneMesh,rayMesh,{kind='dorsal'}={}){
  if(kind!=='dorsal'&&kind!=='anal')throw new Error('Unknown median fin kind');
  membraneMesh.updateMatrixWorld(true);rayMesh.updateMatrixWorld(true);
  const membrane=createBinding(membraneMesh),rays=createBinding(rayMesh),atlas=createAtlas(membrane);
  const values=new Float64Array(rays.count*6);
  for(let i=0;i<rays.count;i++){
    const found=atlasPoint(rays.base[i*3],rays.base[i*3+1],rays.base[i*3+2],atlas);
    values.set(found.indices,i*6);values.set(found.coordinates,i*6+3);
  }
  rays.material=values;rays.tubes=createTubeTrees(rays,atlas);
  return {kind,membrane:membraneMesh,rays:rayMesh,bindings:[membrane,rays],atlas,restPaths:atlas.paths,
    minimumSkinDeterminant:1};
}

function constrainTubeTrees(family){
  const membrane=family.bindings[0],binding=family.bindings[1],target=binding.target;
  let maximum=0;
  for(const tube of binding.tubes){
    const {rings,lengths,original,corrected,root,relative}=tube,m=root*12;
    for(let j=0;j<rings.length;j++)for(let k=0;k<3;k++)original[j*3+k]=rings[j].unique.reduce((sum,i)=>sum+target[i*3+k],0)/8;
    const anchor=tube.shaft?membrane.target.subarray(root*3,root*3+3):binding.tubes[tube.tree*3].corrected.subarray(tube.junction*3,tube.junction*3+3);
    for(let k=0;k<3;k++)corrected[k]=anchor[k]+membrane.matrices[m+k*4]*relative[0]+membrane.matrices[m+k*4+1]*relative[1]+membrane.matrices[m+k*4+2]*relative[2];
    for(let j=1;j<rings.length;j++){
      const a=(j-1)*3,b=j*3,dx=original[b]-original[a],dy=original[b+1]-original[a+1],dz=original[b+2]-original[a+2];
      const scale=lengths[j-1]/Math.hypot(dx,dy,dz);
      corrected[b]=corrected[a]+dx*scale;corrected[b+1]=corrected[a+1]+dy*scale;corrected[b+2]=corrected[a+2]+dz*scale;
    }
    if(!tube.shaft&&tube.border){
      // A branch terminates on the membrane edge. When its chord shortens,
      // distribute that slack through its bend instead of pushing a rigidly
      // integrated free tip outside the tissue. Both passes retain each
      // captured segment length; the parent junction remains the anchor.
      const last=corrected.length-3,anchor=corrected.slice(0,3),length=lengths.reduce((sum,value)=>sum+value,0);
      const edge=tube.border.edge.map(index=>point(membrane.target,index)),inside=point(membrane.target,tube.border.inside);
      const alongEdge=normalize(subtract(edge[1],edge[0])),inward=subtract(inside,edge[0]),along=dot(inward,alongEdge);
      for(let k=0;k<3;k++)inward[k]-=along*alongEdge[k];
      const normalInward=normalize(inward),protrusion=-dot(subtract(point(corrected,rings.length-1),point(original,rings.length-1)),normalInward);
      // Begin contact correction smoothly while some tube wall still covers
      // the edge, reaching full correction at one captured branch radius.
      const contact=smooth(clamp((protrusion/tube.radius-.5)/.5,0,1));
      const tipTarget=point(corrected,rings.length-1).map((value,k)=>value+(original[last+k]-value)*contact);
      const chord=Math.hypot(...subtract(tipTarget,Array.from(anchor)));
      if(chord<length&&contact>0){
        const material=rings[Math.floor(rings.length/2)].unique[0]*6,surface=membrane.target;
        const [a,b,c]=[0,1,2].map(k=>point(surface,binding.material[material+k]));
        const normal=normalize(cross(subtract(b,a),subtract(c,a))),direction=normalize(subtract(point(original,rings.length-1),Array.from(anchor)));
        const transverse=normalize(cross(normal,direction)),toward=subtract(point(surface,root),point(original,Math.floor(rings.length/2)));
        // A small in-plane seed selects inward bending for nearly collinear
        // branches. Its size is geometric and is not a measured tissue gain.
        const bow=Math.sqrt(Math.max(0,length*length-chord*chord))*.005*contact*(dot(transverse,toward)<0?-1:1);
        for(let j=1;j<rings.length-1;j++)for(let k=0;k<3;k++)corrected[j*3+k]+=transverse[k]*bow*Math.sin(Math.PI*j/(rings.length-1));
        // Bound live work to 32 deterministic passes; any remaining tip error
        // is retained, rather than stretching the ray to conceal the residual.
        for(let iteration=0;iteration<32;iteration++){
          corrected.set(tipTarget,last);
          for(let j=rings.length-2;j>=0;j--){
            const a=j*3,b=a+3,dx=corrected[a]-corrected[b],dy=corrected[a+1]-corrected[b+1],dz=corrected[a+2]-corrected[b+2];
            const scale=lengths[j]/Math.hypot(dx,dy,dz);
            corrected[a]=corrected[b]+dx*scale;corrected[a+1]=corrected[b+1]+dy*scale;corrected[a+2]=corrected[b+2]+dz*scale;
          }
          corrected.set(anchor,0);
          for(let j=1;j<rings.length;j++){
            const a=(j-1)*3,b=j*3,dx=corrected[b]-corrected[a],dy=corrected[b+1]-corrected[a+1],dz=corrected[b+2]-corrected[a+2];
            const scale=lengths[j-1]/Math.hypot(dx,dy,dz);
            corrected[b]=corrected[a]+dx*scale;corrected[b+1]=corrected[a+1]+dy*scale;corrected[b+2]=corrected[a+2]+dz*scale;
          }
          if(Math.hypot(corrected[last]-tipTarget[0],corrected[last+1]-tipTarget[1],corrected[last+2]-tipTarget[2])<1e-7)break;
        }
      }
    }
    for(let j=0;j<rings.length;j++){
      const dx=corrected[j*3]-original[j*3],dy=corrected[j*3+1]-original[j*3+1],dz=corrected[j*3+2]-original[j*3+2];
      maximum=Math.max(maximum,Math.hypot(dx,dy,dz));
      const ring=rings[j],center=j*3,guide=ring.unique[0]*3;
      // The tissue atlas supplies orientation, not a new ray thickness. A
      // strong body bend can squeeze its in-plane coordinates while the
      // separately constrained centerline keeps its arc. Carry each captured
      // annulus rigidly around that corrected tangent instead of retaining
      // the squeezed tube wall or merely translating its center.
      tubeFrame(corrected,j,target[guide]-original[center],target[guide+1]-original[center+1],target[guide+2]-original[center+2],ring.frame);
      for(let n=0;n<ring.all.length;n++){
        const i=ring.all[n]*3,a=ring.coordinates[n*3],b=ring.coordinates[n*3+1],c=ring.coordinates[n*3+2];
        for(let k=0;k<3;k++)target[i+k]=corrected[center+k]+ring.frame[k]*a+ring.frame[k+3]*b+ring.frame[k+6]*c;
      }
    }
  }
  family.maximumTubeCorrection=maximum;
}

function updateSkin(binding){
  const {mesh,palette,bone,skin,matrices,inverses,base,posed,joints,weights}=binding;
  for(let i=0;i<mesh.skeleton.bones.length;i++){
    bone.multiplyMatrices(mesh.skeleton.bones[i].matrixWorld,mesh.skeleton.boneInverses[i]);
    skin.multiplyMatrices(mesh.bindMatrixInverse,bone).multiply(mesh.bindMatrix);
    const e=skin.elements,o=i*12;
    for(let row=0;row<3;row++)for(let col=0;col<4;col++)palette[o+row*4+col]=e[col*4+row];
  }
  matrices.fill(0);let minimum=Infinity;
  for(let i=0;i<binding.count;i++){
    const o=i*12;
    for(let slot=0;slot<4;slot++){
      const weight=weights[i*4+slot];if(!weight)continue;
      const bone=joints[i*4+slot]*12;
      for(let j=0;j<12;j++)matrices[o+j]+=weight*palette[bone+j];
    }
    const m=matrices,x=base[i*3],y=base[i*3+1],z=base[i*3+2];
    posed[i*3]=m[o]*x+m[o+1]*y+m[o+2]*z+m[o+3];
    posed[i*3+1]=m[o+4]*x+m[o+5]*y+m[o+6]*z+m[o+7];
    posed[i*3+2]=m[o+8]*x+m[o+9]*y+m[o+10]*z+m[o+11];
    minimum=Math.min(minimum,inverse3(m[o],m[o+1],m[o+2],m[o+4],m[o+5],m[o+6],m[o+8],m[o+9],m[o+10],inverses,i*9));
  }
  return minimum;
}

function tissue(family,motion){
  const binding=family.bindings[0],{posed,target,matrices}=binding;
  for(let row=0;row<family.atlas.paths.length;row++){
    const {indices,lengths}=family.atlas.paths[row],u=row/(family.atlas.paths.length-1);
    // The leading support stays comparatively quiet. Posterior tissue has
    // more lateral freedom; this smooth distribution is authored, not a
    // species-specific stiffness or actuation measurement.
    const inclineGain=.25+.75*smooth(u),curveGain=.10+.90*smooth(u*u);
    const root=indices[0],tip=indices[indices.length-1],m=root*12;
    const lateral=normalize([matrices[m+2],matrices[m+6],matrices[m+10]]);
    // An anal fan includes posterior rays pointing above their basal root.
    // A fixed body-X hinge would move those rays to the opposite side. The
    // ray-relative hinge rotates each posed chord toward the same lateral
    // direction, and stays fixed along that ray for its basal presentation.
    const chord=subtract(point(posed,tip),point(posed,root)),axis=normalize(cross(chord,lateral));
    target.set(posed.subarray(root*3,root*3+3),root*3);
    for(let j=1;j<indices.length;j++){
      const a=indices[j-1]*3,b=indices[j]*3,s=(j-.5)/(indices.length-1);
      const angle=(motion.incline??0)*inclineGain+(motion.curve??0)*curveGain*s*s;
      const tangent=normalize([posed[b]-posed[a],posed[b+1]-posed[a+1],posed[b+2]-posed[a+2]]);
      const c=Math.cos(angle),q=Math.sin(angle),along=dot(axis,tangent)*(1-c),normal=cross(axis,tangent);
      for(let k=0;k<3;k++)target[b+k]=target[a+k]+lengths[j-1]*(tangent[k]*c+normal[k]*q+axis[k]*along);
    }
  }
}

function rayTissue(family){
  const surface=family.bindings[0].target,binding=family.bindings[1],values=binding.material,output=binding.target;
  for(let i=0;i<binding.count;i++){
    const o=i*6,a=values[o]*3,b=values[o+1]*3,c=values[o+2]*3;
    const u=[surface[b]-surface[a],surface[b+1]-surface[a+1],surface[b+2]-surface[a+2]];
    const v=[surface[c]-surface[a],surface[c+1]-surface[a+1],surface[c+2]-surface[a+2]],normal=normalize(cross(u,v));
    for(let k=0;k<3;k++)output[i*3+k]=surface[a+k]+values[o+3]*u[k]+values[o+4]*v[k]+values[o+5]*normal[k];
  }
}

function posedNormals(binding){
  const {target,normals,mesh}=binding,index=mesh.geometry.index.array;
  normals.fill(0);
  for(let i=0;i<index.length;i+=3){
    const a=index[i]*3,b=index[i+1]*3,c=index[i+2]*3;
    const ux=target[b]-target[a],uy=target[b+1]-target[a+1],uz=target[b+2]-target[a+2];
    const vx=target[c]-target[a],vy=target[c+1]-target[a+1],vz=target[c+2]-target[a+2];
    const x=uy*vz-uz*vy,y=uz*vx-ux*vz,z=ux*vy-uy*vx;
    for(const o of [a,b,c]){normals[o]+=x;normals[o+1]+=y;normals[o+2]+=z;}
  }
  for(let i=0;i<binding.count;i++){
    const o=i*3,length=Math.hypot(normals[o],normals[o+1],normals[o+2]);
    if(length>1e-15){normals[o]/=length;normals[o+1]/=length;normals[o+2]/=length;}
    else{
      // The last duplicated anal root belongs only to zero-area triangles.
      // Preserve its captured authored direction in the actual posed frame.
      const m=i*12,x=binding.authoredNormals[o],y=binding.authoredNormals[o+1],z=binding.authoredNormals[o+2],matrix=binding.matrices;
      for(let k=0;k<3;k++)normals[o+k]=matrix[m+k*4]*x+matrix[m+k*4+1]*y+matrix[m+k*4+2]*z;
      const fallback=Math.hypot(normals[o],normals[o+1],normals[o+2]);
      if(fallback>1e-15)for(let k=0;k<3;k++)normals[o+k]/=fallback;
    }
  }
  binding.reconcile(normals);
}

function upload(binding){
  const {mesh,target,normals,matrices,inverses}=binding,positions=mesh.geometry.attributes.position.array,
    outputNormals=mesh.geometry.attributes.normal.array;
  for(let i=0;i<binding.count;i++){
    const o=i*3,m=i*12,q=i*9,x=target[o]-matrices[m+3],y=target[o+1]-matrices[m+7],z=target[o+2]-matrices[m+11];
    for(let k=0;k<3;k++)positions[o+k]=inverses[q+k*3]*x+inverses[q+k*3+1]*y+inverses[q+k*3+2]*z;
    const nx=normals[o],ny=normals[o+1],nz=normals[o+2];
    let length=0;
    for(let k=0;k<3;k++){
      const value=inverses[q+k*3]*nx+inverses[q+k*3+1]*ny+inverses[q+k*3+2]*nz;
      outputNormals[o+k]=value;length+=value*value;
    }
    length=Math.sqrt(length);
    if(length>1e-15)for(let k=0;k<3;k++)outputNormals[o+k]/=length;
  }
  mesh.geometry.attributes.position.needsUpdate=true;mesh.geometry.attributes.normal.needsUpdate=true;
}

export function deformMedianFin(family,motion){
  // Always start from captured original geometry, never the previous corrected
  // buffer. Repeated poses and root-only transforms therefore cannot drift.
  let determinant=Infinity;
  for(const binding of family.bindings)determinant=Math.min(determinant,updateSkin(binding));
  family.minimumSkinDeterminant=determinant;
  tissue(family,motion);rayTissue(family);constrainTubeTrees(family);
  for(const binding of family.bindings){posedNormals(binding);upload(binding);}
  return family;
}
