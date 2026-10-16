/* A moving planar water interface, with dielectric reflection and TIR. */
import * as THREE from './vendor/three.webgpu.js';
import {texture,screenUV,vec2,vec3,vec4,cameraPosition,positionWorld,normalWorld,
  abs,min,max,step,select,exp} from './vendor/three.tsl.js';
import {waterMin,waterMax} from './aquarium-space.js?v=82';

export const waterAbsorption=Object.freeze([.26,.085,.065]);
export const waterScattering=Object.freeze([.004,.008,.010]);
export const waterExtinction=Object.freeze(waterAbsorption.map((v,i)=>v+waterScattering[i]));

export function dielectricReflectance(cosine,from,to){
  if(from===to)return 0;
  const c=Math.max(0,Math.min(1,cosine)),ratio=from/to;
  const transmitted=1-ratio*ratio*(1-c*c);
  if(transmitted<=0)return 1;
  const t=Math.sqrt(transmitted);
  const s=(from*c-to*t)/(from*c+to*t);
  const p=(from*t-to*c)/(from*t+to*c);
  return (s*s+p*p)*.5;
}

export function fresnelTable(size=1024){
  const pixels=new Uint8Array(size*4);
  for(let i=0;i<size;i++){
    const cosine=i/(size-1);
    pixels[i*4]=Math.round(255*dielectricReflectance(cosine,1,1.333));
    pixels[i*4+1]=Math.round(255*dielectricReflectance(cosine,1.333,1));
    pixels[i*4+3]=255;
  }
  return pixels;
}

const direction=new THREE.Vector3(),plane=new THREE.Plane(),clip=new THREE.Vector4(),corner=new THREE.Vector4();
export function updateReflectionCamera(source,mirror,height=waterMax[1]){
  source.updateMatrixWorld();source.getWorldDirection(direction);direction.y=-direction.y;
  mirror.position.copy(source.position);mirror.position.y=2*height-source.position.y;
  mirror.up.copy(source.up);mirror.up.y=-mirror.up.y;
  mirror.lookAt(direction.add(mirror.position));mirror.near=source.near;mirror.far=source.far;
  mirror.coordinateSystem=source.coordinateSystem;mirror.updateMatrixWorld();
  mirror.projectionMatrix.copy(source.projectionMatrix);
  // Retain only the observer's side of the interface. The oblique near plane
  // also keeps objects on the other side from leaking into the reflection.
  plane.set(new THREE.Vector3(0,source.position.y<height?-1:1,0),0);
  plane.constant=-plane.normal.y*height;plane.applyMatrix4(mirror.matrixWorldInverse);
  clip.set(plane.normal.x,plane.normal.y,plane.normal.z,plane.constant);
  const p=mirror.projectionMatrix.elements;
  corner.set((Math.sign(clip.x)+p[8])/p[0],(Math.sign(clip.y)+p[9])/p[5],-1,(1+p[10])/p[14]);
  clip.multiplyScalar(1/clip.dot(corner));
  p[2]=clip.x;p[6]=clip.y;p[10]=clip.z+(source.coordinateSystem===THREE.WebGPUCoordinateSystem?0:1);p[14]=clip.w;
  mirror.projectionMatrixInverse.copy(mirror.projectionMatrix).invert();
  return mirror;
}

export function createWaterInterface(scene,camera){
  // Reflections use quarter the native dimensions. The fish itself remains native
  // resolution; this bounds the cost of the second animated scene render.
  const target=new THREE.RenderTarget(1,1,{type:THREE.HalfFloatType});
  const mirror=camera.clone();mirror.layers.set(1);
  const dimensions=new THREE.Vector2();
  const frustum=new THREE.Frustum(),viewProjection=new THREE.Matrix4();
  const surfaceBounds=new THREE.Box3(new THREE.Vector3(waterMin[0],waterMax[1]-.003,waterMin[2]),
    new THREE.Vector3(waterMax[0],waterMax[1]+.003,waterMax[2]));
  const previousEye=new THREE.Vector3(),previousProjection=new THREE.Matrix4();
  let refresh=0;
  let shadows;
  const lookup=new THREE.DataTexture(fresnelTable(),1024,1,THREE.RGBAFormat);
  lookup.minFilter=lookup.magFilter=THREE.LinearFilter;lookup.needsUpdate=true;
  const ray=positionWorld.sub(cameraPosition);
  const cosine=abs(normalWorld.dot(ray.normalize()));
  const f=texture(lookup,vec2(cosine,.5));
  const reflectance=select(cameraPosition.y.lessThan(waterMax[1]),f.g,f.r);
  const reflected=texture(target.texture,screenUV.flipX().add(normalWorld.xz.mul(.035)));
  // Extinction on the observer-to-interface leg. The mirrored scene's tissue
  // shaders handle its interface-to-fish leg; volume scattering is added later.
  const safeRay=max(abs(ray),vec3(.000001)).mul(step(vec3(0),ray).mul(2).sub(1));
  const a=vec3(...waterMin).sub(cameraPosition).div(safeRay);
  const b=vec3(...waterMax).sub(cameraPosition).div(safeRay);
  const enter=min(a,b),leave=max(a,b);
  const begin=max(max(enter.x,enter.y),max(enter.z,0));
  const finish=min(min(leave.x,leave.y),min(leave.z,1));
  const path=max(finish.sub(begin),0).mul(ray.length());
  const attenuation=exp(vec3(...waterExtinction).mul(path).negate());
  const material=new THREE.MeshBasicNodeMaterial({transparent:true,depthWrite:false,side:THREE.DoubleSide});
  // Normal alpha blending applies R to the reflected radiance and 1-R to the
  // already-rendered underwater scene, including its transparent membranes.
  material.fragmentNode=vec4(reflected.rgb.mul(attenuation),reflectance);
  function update(renderer){
    if(!renderer)return;
    camera.updateMatrixWorld();
    viewProjection.multiplyMatrices(camera.projectionMatrix,camera.matrixWorldInverse);
    frustum.setFromProjectionMatrix(viewProjection,camera.coordinateSystem);
    if(!frustum.intersectsBox(surfaceBounds))return;
    // The small, water-distorted mirror refreshes every third live frame;
    // swimming and surface normals still update on every main-view frame.
    // Camera changes bypass this cadence so orbiting never shows a stale view.
    if(++refresh%3!==1&&previousEye.distanceToSquared(camera.position)<1e-6&&previousProjection.equals(camera.projectionMatrix))return;
    previousEye.copy(camera.position);previousProjection.copy(camera.projectionMatrix);
    renderer.getDrawingBufferSize(dimensions);
    target.setSize(Math.ceil(dimensions.x*.25),Math.ceil(dimensions.y*.25));
    updateReflectionCamera(camera,mirror);
    const previous=renderer.getRenderTarget();
    shadows??=(()=>{const items=[];scene.traverse(o=>{if(o.isLight&&o.castShadow)items.push(o.shadow);});return items;})();
    // Reuse the preceding main-view shadow maps. Rendering each shadow again
    // for a small reflection texture needlessly doubles the depth passes.
    const saved=shadows.map(shadow=>({shadow,auto:shadow.autoUpdate,needs:shadow.needsUpdate}));
    saved.forEach(({shadow})=>{shadow.autoUpdate=shadow.needsUpdate=false;});
    try{renderer.setRenderTarget(target);renderer.render(scene,mirror);}
    finally{
      renderer.setRenderTarget(previous);
      saved.forEach(({shadow,auto,needs})=>{shadow.autoUpdate=auto;shadow.needsUpdate=needs;});
    }
  }
  return {material,update,dispose:()=>{target.dispose();lookup.dispose();}};
}
