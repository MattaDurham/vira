/* Single-scattering water, integrated along the actual camera ray to scene
 * depth. The participating medium shares the aquarium's metre coordinates. */
import {PostProcessing} from './vendor/three.webgpu.js';
import {waterAbsorption,waterScattering} from './aquarium-water.js?v=78';
import {waterMin,waterMax} from './aquarium-space.js?v=78';
import {Fn,Loop,pass,screenUV,uniform,
  getViewPosition,vec3,vec4,float,min,max,exp,pow,select} from './vendor/three.tsl.js';

export function waterOptics(renderer,scene,camera){
  const scenePass=pass(scene,camera);
  // Full-screen passes use their own orthographic camera. Bind the actual
  // aquarium camera explicitly, including its changing orbit and projection.
  const origin=uniform(camera.position),cameraFrame=uniform(camera.matrixWorld),projectionInverse=uniform(camera.projectionMatrixInverse);
  const image=scenePass.getTextureNode('output'),depth=scenePass.getTextureNode('depth');
  const absorption=vec3(...waterAbsorption),scattering=vec3(...waterScattering);
  const extinction=absorption.add(scattering);
  const boxMin=vec3(...waterMin),boxMax=vec3(...waterMax);
  const lights=[
    {position:vec3(.1,1.70,.4),radiance:vec3(8,7.44,6.56)},
    {position:vec3(1,1.60,-.8),radiance:vec3(2.5,3.6,4.0)},
  ];
  const inverseDirection=Fn(([direction])=>select(direction.greaterThanEqual(0),vec3(1),vec3(-1)).div(max(direction.abs(),vec3(.00001))));
  const output=Fn(()=>{
    const view=getViewPosition(screenUV,depth.r,projectionInverse);
    const destination=cameraFrame.mul(vec4(view,1)).xyz;
    const direction=destination.sub(origin).normalize();
    const inverse=inverseDirection(direction);
    const t0=boxMin.sub(origin).mul(inverse),t1=boxMax.sub(origin).mul(inverse);
    const entry=min(t0,t1),exit=max(t0,t1);
    const begin=max(max(entry.x,entry.y),max(entry.z,0));
    const finish=min(min(exit.x,exit.y),min(exit.z,destination.sub(origin).length()));
    const length=max(finish.sub(begin),0),step=length.div(12);
    const integral=vec3(0).toVar();
    Loop(12,({i})=>{
      const travelled=float(i).add(.5).mul(step);
      const sample=origin.add(direction.mul(begin.add(travelled)));
      for(const light of lights){
        const towards=light.position.sub(sample),distance=towards.length(),incident=towards.div(distance);
        const lightInverse=inverseDirection(incident);
        const boundary=max(boxMin.sub(sample).mul(lightInverse),boxMax.sub(sample).mul(lightInverse));
        const lightPath=max(min(min(boundary.x,boundary.y),min(boundary.z,distance)),0);
        // Henyey-Greenstein phase function (g=.4): light scatters more strongly
        // forward, so the back rim reveals the volume without a painted beam.
        const cosine=incident.dot(direction);
        const phase=float(.84/(4*Math.PI)).div(pow(float(1.16).sub(cosine.mul(.8)),1.5));
        const incoming=light.radiance.div(distance.mul(distance).add(.25)).mul(exp(extinction.mul(lightPath).negate()));
        const outgoing=exp(extinction.mul(travelled).negate());
        integral.addAssign(scattering.mul(incoming).mul(phase).mul(outgoing).mul(step));
      }
    });
    return vec4(image.rgb.add(integral),image.a);
  })();
  const processing=new PostProcessing(renderer,output);
  // r179's PostProcessing has no dispose method. Its quad material is owned
  // by this instance; the shared full-screen geometry belongs to Three.js.
  return {prepare:()=>processing.renderAsync(),render:()=>processing.render(),dispose:()=>{scenePass.dispose();processing._quadMesh.material.dispose();}};
}
