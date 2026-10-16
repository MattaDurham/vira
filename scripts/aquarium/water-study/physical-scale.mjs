/* Experimental SI authoring adapter. Uniform geometry scaling is not species transfer,
 * a passive material law, or Reynolds/dynamic similarity. No simulation here. */
import {Vector3,Quaternion,Matrix4} from '../../../static/vendor/three.webgpu.js';

const finite=(x,name)=>{if(!Number.isFinite(x))throw new Error(`Finite ${name} required`);return x;};
const xyz=(value)=>{
  const values=[value?.x??value?.[0],value?.y??value?.[1],value?.z??value?.[2]];
  if(!values.every(Number.isFinite))throw new Error('Finite xyz vector required');return new Vector3(...values);
};
const cloneJSON=value=>JSON.parse(JSON.stringify(value));
const copiedQ=value=>{
  const values=[value?.x??value?.[0],value?.y??value?.[1],value?.z??value?.[2],value?.w??value?.[3]];
  if(!values.every(Number.isFinite))throw new Error('Finite quaternion required');
  const q=new Quaternion(...values);if(q.lengthSq()<1e-20)throw new Error('Nonzero quaternion required');return q;
};
const scaleValues=(values,factor)=>Array.from(values,x=>finite(x,'coefficient')*factor);

// Neutral material vertices in the animal's native coordinates. Including the
// caudal membrane, rather than its last bone origin, is essential for full TL.
export function measureNativeTotalLength(asset){
  asset.updateMatrixWorld(true);
  const inverse=new Matrix4().copy(asset.matrixWorld).invert(),point=new Vector3(),meshes={};
  for(const name of ['Koi_skin','Caudal_membrane']){
    const mesh=asset.getObjectByName(name);if(!mesh?.isSkinnedMesh)throw new Error(`Actual neutral ${name} required`);
    mesh.skeleton.update();const min=new Vector3(Infinity,Infinity,Infinity),max=new Vector3(-Infinity,-Infinity,-Infinity);
    for(let i=0;i<mesh.geometry.attributes.position.count;i++){
      mesh.getVertexPosition(i,point).applyMatrix4(mesh.matrixWorld).applyMatrix4(inverse);
      if(!point.toArray().every(Number.isFinite))throw new Error('Nonfinite material vertex');
      min.min(point);max.max(point);
    }
    meshes[name]={min:min.toArray(),max:max.toArray(),vertices:mesh.geometry.attributes.position.count};
  }
  const anteriorX=meshes.Koi_skin.min[0],posteriorX=meshes.Caudal_membrane.max[0],length=posteriorX-anteriorX;
  if(!(length>0))throw new Error('Positive full material length required');
  return Object.freeze({length,anteriorX,posteriorX,meshes,
    definition:'Neutral animal-coordinate Koi_skin minX to Caudal_membrane maxX; actual deformed/skinned GLB vertices, no guessed metadata or bone-to-bone surrogate'});
}

export function createPhysicalScaleAdapter({asset,physicalLengthM,nativeMeasurement=measureNativeTotalLength(asset)}){
  if(!(finite(physicalLengthM,'physical length')>0)||!(finite(nativeMeasurement.length,'native length')>0))throw new Error('Positive lengths required');
  const S=physicalLengthM/nativeMeasurement.length,S2=S*S,S3=S2*S,S4=S3*S,S5=S4*S;
  if(![S,S2,S3,S4,S5,1/S].every(x=>Number.isFinite(x)&&x>0))throw new Error('Representable positive physical scale powers required');
  const point=(value)=>xyz(value).multiplyScalar(S),renderPoint=(value)=>xyz(value).multiplyScalar(1/S);
  function transformPacket(packet,factor,out){
    if(!packet.positions||!packet.rotations||packet.positions.length!==packet.rotations.length)throw new Error('Matched position/rotation packet required');
    out??={positions:packet.positions.map(()=>new Vector3()),rotations:packet.rotations.map(()=>new Quaternion())};
    if(out.positions.length!==packet.positions.length||out.rotations.length!==packet.rotations.length)throw new Error('Matched output packet required');
    if(out===packet||out.positions.some(p=>packet.positions.includes(p))||out.rotations.some(q=>packet.rotations.includes(q)))throw new Error('Transform output must not alias source packet');
    for(let i=0;i<packet.positions.length;i++){
      out.positions[i].copy(xyz(packet.positions[i])).multiplyScalar(factor);
      out.rotations[i].copy(copiedQ(packet.rotations[i]));
    }
    if(packet.age!==undefined)out.age=finite(packet.age,'age');
    return out;
  }
  function scaleSampler(native){
    const centers=native.centers.map(point),rest=native.rest.map(q=>q.clone()),parents=[...native.parents],axial=native.axial.map(x=>x*S);
    const createFrame=()=>({age:0,positions:centers.map(()=>new Vector3()),rotations:rest.map(()=>new Quaternion())});
    const scratch=createFrame(),nativeScratch=native.createFrame();
    // The native sampler's spinalField deliberately retains its native geometry,
    // source-clock phase and dimensionless angular command convention. Scaling
    // its axial inputs would alter its fixed-length wave amplitudes a second time.
    function capture(fish,out=scratch){return transformPacket(native.capture(fish,nativeScratch),S,out);}
    return {centers,rest,parents,axial,createFrame,capture,physicalScale:S,sourceSampler:native};
  }
  function scaleMassProfile(native){
    if(!Array.isArray(native.bodies29)||native.bodies29.length!==29)throw new Error('29 native physical bodies required');
    const fields=new Set(['source','closure','density','units','partition','assetSHA256','bodies29','regions']);
    if(Object.keys(native).some(key=>!fields.has(key))||native.regions?.some(r=>Object.keys(r).some(key=>key!=='x')))
      throw new Error('Use compact body-mass.json profile, optionally with x-only water regions; extended dimensional profiles are unsupported');
    const out=cloneJSON(native);
    out.bodies29=native.bodies29.map(body=>{
      if(!(finite(body.mass,'body mass')>0)||body.com.length!==3||body.inertia.length!==9)throw new Error('Positive mass and full COM/inertia required');
      const bodyFields=new Set(['regions','mass','com','inertia','volume','firstVolumeMoment','secondVolumeMoment']);
      if(Object.keys(body).some(key=>!bodyFields.has(key)))throw new Error('Unsupported dimensional body metadata');
      const b={...cloneJSON(body),mass:body.mass*S3,com:scaleValues(body.com,S),inertia:scaleValues(body.inertia,S5)};
      if(body.volume!==undefined)b.volume=finite(body.volume,'volume')*S3;
      if(body.firstVolumeMoment!==undefined)b.firstVolumeMoment=scaleValues(body.firstVolumeMoment,S4);
      if(body.secondVolumeMoment!==undefined)b.secondVolumeMoment=scaleValues(body.secondVolumeMoment,S5);
      return b;
    });
    // Some ignored water-profile callers append physical X interval metadata.
    // The integer body.regions membership indices must never be scaled.
    if(native.regions)out.regions=native.regions.map(region=>({...cloneJSON(region),...(region.x?{x:scaleValues(region.x,S)}:{})}));
    out.physicalScaling={nativeAssetSHA256:native.assetSHA256,S,physicalLengthM,nativeLengthM:nativeMeasurement.length,
      density:'unchanged kg/m^3',mass:'native*S^3 kg',inertia:'native*S^5 kg m^2',com:'native*S m',speciesCalibration:false};
    return out;
  }
  function scaleSurface(native){
    if(!Array.isArray(native.samples))throw new Error('Material surface samples required');
    const positiveArea=value=>{if(!(finite(value,'surface area')>0))throw new Error('Positive area required');return value*S2;};
    const out={...cloneJSON({...native,samples:undefined}),samples:native.samples.map(sample=>{
      if(sample.dynamic||sample.intrinsicVelocity!==undefined)throw new Error('Only fixed rigid native surface samples are supported; dynamic/intrinsic material velocity needs an explicit live SI adapter');
      const normal=xyz(sample.localNormal);if(Math.abs(normal.length()-1)>1e-5)throw new Error('Unit surface normal required');
      return {...cloneJSON({...sample,localPoint:undefined,localNormal:undefined}),localPoint:point(sample.localPoint),localNormal:normal,area:positiveArea(sample.area)};
    })};
    if(native.totalArea!==undefined)out.totalArea=positiveArea(native.totalArea);
    if(native.membrane)out.membrane={...cloneJSON(native.membrane),area:positiveArea(native.membrane.area)};
    out.physicalScale=S;return out;
  }
  function scaleTorqueCaps(native){
    if(native.length!==28||Array.from(native).some(x=>!Number.isFinite(x)||x<=0))throw new Error('28 positive native torque capacities required');
    return scaleValues(native,S3);
  }
  function scaleActiveCoefficients(nativeLinks){
    if(nativeLinks.length!==28)throw new Error('28 native active links required');
    return nativeLinks.map(link=>{
      const values={stiffness:link.stiffness,damping:link.damping,cap:link.cap};
      if(Object.values(values).some(x=>!Number.isFinite(x)||x<0)||values.stiffness===0||values.cap===0)throw new Error('Positive active stiffness/cap and nonnegative damping required');
      return Object.fromEntries(Object.entries(values).map(([key,value])=>[key,value*S3]));
    });
  }
  function applyActiveLaw(physicalChain,nativeLinks){
    if(physicalChain.links.length!==28||physicalChain.mode!=='off')throw new Error('Owned physical chain must be configured off before active-law authoring');
    const values=scaleActiveCoefficients(nativeLinks);
    // The constructor otherwise regenerates K,D,cap from scaled inertia, giving
    // S^5. Replace only active control coefficients from the NATIVE baseline.
    // Keep actual physical effective inertia, joint anchors and bodies intact.
    values.forEach((value,i)=>Object.assign(physicalChain.links[i],value));return values;
  }
  return Object.freeze({S,S2,S3,S4,S5,physicalLengthM,nativeMeasurement,point,renderPoint,
    scaleSampler,scaleMassProfile,scaleSurface,scaleTorqueCaps,scaleActiveCoefficients,applyActiveLaw,
    toPhysicalTransforms:(packet,out)=>transformPacket(packet,S,out),
    toRenderTransforms:(packet,out)=>transformPacket(packet,1/S,out),
    // This converts only the display metadata at the existing model-coordinate
    // render bridge. Caller-provided physical SI velocity enters Rapier unchanged.
    toRenderLinearVelocity:renderPoint,
    definition:'Geometry S, area S^2, mass S^3, inertia S^5; stress torque and explicitly authored ACTIVE K,D,cap S^3. Q, angular rate, seconds, density and caller SI velocities unchanged. No passive stiffness or dynamic similarity calibration.'});
}
