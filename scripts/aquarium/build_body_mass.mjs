/* Regenerate the compact neutral body-mass profile from the uploaded GLB.
 * Usage: node scripts/aquarium/build_body_mass.mjs [output.json]
 * This offline authoring step never imports the live swimmer or renderer. */
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFile,writeFile} from 'node:fs/promises';
import {resolve} from 'node:path';
import {pathToFileURL} from 'node:url';
import {loadNeutralSkinEnvelope,clipClosedSkinSlab,integrateVolume} from './neutral_skin_volume.mjs';

const defaultAsset=new URL('../../static/aquarium/koi.glb',import.meta.url);
const defaultOutput=new URL('../../static/aquarium/body-mass.json',import.meta.url);
const density=1000;

function combine(items){
  const volume=items.reduce((sum,item)=>sum+item.volume,0),first=[0,0,0],second=Array(9).fill(0);
  for(const item of items){for(let i=0;i<3;i++)first[i]+=item.firstVolumeMoment[i];for(let i=0;i<9;i++)second[i]+=item.secondVolumeMoment[i];}
  const com=first.map(value=>value/volume),central=second.map((value,k)=>value-volume*com[Math.floor(k/3)]*com[k%3]);
  const trace=central[0]+central[4]+central[8],inertia=central.map((value,k)=>density*((k%4===0?trace:0)-value));
  return {volume,mass:volume*density,com,inertia,firstVolumeMoment:first,secondVolumeMoment:second};
}
const compact=({regions,volume,mass,com,inertia})=>({regions,volume,mass,com,inertia});

/** Return the deterministic profile without writing files. assetPath accepts
 * a filesystem path or file URL, so tests can supply their own GLB fixture. */
export async function buildBodyMass({assetPath=defaultAsset}={}){
  const bytes=await readFile(assetPath),{mesh,boneX}=await loadNeutralSkinEnvelope(bytes);
  const whole=integrateVolume(mesh.vertices,mesh.faces),limits=[Infinity,-Infinity],regions=[];
  for(let i=0;i<mesh.vertices.length;i+=3){limits[0]=Math.min(limits[0],mesh.vertices[i]);limits[1]=Math.max(limits[1],mesh.vertices[i]);}
  for(let index=0;index<33;index++){
    const minimum=index===0?limits[0]:boneX[index],maximum=index===32?limits[1]:boneX[index+1];
    const slab=clipClosedSkinSlab(mesh,minimum,maximum);assert.ok(slab,'every body region must contain actual envelope volume');
    const moments=integrateVolume(slab.vertices,slab.faces),I=moments.inertia;
    assert.ok(Number.isFinite(moments.mass)&&moments.volume>0&&moments.com.every(Number.isFinite),'each closed region must have finite positive mass and COM');
    const determinant=I[0]*(I[4]*I[8]-I[5]*I[7])-I[1]*(I[3]*I[8]-I[5]*I[6])+I[2]*(I[3]*I[7]-I[4]*I[6]);
    assert.ok(I.every(Number.isFinite)&&I[0]>0&&I[0]*I[4]-I[1]*I[3]>0&&determinant>0,'region inertia must be finite and positive definite');
    regions.push({index,...moments});
  }
  const sum=combine(regions),maximumError=(a,b)=>Math.max(...a.map((value,i)=>Math.abs(value-b[i])));
  assert.ok(Math.abs(sum.volume-whole.volume)<1e-9&&maximumError(sum.firstVolumeMoment,whole.firstVolumeMoment)<1e-9
    &&maximumError(sum.secondVolumeMoment,whole.secondVolumeMoment)<1e-9&&maximumError(sum.inertia,whole.inertia)<1e-7,
  'the 33-region partition must reproduce the same capped whole-envelope moments');
  const bodies29=[{regions:[0,1,2,3,4],...combine(regions.slice(0,5))},...regions.slice(5).map(region=>({regions:[region.index],...combine([region])}))].map(compact);
  return {
    source:'Exact triangle clipping and signed tetrahedral moments of uploaded-rest Koi_skin envelope; uniform1000kg/m3, no equal vertex masses',
    closure:'Original uploaded skin plus explicit reversed boundary-centroid fan caps at one rostral, four narial, two opercular openings. This envelope proxy is not a biological union of separate facial meshes.',
    density,
    units:'metres, kilograms, kilogram metre squared; inertia row-major XYZ about each region/body COM in neutral fish axes',
    partition:'33 bone-origin X regions; first begins at actual skin minimum -0.66 instead of phantom -0.67; last includes true tapered skin tip to +0.58 instead of stopping at bone33 +0.54',
    assetSHA256:createHash('sha256').update(bytes).digest('hex'),
    bodies29,
  };
}

export async function writeBodyMass(output=defaultOutput,options={}){
  const profile=await buildBodyMass(options);
  await writeFile(output,JSON.stringify(profile,null,2)+'\n','utf8');return profile;
}

if(process.argv[1]&&pathToFileURL(process.argv[1]).href===import.meta.url){
  const output=process.argv[2]?pathToFileURL(resolve(process.argv[2])):defaultOutput;
  const profile=await writeBodyMass(output);
  console.log(`Generated ${profile.bodies29.length} body regions from ${profile.assetSHA256}`);
}
