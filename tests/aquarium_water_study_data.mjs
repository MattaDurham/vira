/* No simulation or private authoring files: verify the public recorded-data
 * contract, byte integrity, SI/native coordinates and actual GLB render join. */
import assert from 'node:assert/strict';
import {readFile,stat} from 'node:fs/promises';
import {createHash,webcrypto} from 'node:crypto';
import {GLTFLoader} from '../static/vendor/GLTFLoader.js';
import {Texture,Vector3,Quaternion} from '../static/vendor/three.webgpu.js';
import {createKoiInstance} from '../static/aquarium-fish-instance.js';
import {createAxialFrame} from '../static/aquarium-axial-frame.js';
import {createRapierRenderBridge} from '../static/aquarium-articulated-render.js';
import {loadWaterStudy,decodeWaterStudyRun} from '../static/aquarium-water-study-data.js';
import {encodeRecordedRun,validateGeneratedProvenance,sha256} from '../scripts/aquarium/water-study/pack.mjs';
import {validateInputs,parseOptions} from '../scripts/aquarium/water-study/generate.mjs';
globalThis.crypto??=webcrypto;
const base=new URL('../static/aquarium/water-study/',import.meta.url),calls=[];
const bytesResponse=bytes=>({ok:true,status:200,json:async()=>JSON.parse(bytes.toString('utf8')),arrayBuffer:async()=>bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength)});
const fetchFile=async url=>{assert.ok(new URL(url).href.startsWith(base.href),'recorded-data test cannot access another store or the network');calls.push(new URL(url).href);return bytesResponse(await readFile(url));};
const {manifest,runs}=await loadWaterStudy(base,{fetch:fetchFile});assert.equal(calls.length,1,'opening the study fetches only its manifest');
assert.deepEqual(runs.map(r=>r.id),['original','distributedC','source13cm']);assert.equal(manifest.recordingRate,120);
const assetBytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url)),profile=JSON.parse(await readFile(new URL('../static/aquarium/body-mass.json',import.meta.url),'utf8'));
assert.equal(createHash('sha256').update(assetBytes).digest('hex'),manifest.assetSHA256,'every saved case belongs to the current actual geometry');
assert.equal(manifest.assetSHA256,profile.assetSHA256);assert.equal(manifest.provenance.observedData.license,'CC-BY-4.0');
let totalBytes=(await stat(new URL('manifest.json',base))).size,frames=0,maximumPositionError=0,maximumRotationError=0,maximumSIConversionError=0;
const source=(await new GLTFLoader().register(()=>({name:'RecordedWaterDataTest',loadTexture:()=>Promise.resolve(new Texture())})).parseAsync(assetBytes.buffer.slice(assetBytes.byteOffset,assetBytes.byteOffset+assetBytes.byteLength),'')).scene;
const rig=createAxialFrame(source),instance=createKoiInstance(source),bridge=createRapierRenderBridge(instance,{sampler:rig});
for(let caseIndex=0;caseIndex<runs.length;caseIndex++){
  const descriptor=runs[caseIndex],pending=descriptor.load();assert.equal(descriptor.load(),pending,'concurrent selections share one in-flight load');const run=await pending;
  assert.equal(await descriptor.load(),run,'successful recorded data is reused');assert.equal(run.frames.length,caseIndex===0?360:361);
  assert.equal(run.frames[0].time,caseIndex===0?1/960:0);assert.equal(run.frames.at(-1).time,descriptor.lastTime);
  assert.deepEqual(run.physicalGeometry,descriptor.physicalGeometry);assert.deepEqual(run.cue??null,descriptor.cue);assert.deepEqual(run.capacity??null,descriptor.capacity);
  assert.ok(!run.frames.some(f=>'dragLinearImpulses'in f||'motorAngularImpulses'in f),'viewer data carries actual poses/readouts without unrelated force arrays');
  assert.equal(descriptor.physicalGeometry.lengthM,caseIndex===2?.13:1.496383046790577);
  if(caseIndex<2)assert.ok(Math.abs(descriptor.physicalGeometry.massKg-profile.bodies29.reduce((s,b)=>s+b.mass,0))<1e-12);
  const poseBytes=await readFile(new URL(descriptor.poses.url,base)),metadataBytes=await readFile(new URL(descriptor.metadata.url,base));totalBytes+=poseBytes.length+metadataBytes.length;
  const repacked=encodeRecordedRun(run,{label:descriptor.label,source:descriptor.source});assert.ok(poseBytes.equals(repacked.poses),'every retained pose/time/physical-SI Float64 bit survives decode and re-encode');
  const view=new DataView(poseBytes.buffer,poseBytes.byteOffset,poseBytes.byteLength);let offset=0;
  for(const frame of run.frames){for(const field of descriptor.poses.fields){const values=field.width===1?[frame[field.key]]:frame[field.key].flat();for(const v of values){assert.ok(Object.is(v,view.getFloat64(offset,true)));offset+=8;}}
    if(caseIndex===2)for(let i=0;i<34;i++)for(let j=0;j<3;j++)maximumSIConversionError=Math.max(maximumSIConversionError,Math.abs(frame.bonePositionsMeters[i][j]-frame.bonePositions[i][j]*run.physicalScale.S));
  }
  const peak=run.frames.reduce((best,f,i)=>f.largestJoint.angle>run.frames[best].largestJoint.angle?i:best,0);
  for(const i of [0,peak,run.frames.length-1]){const f=run.frames[i],result=bridge.pose({positions:f.bonePositions.map(p=>new Vector3(...p)),rotations:f.boneRotations.map(q=>new Quaternion(...q))},{age:f.time,visualFish:{age:f.time},headAngularVelocity:new Vector3(...f.headOmega)});
    maximumPositionError=Math.max(maximumPositionError,result.diagnostics.maximumPositionError);maximumRotationError=Math.max(maximumRotationError,result.diagnostics.maximumRotationError);frames++;
  }
}
assert.equal(calls.length,7,'each case fetches exactly one pose file and one metadata file');assert.ok(totalBytes<3_000_000,'the three public recordings fit the3MB data budget');
assert.ok(maximumPositionError<1e-12&&maximumRotationError<1e-6,'actual rig uses saved physical positions/quaternions without FK projection');assert.ok(maximumSIConversionError<1e-14,'13cm physical positions and normalized native render positions retain coherent units');
bridge.dispose();instance.dispose();

// Integrity errors are rejected before any pose is exposed, and a later good
// fetch can retry rather than inheriting a cached failure.
let corrupt=true;
const brokenFetch=async url=>{const buf=await readFile(url);if(corrupt&&new URL(url).pathname.endsWith('original.f64')){corrupt=false;buf[100]^=1;}return bytesResponse(buf);};
const retry=await loadWaterStudy(base,{fetch:brokenFetch});await assert.rejects(retry.runs[0].load(),/integrity mismatch/);assert.equal((await retry.runs[0].load()).id,'original');
const badManifestFetch=async url=>{const bytes=await readFile(url);if(new URL(url).pathname.endsWith('manifest.json')){const value=JSON.parse(bytes);value.units.com='centimetres';return bytesResponse(Buffer.from(JSON.stringify(value)));}return bytesResponse(bytes);};
await assert.rejects(loadWaterStudy(base,{fetch:badManifestFetch}),/manifest or units/);
const descriptor=runs[0],metadata=JSON.parse(await readFile(new URL(descriptor.metadata.url,base),'utf8')),raw=await readFile(new URL(descriptor.poses.url,base));
const truncated=raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength-8);assert.throws(()=>decodeWaterStudyRun(descriptor,metadata,truncated),/length/);
const nonfinite=raw.buffer.slice(raw.byteOffset,raw.byteOffset+raw.byteLength);new DataView(nonfinite).setFloat64(8,NaN,true);assert.throws(()=>decodeWaterStudyRun(descriptor,metadata,nonfinite),/nonfinite/);
const inputBytes=await readFile(new URL('../scripts/aquarium/water-study/inputs.json',import.meta.url)),inputs=JSON.parse(inputBytes.toString('utf8'));validateInputs(inputs);
assert.equal(inputs.provenance.assetSHA256,manifest.assetSHA256);assert.equal(inputs.provenance.massSHA256,sha256(await readFile(new URL('../static/aquarium/body-mass.json',import.meta.url))));
const changed=JSON.parse(inputBytes);changed.payload.capacity.fraction050[0]*=2;assert.throws(()=>validateInputs(changed),/checksum/);
assert.throws(()=>parseOptions(['--rate=1000']),/Rate/);assert.throws(()=>parseOptions(['--case=unknown']),/Case/);
const proof={schemaVersion:1,case:'bounded',rate:960,frameRate:120,timelineSHA256:'a',assetSHA256:'b',massSHA256:'c',inputsSHA256:'d',inputExtractSHA256:'e',units:{time:'s',positions:'m in native render coordinates',rotation:'quaternion xyzw',physical13cmSI:'m'},moduleSHA256:Object.fromEntries(Array.from({length:7},(_,i)=>['source'+i,'hash']))},expected={...proof,caseName:'bounded'};
validateGeneratedProvenance(proof,expected);assert.throws(()=>validateGeneratedProvenance({...proof,timelineSHA256:'wrong'},expected),/SHA256/);assert.throws(()=>validateGeneratedProvenance({...proof,units:{...proof.units,time:'ms'}},expected),/units/);
console.log(JSON.stringify({cases:runs.length,actualRenderFrames:frames,bytes:totalBytes,maximumPositionError,maximumRotationError,maximumSIConversionError,integrityAndUnitMutations:'rejected'}));
