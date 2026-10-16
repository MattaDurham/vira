/* Recorded experimental states only. This module neither simulates physics nor
 * interpolates/repairs poses. All binary values retain recorded Float64 bits. */
const schema='vira-water-study-f64-le-v1',finite=Number.isFinite;
const tuple=(a,n)=>Array.isArray(a)&&a.length===n&&a.every(finite);
const fail=message=>{throw new Error(`Water study data: ${message}`);};
async function digest(bytes){
  if(!globalThis.crypto?.subtle)fail('SHA256 verification unavailable');
  const value=await globalThis.crypto.subtle.digest('SHA-256',bytes);return Array.from(new Uint8Array(value),v=>v.toString(16).padStart(2,'0')).join('');
}
async function checkedBytes(url,descriptor,fetcher){
  const response=await fetcher(url);if(!response.ok)fail(`unavailable ${url}: HTTP${response.status}`);
  const buffer=await response.arrayBuffer();if(buffer.byteLength!==descriptor.bytes||await digest(buffer)!==descriptor.sha256)fail(`integrity mismatch for ${url}`);return buffer;
}
export function decodeWaterStudyRun(descriptor,metadata,buffer){
  if(metadata.schema!==schema||metadata.run?.id!==descriptor.id||metadata.frames?.length!==descriptor.frameCount)fail('metadata does not match the selected run');
  const fields=descriptor.poses.fields,expected=[['time',1],['bonePositions',102],['boneRotations',136]];
  if(!Array.isArray(fields)||fields.length<3||fields.length>4||expected.some(([k,n],i)=>fields[i]?.key!==k||fields[i]?.width!==n)||fields.length===4&&(fields[3].key!=='bonePositionsMeters'||fields[3].width!==102))fail('invalid pose layout');
  const stride=fields.reduce((sum,f)=>sum+f.width,0);if(stride!==descriptor.poses.stride||buffer.byteLength!==descriptor.frameCount*stride*8)fail('invalid Float64 length');
  const view=new DataView(buffer),frames=[];let offset=0,previous=-Infinity;
  for(let i=0;i<descriptor.frameCount;i++){
    const frame={...metadata.constantFrameFields,...metadata.frames[i]};
    for(const field of fields){const values=[];for(let j=0;j<field.width;j++){const value=view.getFloat64(offset,true);offset+=8;if(!finite(value))fail('nonfinite physical sample');values.push(value);}frame[field.key]=field.width===1?values[0]:Array.from({length:34},(_,j)=>values.slice(j*(field.width===136?4:3),(j+1)*(field.width===136?4:3)));}
    if(frame.time<=previous||!tuple(frame.b,3)||!tuple(frame.com,3)||!tuple(frame.headOmega,3)||!tuple(frame.headQuaternion,4)||frame.renderCOM&&!tuple(frame.renderCOM,3)||!finite(frame.kineticEnergy)||frame.kineticEnergy<0||!finite(frame.anchorError)||frame.anchorError<0||!finite(frame.largestJoint?.angle)||!Number.isInteger(frame.largestJoint?.childBone))fail('invalid physical readout');
    if(frame.boneRotations.some(q=>Math.abs(Math.hypot(...q)-1)>1e-5))fail('nonunit physical quaternion');previous=frame.time;frames.push(frame);
  }
  if(frames[0].time!==descriptor.firstTime||frames.at(-1).time!==descriptor.lastTime)fail('recorded bounds mismatch');
  const run={...metadata.run,frames};if(run.report){run.report={...run.report};for(const a of metadata.reportAliases??[])run.report[a.key]=run[a.from];}
  return run;
}
export async function loadWaterStudy(base=new URL('./aquarium/water-study/',import.meta.url),{fetch:fetcher=globalThis.fetch}={}){
  if(typeof fetcher!=='function')fail('fetch unavailable');
  const baseURL=new URL(base,globalThis.location?.href??import.meta.url),response=await fetcher(new URL('manifest.json',baseURL));if(!response.ok)fail(`manifest unavailable: HTTP${response.status}`);
  const manifest=await response.json();
  if(manifest.schema!==schema||manifest.units?.time!=='s'||manifest.units?.quaternion!=='xyzw'||manifest.units?.com!=='SI metres'||manifest.units?.bonePositions!=='native model metres'||!Array.isArray(manifest.runs)||manifest.runs.length!==3)fail('unsupported manifest or units');
  const ids=new Set();const runs=manifest.runs.map(descriptor=>{
    if(ids.has(descriptor.id)||!['original','distributedC','source13cm'].includes(descriptor.id)||!Number.isInteger(descriptor.frameCount)||descriptor.frameCount<2||!finite(descriptor.physicalGeometry?.lengthM)||!(descriptor.physicalGeometry.lengthM>0)||!finite(descriptor.physicalGeometry?.massKg)||!(descriptor.physicalGeometry.massKg>0))fail('invalid run descriptor');ids.add(descriptor.id);
    for(const key of ['poses','metadata'])if(!/^[a-zA-Z0-9][a-zA-Z0-9.-]*\.(f64|json)$/.test(descriptor[key]?.url??'')||descriptor[key].url.includes('..')||!/^[a-f0-9]{64}$/.test(descriptor[key]?.sha256??'')||!Number.isInteger(descriptor[key]?.bytes)||descriptor[key].bytes<=0)fail('invalid resource descriptor');
    let pending;
    return {...descriptor,load(){
      if(!pending)pending=(async()=>{
        const [poses,metadata]=await Promise.all([checkedBytes(new URL(descriptor.poses.url,baseURL),descriptor.poses,fetcher),checkedBytes(new URL(descriptor.metadata.url,baseURL),descriptor.metadata,fetcher)]);
        return decodeWaterStudyRun(descriptor,JSON.parse(new TextDecoder().decode(metadata)),poses);
      })().catch(error=>{pending=undefined;throw error;});
      return pending;
    }};
  });
  return {manifest,runs};
}
