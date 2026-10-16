/* Inspect actual exported, neutral Float32 skin palettes. These conformation
 * requirements are authored design intent, not measured koi morphometrics.
 * Fin attachments and full live poses have separate actual-asset tests. */
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {dirname,join,resolve} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Matrix4,Quaternion} from '../static/vendor/three.webgpu.js';

const repository=fileURLToPath(new URL('..',import.meta.url));
const defaultAsset=join(repository,'static/aquarium/refined/koi.glb');
const defaultBaseline=join(repository,'static/aquarium/koi.glb');
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const parse=async bytes=>(await new GLTFLoader().register(()=>({name:'RefinedQualityTextureStub',loadTexture:()=>Promise.resolve(new Texture())}))
  .parseAsync(bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength),'')).scene;

function uploadedSkin(asset){
  asset.updateMatrixWorld(true);const mesh=asset.getObjectByName('Koi_skin');
  assert.ok(mesh?.isSkinnedMesh,'the actual exported skin is required');mesh.skeleton.update();
  const a=mesh.geometry.attributes,palette=mesh.skeleton.boneMatrices;
  const post=new Matrix4().copy(asset.matrixWorld).invert().multiply(mesh.matrixWorld).multiply(mesh.bindMatrixInverse);
  const bind=new Vector3(),point=new Vector3(),positions=[];
  for(let i=0;i<a.position.count;i++){
    bind.fromBufferAttribute(a.position,i).applyMatrix4(mesh.bindMatrix);point.set(0,0,0);
    for(let slot=0;slot<4;slot++){
      const weight=a.skinWeight.getComponent(i,slot);if(!weight)continue;
      const p=a.skinIndex.getComponent(i,slot)*16;
      point.x+=weight*(palette[p]*bind.x+palette[p+4]*bind.y+palette[p+8]*bind.z+palette[p+12]);
      point.y+=weight*(palette[p+1]*bind.x+palette[p+5]*bind.y+palette[p+9]*bind.z+palette[p+13]);
      point.z+=weight*(palette[p+2]*bind.x+palette[p+6]*bind.y+palette[p+10]*bind.z+palette[p+14]);
    }
    point.applyMatrix4(post);assert.ok(point.toArray().every(Number.isFinite),'all actually uploaded body vertices must remain finite');
    positions.push(point.toArray());
  }
  return positions;
}

function ringProfile(positions){
  const rings=[];
  for(const [x,y,z] of [...positions].sort((a,b)=>a[0]-b[0])){
    let ring=rings.at(-1);
    if(!ring||x-ring.firstX>1e-6){ring={firstX:x,x:0,count:0,ymin:Infinity,ymax:-Infinity,zmin:Infinity,zmax:-Infinity};rings.push(ring);}
    ring.x+=x;ring.count++;ring.ymin=Math.min(ring.ymin,y);ring.ymax=Math.max(ring.ymax,y);ring.zmin=Math.min(ring.zmin,z);ring.zmax=Math.max(ring.zmax,z);
  }
  return rings.map(r=>({...r,x:r.x/r.count,width:(r.zmax-r.zmin)/2}));
}
function widthAt(rings,x){
  assert.ok(x>=rings[0].x&&x<=rings.at(-1).x,'width sample belongs to the actual flesh interval');
  for(let i=0;i<rings.length-1;i++)if(x<=rings[i+1].x){const a=rings[i],b=rings[i+1],t=(x-a.x)/(b.x-a.x);return a.width+t*(b.width-a.width);}
  return rings.at(-1).width;
}
function lineDeviation(rings,first=-.13,last=.37){
  const a=widthAt(rings,first),b=widthAt(rings,last);let maximum=0;
  for(const r of rings)if(r.x>=first&&r.x<=last){const line=a+(b-a)*(r.x-first)/(last-first);maximum=Math.max(maximum,Math.abs(r.width-line));}
  return maximum;
}
function preservedDistance(a,b){
  const cell=1e-6,bins=new Map();
  for(const p of b){const key=p.map(x=>Math.floor(x/cell)).join(',');if(!bins.has(key))bins.set(key,[]);bins.get(key).push(p);}
  let maximum=0;
  for(const p of a){
    const c=p.map(x=>Math.floor(x/cell));let nearest=Infinity;
    for(let dx=-1;dx<=1;dx++)for(let dy=-1;dy<=1;dy++)for(let dz=-1;dz<=1;dz++)for(const q of bins.get([c[0]+dx,c[1]+dy,c[2]+dz].join(','))??[])
      nearest=Math.min(nearest,Math.hypot(p[0]-q[0],p[1]-q[1],p[2]-q[2]));
    maximum=Math.max(maximum,nearest);
  }
  return maximum;
}

export function assertRefinedConformation(asset,originalAsset){
  const original=uploadedSkin(originalAsset),candidate=uploadedSkin(asset),old=ringProfile(original),refined=ringProfile(candidate);
  assert.equal(refined.length,old.length,'UV splitting cannot change or remove the original neutral flesh stations');
  let maximumVerticalError=0,maximumStationError=0,worstVerticalRing=null;
  for(let i=0;i<old.length;i++){
    maximumStationError=Math.max(maximumStationError,Math.abs(refined[i].x-old[i].x));
    const verticalError=Math.max(Math.abs(refined[i].ymin-old[i].ymin),Math.abs(refined[i].ymax-old[i].ymax));
    if(verticalError>maximumVerticalError){maximumVerticalError=verticalError;worstVerticalRing={original:old[i],refined:refined[i]};}
  }
  assert.ok(maximumStationError<2e-7,'longitudinal material stations remain unchanged in the real export');
  assert.ok(maximumVerticalError<5e-7,`all actual vertical dorsal/ventral ring extrema retain the original profile: ${maximumVerticalError}, ${JSON.stringify(worstVerticalRing)}`);
  const oldPeak=old.reduce((a,b)=>a.width>b.width?a:b),newPeak=refined.reduce((a,b)=>a.width>b.width?a:b),length=refined.at(-1).x-refined[0].x;
  const oldPeakFraction=(oldPeak.x-old[0].x)/(old.at(-1).x-old[0].x),newPeakFraction=(newPeak.x-refined[0].x)/length;
  assert.ok(oldPeakFraction>.40,'the immutable baseline exposes its central side-derived ellipse');
  assert.ok(newPeakFraction>.24&&newPeakFraction<.35,'the actual new overhead maximum belongs to the authored anterior shoulder');
  assert.ok(newPeak.width>.14&&newPeak.width<.16,'the shoulder remains inside the intended native-size body envelope');
  const posterior=refined.filter(r=>r.x>=newPeak.x+.005&&r.x<=.454);
  let maximumPosteriorIncrease=0;
  for(let i=1;i<posterior.length;i++)maximumPosteriorIncrease=Math.max(maximumPosteriorIncrease,posterior[i].width-posterior[i-1].width);
  assert.ok(maximumPosteriorIncrease<5e-7,'actual posterior body width must taper without a second exported bulge');
  assert.ok(widthAt(refined,-.37)>widthAt(old,-.37)*1.10,'new skin really carries broader anterior shoulders');
  const originalEyePlaneWidth=widthAt(old,-.534),refinedEyePlaneWidth=widthAt(refined,-.534);
  assert.ok(refinedEyePlaneWidth>.100&&refinedEyePlaneWidth>originalEyePlaneWidth*1.25,
    'the actual cranial variant broadens the forehead at the eye plane instead of retaining the needle-shaped head');
  assert.ok(widthAt(refined,.26)<widthAt(old,.26)*.80,'the long posterior taper replaces the central ellipse');
  const oldLineDeviation=lineDeviation(old),newLineDeviation=lineDeviation(refined);
  assert.ok(newLineDeviation<.006&&newLineDeviation<oldLineDeviation*.5,'the actual posterior silhouette has a sustained gradual taper rather than a late pinch');
  const rostral=p=>p[0]<=-.655,caudal=p=>p[0]>=.454;
  const oldHead=original.filter(rostral),newHead=candidate.filter(rostral),oldTail=original.filter(caudal),newTail=candidate.filter(caudal);
  const maximumRostralPositionError=Math.max(preservedDistance(oldHead,newHead),preservedDistance(newHead,oldHead));
  const maximumCaudalPositionError=Math.max(preservedDistance(oldTail,newTail),preservedDistance(newTail,oldTail));
  assert.ok(maximumRostralPositionError<5e-7,'both exports retain nose/mouth material points through x=-.655, despite UV vertex splitting');
  assert.ok(maximumCaudalPositionError<5e-7,'both exports retain the actual caudal flesh/root interval from x=.454');
  let maximumAxialPositionError=0,maximumAxialOrientationError=0;
  const inv=new Matrix4().copy(asset.matrixWorld).invert(),oldInv=new Matrix4().copy(originalAsset.matrixWorld).invert();
  for(let i=0;i<34;i++){
    const name=`spine_${String(i).padStart(2,'0')}`,a=asset.getObjectByName(name),b=originalAsset.getObjectByName(name);assert.ok(a?.isBone&&b?.isBone,'all 34 actual axial joints remain present');
    const p=a.getWorldPosition(new Vector3()).applyMatrix4(inv),q=b.getWorldPosition(new Vector3()).applyMatrix4(oldInv);
    maximumAxialPositionError=Math.max(maximumAxialPositionError,p.distanceTo(q));
    const A=a.getWorldQuaternion(new Quaternion()).normalize(),B=b.getWorldQuaternion(new Quaternion()).normalize(),delta=A.invert().multiply(B).normalize();
    maximumAxialOrientationError=Math.max(maximumAxialOrientationError,2*Math.atan2(Math.hypot(delta.x,delta.y,delta.z),Math.abs(delta.w)));
    if(i>0)assert.equal(a.parent.name,`spine_${String(i-1).padStart(2,'0')}`,'the real axial hierarchy remains consecutive');
  }
  assert.ok(maximumAxialPositionError<2e-7&&maximumAxialOrientationError<1e-6,`width authoring cannot alter the actual neutral skeleton: position=${maximumAxialPositionError}, angle=${maximumAxialOrientationError}`);
  return {originalVertices:original.length,candidateVertices:candidate.length,stations:refined.length,oldPeakFraction,newPeakFraction,
    newMaximumHalfWidth:newPeak.width,originalEyePlaneWidth,refinedEyePlaneWidth,oldLineDeviation,newLineDeviation,maximumPosteriorIncrease,maximumStationError,maximumVerticalError,
    preservedRostralVertices:[oldHead.length,newHead.length],preservedCaudalVertices:[oldTail.length,newTail.length],
    maximumRostralPositionError,maximumCaudalPositionError,maximumAxialPositionError,maximumAxialOrientationError};
}

export async function assertRefinedQuality({assetPath=defaultAsset,baselinePath=defaultBaseline,manifestPath=join(dirname(assetPath),'manifest.json')}={}){
  const [bytes,originalBytes,manifest]=await Promise.all([readFile(assetPath),readFile(baselinePath),readFile(manifestPath,'utf8').then(JSON.parse)]);
  const [asset,originalAsset]=await Promise.all([parse(bytes),parse(originalBytes)]);
  assert.equal(manifest.geometry_sha256,sha(bytes),'metadata must describe the actual tested refined export');
  assert.notEqual(sha(bytes),sha(originalBytes),'the new variant is distinct from the immutable original');
  const conformation=assertRefinedConformation(asset,originalAsset);
  const {assertRefinedTexture}=await import('./aquarium_refined_texture.mjs');
  const texture=await assertRefinedTexture({bytes,asset,originalBytes,originalAsset,assetPath,manifest});
  return {assetSHA256:sha(bytes),originalSHA256:sha(originalBytes),conformation,texture,
    scope:'Actual exported neutral skin, UV/PNG and material metadata; conformation is authored design intent, not a measured biological standard.'};
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  const args=process.argv.slice(2),option=(name,fallback)=>{const i=args.indexOf(name);return i<0?fallback:resolve(args[i+1]);};
  const assetPath=option('--asset',args[0]&&!args[0].startsWith('--')?resolve(args[0]):defaultAsset);
  console.log(JSON.stringify(await assertRefinedQuality({assetPath,baselinePath:option('--baseline',defaultBaseline),manifestPath:option('--manifest',join(dirname(assetPath),'manifest.json'))})));
}
