import {Vector3,Box3} from './vendor/three.webgpu.js';
import {waterMin,waterMax} from './aquarium-space.js?v=75';

const point=new Vector3();
const tankMin=new Vector3(...waterMin),tankMax=new Vector3(...waterMax);

export function createContainment(asset){
  const probes=[];
  asset.traverse(mesh=>{
    if(!mesh.isSkinnedMesh||mesh.name.startsWith('Anatomy')||mesh.name.includes('branched_rays')||mesh.name.startsWith('Overlapping_scales'))return;
    const positions=mesh.geometry.attributes.position,indices=new Set();
    if(mesh.name.startsWith('Koi_skin')){
      // Sixteen radial support directions per centimetre of the trunk. A
      // 7 mm guard covers the angular sampling and sub-millimetre scale rims.
      const bins=new Map();
      for(let i=0;i<positions.count;i++){
        const bin=Math.round(positions.getX(i)*100);
        if(!bins.has(bin))bins.set(bin,Array.from({length:16},()=>({score:-Infinity,index:0})));
        const supports=bins.get(bin),y=positions.getY(i),z=positions.getZ(i);
        for(let j=0;j<16;j++){
          const a=j*Math.PI/8,score=y*Math.cos(a)+z*Math.sin(a);
          if(score>supports[j].score)supports[j]={score,index:i};
        }
      }
      bins.forEach(supports=>supports.forEach(s=>indices.add(s.index)));
    }else{
      // Fin membranes are small and flexible: inspect all their vertices.
      const stride=mesh.name.includes('membrane')?1:4;
      for(let i=0;i<positions.count;i+=stride)indices.add(i);
      indices.add(positions.count-1);
    }
    probes.push({mesh,indices:[...indices]});
  });
  const bounds=new Box3();
  function measure(){
    bounds.makeEmpty();
    const skeletons=new Set(probes.map(p=>p.mesh.skeleton));skeletons.forEach(s=>s.update());
    for(const {mesh,indices} of probes)for(const index of indices){
      mesh.getVertexPosition(index,point);point.applyMatrix4(mesh.matrixWorld);bounds.expandByPoint(point);
    }
    return bounds;
  }
  function correction(){
    measure();const result={x:0,y:0,z:0},margin=.007;
    for(const axis of ['x','y','z']){
      if(bounds.min[axis]<tankMin[axis]+margin)result[axis]=tankMin[axis]+margin-bounds.min[axis];
      if(bounds.max[axis]>tankMax[axis]-margin)result[axis]=tankMax[axis]-margin-bounds.max[axis];
    }
    return result;
  }
  return {measure,correction,probeCount:probes.reduce((n,p)=>n+p.indices.length,0)};
}
