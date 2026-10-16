/* Lossless recorded-state packer. This never runs the physics simulation. */
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {resolve,relative} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=fileURLToPath(new URL('../../../',import.meta.url));
export const waterStudySchema='vira-water-study-f64-le-v1';
const assetSHA256='1066a43d881f7baea9ef451a8529c870544043b6a8d06bf905ccb417111ccdf6';
export const waterStudyCases=[
  {id:'original',generated:'hinge',label:'Original: the tail hinge',file:'subtle-wet-turn-timeline.json',sha256:'1ac8aadb8358792cf61f5cdf16de6b085967801113123f4966fead402ea36088'},
  {id:'distributedC',generated:'bounded',label:'Distributed C: bounded loading',file:'subtle-frame-muscle-water-capacity050-range016-timeline.json',sha256:'b329b6689dc49ee0c07c04d837e7de93da4a2d87e5494b068a6a010c888ec111'},
  {id:'source13cm',generated:'source13cm',label:'Source cue: 13 cm koi proxy',file:'subtle-frame-muscle-water-capacity050-range016-clarkfast-moving-source-physical13cm-timeline.json',sha256:'1025ead5e461006e633cde78cae5d3fe4b6ae375457e89ff72976bdc41a1515b'},
];
export const sha256=bytes=>createHash('sha256').update(bytes).digest('hex');
const equal=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
const finite=value=>typeof value==='number'&&Number.isFinite(value);
const fail=message=>{throw new Error(message);};
function tuple(value,size,name){if(!Array.isArray(value)||value.length!==size||!value.every(finite))fail(`Invalid ${name}`);}
export function validateGeneratedProvenance(provenance,expected){
  if(provenance.schemaVersion!==1||provenance.case!==expected.caseName||provenance.rate!==960||provenance.frameRate!==120)fail('Generated case/rate provenance mismatch');
  for(const key of ['timelineSHA256','assetSHA256','massSHA256','inputsSHA256','inputExtractSHA256'])if(provenance[key]!==expected[key])fail(`Generated ${key} mismatch`);
  if(provenance.units?.time!=='s'||provenance.units?.positions!=='m in native render coordinates'||provenance.units?.rotation!=='quaternion xyzw'||provenance.units?.physical13cmSI!=='m')fail('Generated physical units mismatch');
  if(!provenance.moduleSHA256||Object.keys(provenance.moduleSHA256).length<7)fail('Generated solver source hashes missing');
  return provenance;
}
function clean(value){
  if(Array.isArray(value))return value.map(clean);
  if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,clean(v)]));
  if(typeof value==='string')return value.replaceAll('.aquarium-build/clark-turn-shape-fit.json','scripts/aquarium/water-study/inputs.json#payload.observedCue').replaceAll('.aquarium-build/axial-capacity-profile.json','scripts/aquarium/water-study/inputs.json#payload.capacity');
  return value;
}
export function normalizeRecordedRun(input,{id,physicalGeometry}){
  if(input.rate!==960||!Array.isArray(input.frames)||input.frames.length<2)fail('Selected recordings must be actual960Hz runs');
  const frames=input.frames.map((f,index)=>{
    if(!finite(f.time)||index&&f.time<=input.frames[index-1].time)fail('Recorded times must increase');
    if(f.bonePositions?.length!==34||f.boneRotations?.length!==34)fail('All34 physical bone transforms are required');
    f.bonePositions.forEach(v=>tuple(v,3,'bone position'));f.boneRotations.forEach(v=>{tuple(v,4,'bone quaternion');if(Math.abs(Math.hypot(...v)-1)>1e-5)fail('Recorded quaternion is not unit');});
    if(f.bonePositionsMeters){if(f.bonePositionsMeters.length!==34)fail('All34 SI bone positions required');f.bonePositionsMeters.forEach(v=>tuple(v,3,'SI bone position'));}
    const head=f.headQuaternion??f.bodyRotations?.[0];tuple(head,4,'actual physical head quaternion');
    for(const [key,size] of [['b',3],['headOmega',3],['com',3]])tuple(f[key],size,key);
    if(f.renderCOM)tuple(f.renderCOM,3,'render COM');
    if(!finite(f.kineticEnergy)||f.kineticEnergy<0||!finite(f.anchorError)||f.anchorError<0||!finite(f.largestJoint?.angle)||!Number.isInteger(f.largestJoint?.childBone))fail('Missing physical readout');
    const keys=['time','b','bonePositions','boneRotations','headOmega','com','kineticEnergy','anchorError','kickdt','work','renderCOM','bonePositionsMeters'];
    return {...Object.fromEntries(keys.filter(k=>f[k]!==undefined).map(k=>[k,f[k]])),headQuaternion:head,largestJoint:{angle:f.largestJoint.angle,childBone:f.largestJoint.childBone}};
  });
  const header=clean({...input});delete header.frames;delete header.source;
  return {...header,id,physicalScale:header.physicalScale??null,physicalGeometry,frames};
}
export function encodeRecordedRun(run,{label,source}){
  const fields=[{key:'time',width:1},{key:'bonePositions',width:102},{key:'boneRotations',width:136}];
  if(run.frames[0].bonePositionsMeters)fields.push({key:'bonePositionsMeters',width:102});
  const stride=fields.reduce((sum,f)=>sum+f.width,0),poses=Buffer.alloc(run.frames.length*stride*8);let offset=0;
  for(const frame of run.frames)for(const field of fields){
    const values=field.width===1?[frame[field.key]]:frame[field.key].flat();
    if(values.length!==field.width||!values.every(finite))fail('Finite complete Float64 records required');
    for(const value of values){poses.writeDoubleLE(value,offset);if(!Object.is(value,poses.readDoubleLE(offset)))fail('Float64 round trip changed a recorded number');offset+=8;}
  }
  const encoded=new Set(fields.map(f=>f.key)),constantFrameFields={};
  for(const key of ['kickdt'])if(run.frames[0][key]!==undefined&&run.frames.every(f=>equal(f[key],run.frames[0][key])))constantFrameFields[key]=run.frames[0][key];
  const header={...run};delete header.frames;const aliases=[];
  // Hoist exact repeated metadata without changing any retained numeric value.
  if(header.report){header.report={...header.report};for(const key of ['cue','capacity','jointLimits','physicalScale','initialState'])if(header[key]!==undefined&&equal(header.report[key],header[key])){delete header.report[key];aliases.push({key,from:key});}}
  const frameMetadata=run.frames.map(f=>Object.fromEntries(Object.entries(f).filter(([key])=>!encoded.has(key)&&!(key in constantFrameFields))));
  const metadata=Buffer.from(JSON.stringify({schema:waterStudySchema,run:header,constantFrameFields,reportAliases:aliases,frames:frameMetadata})+'\n','utf8');
  const descriptor={id:run.id,label,wip:'Experimental comparison: the body bends; a completed U-turn is still unresolved.',rate:run.rate,
    physicalScale:run.physicalScale,physicalGeometry:run.physicalGeometry,cue:run.cue??null,capacity:run.capacity??null,range:run.range??null,jointLimits:run.jointLimits??null,
    source,frameCount:run.frames.length,firstTime:run.frames[0].time,lastTime:run.frames.at(-1).time,
    poses:{url:`${run.id}.f64`,sha256:sha256(poses),bytes:poses.length,stride,fields},metadata:{url:`${run.id}.json`,sha256:sha256(metadata),bytes:metadata.length}};
  return {descriptor,poses,metadata};
}
async function main(){
  const option=name=>process.argv.find(a=>a.startsWith(`--${name}=`))?.slice(name.length+3),archive=option('archive-dir'),input=option('input-dir');
  if(Boolean(archive)===Boolean(input))fail('Choose --archive-dir=<pinned authoring recordings> OR --input-dir=<generated timelines>');
  const out=resolve(root,option('out')??'static/aquarium/water-study');
  const bytes=await readFile(new URL('../../../static/aquarium/koi.glb',import.meta.url));if(sha256(bytes)!==assetSHA256)fail('Uploaded koi asset differs from these recordings');
  const massBytes=await readFile(new URL('../../../static/aquarium/body-mass.json',import.meta.url)),mass=JSON.parse(massBytes.toString('utf8'));
  if(mass.assetSHA256!==assetSHA256)fail('Mass profile does not describe the uploaded asset');
  const inputBytes=await readFile(new URL('./inputs.json',import.meta.url)),inputs=JSON.parse(inputBytes.toString('utf8'));
  if(sha256(JSON.stringify(inputs.payload))!==inputs.provenance.extractSHA256||sha256(massBytes)!==inputs.provenance.massSHA256||assetSHA256!==inputs.provenance.assetSHA256)fail('Reproducer input/asset/mass integrity mismatch');
  const nativeMass=mass.bodies29.reduce((sum,b)=>sum+b.mass,0),packed=[],recordings=[];
  for(const spec of waterStudyCases){
    const filename=archive?spec.file:`${spec.generated}-960-timeline.json`,path=resolve(root,archive??input,filename),raw=await readFile(path),hash=sha256(raw);
    let provenance;
    if(archive){if(hash!==spec.sha256)fail(`Pinned source hash changed: ${filename}`);}
    else{
      provenance=validateGeneratedProvenance(JSON.parse(await readFile(resolve(root,input,`${spec.generated}-960-provenance.json`),'utf8')),
        {caseName:spec.generated,timelineSHA256:hash,assetSHA256,massSHA256:sha256(massBytes),inputsSHA256:sha256(inputBytes),inputExtractSHA256:inputs.provenance.extractSHA256});
      for(const [path,expected] of Object.entries(provenance.moduleSHA256)){
        if(!/^(scripts\/aquarium\/water-study|static)\/[a-zA-Z0-9._/-]+$/.test(path)||path.split('/').includes('..')||!/^[a-f0-9]{64}$/.test(expected))fail('Invalid generated source descriptor');
        if(sha256(await readFile(resolve(root,path)))!==expected)fail(`Generated solver source changed: ${path}`);
      }
    }
    const parsed=JSON.parse(raw),candidate=parsed.runs?.find(run=>run.rate===960&&(spec.id!=='original'||run.wet===true));if(!candidate)fail(`Actual960Hz case missing: ${filename}`);
    recordings.push({spec,filename,hash,provenance,candidate});
  }
  const nativeLength=recordings.find(r=>r.spec.id==='source13cm').candidate.physicalScale?.nativeLengthM;
  if(!finite(nativeLength)||nativeLength<=0)fail('Source-size recording must identify its measured native material length');
  for(const {spec,filename,hash,provenance,candidate} of recordings){
    const geometry={lengthM:candidate.physicalScale?.physicalLengthM??nativeLength,massKg:candidate.physicalScale?.physicalBodyMassKg??nativeMass,speciesCalibration:false};
    const run=normalizeRecordedRun(candidate,{id:spec.id,physicalGeometry:geometry});
    if(spec.id==='source13cm'&&(!run.physicalScale||run.physicalScale.physicalLengthM!==.13||!run.frames[0].bonePositionsMeters||!run.frames[0].renderCOM))fail('Physical13cm case must preserve SI and native render coordinates');
    const source={file:filename,sha256:hash,precision:'Exact recorded JS numbers stored as little-endian Float64; no rounding or pose interpolation',
      generator:'scripts/aquarium/water-study/generate.mjs',case:spec.generated,units:{time:'s',bonePositions:'native model metres',bonePositionsMeters:spec.id==='source13cm'?'SI metres':null,quaternion:'xyzw'},...(provenance?{generation:provenance}:{})};
    packed.push(encodeRecordedRun(run,{label:spec.label,source}));
  }
  const manifest={schema:waterStudySchema,assetSHA256,recordingRate:120,definition:'Exact recorded physical states, not a real-time water simulation. No normal aquarium behavior is replaced.',
    units:{time:'s',bonePositions:'native model metres',quaternion:'xyzw',com:'SI metres',energy:'J'},
    provenance:{ownCode:'MIT; repository LICENSE',observedData:{authors:['Andrew David Clark','Eric D. Tytell'],title:'Data and Code for Tradeoffs for bluegill sunfish turning at different rates',dataset:'https://doi.org/10.5281/zenodo.19102452',paper:'https://doi.org/10.7717/peerj.21161',license:'CC-BY-4.0',licenseURL:'https://creativecommons.org/licenses/by/4.0/',changes:'Selected fast trial, fitted and retargeted regional shape, quintic temporal interpolation/window, authored koi proxy and13cm uniform scaling; head and COM are outcomes.'}},runs:packed.map(p=>p.descriptor)};
  const manifestBytes=Buffer.from(JSON.stringify(manifest)+'\n','utf8');
  const total=manifestBytes.length+packed.reduce((sum,p)=>sum+p.poses.length+p.metadata.length,0);if(total>3_000_000)fail(`Recorded package exceeds the3MB data budget: ${total}`);
  await mkdir(out,{recursive:true});for(const p of packed){await writeFile(resolve(out,p.descriptor.poses.url),p.poses);await writeFile(resolve(out,p.descriptor.metadata.url),p.metadata);}
  await writeFile(resolve(out,'manifest.json'),manifestBytes);
  for(const p of packed)for(const [key,data] of [['poses',p.poses],['metadata',p.metadata]])if(sha256(await readFile(resolve(out,p.descriptor[key].url)))!==sha256(data))fail('Written data integrity check failed');
  console.log(JSON.stringify({runs:packed.length,bytes:total,out:relative(root,out)}));
}
if(process.argv[1]&&pathToFileURL(resolve(process.argv[1])).href===import.meta.url)await main();
