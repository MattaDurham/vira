// Inspect exported UVs, embedded pixels and PBR bindings together. This does
// not infer a rendered appearance from authoring helper/source text.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {deflateSync,inflateSync} from 'node:zlib';
import {NoColorSpace,SRGBColorSpace,Vector3} from '../static/vendor/three.webgpu.js';

const near=(a,b,tolerance=1e-6)=>Math.abs(a-b)<=tolerance;
const hash=data=>createHash('sha256').update(data).digest('hex');
const crcTable=Uint32Array.from({length:256},(_,i)=>{
  let value=i;for(let bit=0;bit<8;bit++)value=(value&1)?0xedb88320^(value>>>1):value>>>1;
  return value>>>0;
});
const crc32=bytes=>{let crc=0xffffffff;for(const byte of bytes)crc=crcTable[(crc^byte)&255]^(crc>>>8);return (crc^0xffffffff)>>>0;};
const paeth=(left,up,corner)=>{
  const estimate=left+up-corner,a=Math.abs(estimate-left),b=Math.abs(estimate-up),c=Math.abs(estimate-corner);
  return a<=b&&a<=c?left:b<=c?up:corner;
};
const prediction=(filter,left,up,corner)=>filter===0?0:filter===1?left:filter===2?up:filter===3?Math.floor((left+up)/2):paeth(left,up,corner);

export function decodeAtlasPNG(input){
  const bytes=Buffer.isBuffer(input)?input:Buffer.from(input);
  assert.equal(bytes.subarray(0,8).toString('hex'),'89504e470d0a1a0a','embedded atlas is a PNG');
  let offset=8,header=null,ended=false;const blocks=[];
  while(offset<bytes.length){
    assert.ok(offset+12<=bytes.length,'PNG chunk header fits its buffer');
    const length=bytes.readUInt32BE(offset),end=offset+12+length;
    assert.ok(end<=bytes.length,'PNG chunk payload fits its buffer');
    const kind=bytes.toString('ascii',offset+4,offset+8),payload=bytes.subarray(offset+8,offset+8+length);
    assert.equal(crc32(bytes.subarray(offset+4,offset+8+length)),bytes.readUInt32BE(offset+8+length),'PNG CRC matches actual encoded bytes');
    if(kind==='IHDR'){
      assert.equal(header,null,'PNG has one header');assert.equal(length,13);
      const width=payload.readUInt32BE(0),height=payload.readUInt32BE(4),bits=payload[8],color=payload[9];
      assert.ok(width>0&&height>0&&width*height<=16_777_216,'atlas fits the bounded test decoder');
      assert.ok(bits===8&&(color===2||color===6)&&payload[10]===0&&payload[11]===0&&payload[12]===0,
        'atlas uses noninterlaced 8-bit RGB or RGBA');
      header={width,height,channels:color===2?3:4};
    }else if(kind==='IDAT'){assert.ok(header,'IHDR precedes pixel data');blocks.push(payload);}
    else if(kind==='IEND'){assert.equal(length,0);ended=true;offset=end;break;}
    offset=end;
  }
  assert.ok(header&&ended&&blocks.length,'PNG contains header, pixels and end marker');
  assert.equal(offset,bytes.length,'PNG ends after IEND');
  const {width,height,channels}=header,stride=width*channels;
  const raw=inflateSync(Buffer.concat(blocks),{maxOutputLength:height*(stride+1)});
  assert.equal(raw.length,height*(stride+1),'PNG has exactly one full filtered row per scanline');
  const pixels=new Uint8Array(width*height*channels);
  for(let y=0;y<height;y++){
    const inputRow=y*(stride+1),outputRow=y*stride,filter=raw[inputRow];
    assert.ok(filter<=4,'PNG uses a standard byte filter');
    for(let i=0;i<stride;i++){
      const left=i>=channels?pixels[outputRow+i-channels]:0,up=y?pixels[outputRow+i-stride]:0;
      const corner=y&&i>=channels?pixels[outputRow+i-stride-channels]:0;
      pixels[outputRow+i]=(raw[inputRow+1+i]+prediction(filter,left,up,corner))&255;
    }
  }
  return {...header,pixels};
}

function chunk(kind,payload){
  const tag=Buffer.from(kind,'ascii'),result=Buffer.alloc(payload.length+12);
  result.writeUInt32BE(payload.length);tag.copy(result,4);payload.copy(result,8);
  result.writeUInt32BE(crc32(Buffer.concat([tag,payload])),payload.length+8);return result;
}

export function assertAtlasPNGDecoder(){
  // Each fixture exercises every PNG filter, multiple IDAT blocks, RGB/RGBA,
  // byte overflow and nonconstant neighbors, independently of Blender output.
  for(const channels of [3,4]){
    const width=3,height=5;
    const known=channels===3?[
      '135699dc1f62a5074a8dd0135699fb3e81c4074a8def3275b8fb3e81e32669acef3275d71a5da0e32669cb0e51',
      '00135699dc1f62a5074a018dd013c9c9e8e8e8c9027a7a7a99997a7a7a7a037ebee0b122a2a2a24104997a7ac9c97ae8e8e8'
    ]:[
      '135699dc1f62a5074a8dd0135699fb3e81c4074a8def3275b8fb3e81e32669acef3275d71a5da0e32669cb0e5194d71a5dbf024588cb0e51b3f6397c',
      '00135699dc1f62a5074a8dd013015699fb3e2b2b0c0c0c2b2b2b0262624343626262626243436203bee081a3a8284747c74737a804436262622b0c0c432b2b2b2b'
    ];
    // Frozen filtered bytes make the decoder oracle independent of its own
    // prediction implementation, including Paeth and average tie behavior.
    const pixels=new Uint8Array(Buffer.from(known[0],'hex')),raw=Buffer.from(known[1],'hex');
    const header=Buffer.alloc(13);header.writeUInt32BE(width);header.writeUInt32BE(height,4);header[8]=8;header[9]=channels===3?2:6;
    const compressed=deflateSync(raw),middle=Math.floor(compressed.length/2);
    const png=Buffer.concat([Buffer.from('89504e470d0a1a0a','hex'),chunk('IHDR',header),chunk('IDAT',compressed.subarray(0,middle)),chunk('IDAT',compressed.subarray(middle)),chunk('IEND',Buffer.alloc(0))]);
    assert.deepEqual(decodeAtlasPNG(png),{width,height,channels,pixels});
    const damaged=Buffer.from(png);damaged[damaged.length-5]^=1;
    assert.throws(()=>decodeAtlasPNG(damaged),/CRC/,'decoder cannot silently accept corrupt pixels');
  }
  const alpha=new Uint8Array(3*3*4).fill(200);for(let i=3;i<alpha.length;i+=4)alpha[i]=255;
  pixelStats({width:3,height:3,channels:4,pixels:alpha});alpha[(1*3+1)*4+3]=254;
  assert.throws(()=>pixelStats({width:3,height:3,channels:4,pixels:alpha}),/opaque/,'a single interior translucent texel is rejected');
  const periodic={width:16,height:32,channels:3,pixels:new Uint8Array(16*32*3)};
  for(let y=0;y<32;y++)for(let x=0;x<16;x++)for(let c=0;c<3;c++)periodic.pixels[(y*16+x)*3+c]=200+Math.round(Math.sin(2*Math.PI*y/31));
  seamMips(periodic,[0,1,2],true);
  const darkRing={...periodic,pixels:periodic.pixels.slice()};darkRing.pixels.fill(0,16*3,32*3);
  assert.throws(()=>seamMips(darkRing,[0,1,2],true),/gutter/,'equal endpoint rows cannot hide a dark adjacent mip ring');
  assertAlbedoPixels({width:1,height:1,channels:3,pixels:Uint8Array.of(200,0,0)});
  assert.throws(()=>assertAlbedoPixels({width:1,height:1,channels:3,pixels:Uint8Array.of(5,3,1)}),/black cutout/,
    'a saturated pigment is valid, while an entirely near-black RGB texel is rejected');
  return {filters:5,formats:['RGB','RGBA'],splitIDAT:true,crcGuard:true,interiorAlphaGuard:true,mipRingGuard:true,albedoBlackGuard:true};
}

function parseGLB(input){
  const bytes=Buffer.isBuffer(input)?input:Buffer.from(input);
  assert.equal(bytes.toString('ascii',0,4),'glTF');assert.equal(bytes.readUInt32LE(4),2);assert.equal(bytes.readUInt32LE(8),bytes.length);
  let offset=12,json,bin;
  while(offset<bytes.length){
    assert.ok(offset+8<=bytes.length);const length=bytes.readUInt32LE(offset),kind=bytes.readUInt32LE(offset+4);offset+=8;
    assert.ok(offset+length<=bytes.length);const data=bytes.subarray(offset,offset+length);
    if(kind===0x4e4f534a)json=JSON.parse(data.toString('utf8').replace(/[\s\0]+$/u,''));
    if(kind===0x004e4942)bin=data;offset+=length;
  }
  assert.ok(json&&bin,'GLB contains actual JSON and binary chunks');return {json,bin};
}

function embeddedTexture(glb,index){
  const texture=glb.json.textures?.[index],image=glb.json.images?.[texture?.source];
  assert.ok(texture&&image&&image.mimeType==='image/png'&&Number.isInteger(image.bufferView)&&!image.uri,'skin image is embedded PNG, with no external path');
  const view=glb.json.bufferViews[image.bufferView],start=view.byteOffset??0;
  assert.ok((view.buffer??0)===0&&start+view.byteLength<=glb.bin.length,'embedded PNG fits GLB BIN');
  const sampler=glb.json.samplers?.[texture.sampler]??{};
  assert.equal(sampler.wrapT??10497,10497,'circumferential V repeats over the ventral seam');
  assert.ok([9984,9985,9986,9987].includes(sampler.minFilter),'the actual skin sampler uses mipmaps');
  const bytes=glb.bin.subarray(start,start+view.byteLength);
  return {bytes,image:decodeAtlasPNG(bytes),sampler,name:image.name};
}

function pixelStats(image){
  const min=[255,255,255,255],max=[0,0,0,0];let red=0,white=0,minimumRGBPeak=255;
  for(let i=0;i<image.pixels.length;i+=image.channels){
    for(let c=0;c<image.channels;c++){const value=image.pixels[i+c];min[c]=Math.min(min[c],value);max[c]=Math.max(max[c],value);}
    const r=image.pixels[i],g=image.pixels[i+1],b=image.pixels[i+2];minimumRGBPeak=Math.min(minimumRGBPeak,Math.max(r,g,b));
    if(r>150&&g<100&&b<70)red++;if(Math.min(r,g,b)>178)white++;
  }
  if(image.channels===4)assert.equal(min[3],255,'every actual skin texel is opaque, including mip source gutters');
  return {min:min.slice(0,image.channels),max:max.slice(0,image.channels),red,white,minimumRGBPeak,count:image.width*image.height};
}

function assertAlbedoPixels(image){
  const stats=pixelStats(image);
  assert.ok(stats.minimumRGBPeak>6,'opaque albedo has no undefined black cutout RGB');
  return stats;
}

function seamMips(image,channels,white=false){
  // Four levels of real 2x2 footprints, using just the top/bottom 16-row
  // bands. There is no need to average millions of unrelated interior texels.
  let width=image.width,height=16;
  let bands=[0,image.height-height].map(first=>{
    const pixels=new Float32Array(width*height*channels.length);
    for(let y=0;y<height;y++)for(let x=0;x<width;x++)for(let c=0;c<channels.length;c++)
      pixels[(y*width+x)*channels.length+c]=image.pixels[((first+y)*image.width+x)*image.channels+channels[c]];
    return pixels;
  });
  const metrics=[];
  for(let level=0;level<=4;level++){
    let delta=0,minimum=255;
    for(let x=0;x<width;x++)for(let c=0;c<channels.length;c++){
      const a=bands[0][x*channels.length+c],b=bands[1][((height-1)*width+x)*channels.length+c];
      delta=Math.max(delta,Math.abs(a-b));minimum=Math.min(minimum,a,b);
    }
    assert.ok(delta<=(level===0?0:5),`actual atlas mip ${level} has a continuous ventral gutter (${delta} byte delta)`);
    if(white)assert.ok(minimum>178,`albedo mip ${level} keeps the ventral gutter white, without dark cutout bleed`);
    metrics.push({level,maximumSeamDelta:delta,minimumEdge:minimum});
    if(level===4)break;
    const nextWidth=width/2,nextHeight=height/2;
    bands=bands.map(previous=>{
      const next=new Float32Array(nextWidth*nextHeight*channels.length);
      for(let y=0;y<nextHeight;y++)for(let x=0;x<nextWidth;x++)for(let c=0;c<channels.length;c++){
        let sum=0;for(let dy=0;dy<2;dy++)for(let dx=0;dx<2;dx++)sum+=previous[((2*y+dy)*width+2*x+dx)*channels.length+c];
        next[(y*nextWidth+x)*channels.length+c]=sum/4;
      }return next;
    });width=nextWidth;height=nextHeight;
  }
  return metrics;
}

const meshList=asset=>{const result=[];(asset.scene??asset).traverse(mesh=>{if(mesh.isMesh)result.push(mesh);});return result;};
const skinMaterial=(mesh,name)=>(Array.isArray(mesh.material)?mesh.material:[mesh.material]).find(material=>material.name===name);

function uvMetrics(meshes,{check=false,atlasWidth=4096}={}){
  let vertices=0,triangles=0,unwrapped=0,maxVSpan=0,maxLongitudinalError=0,mouthEdgeResidual=0;
  const point=new Vector3();
  for(const mesh of meshes){
    const position=mesh.geometry.attributes.position,uv=mesh.geometry.attributes.uv,index=mesh.geometry.index;
    assert.ok(position&&uv&&uv.count===position.count,`${mesh.name} exports complete material UVs`);
    for(let i=0;i<position.count;i++){
      point.fromBufferAttribute(position,i).applyMatrix4(mesh.matrixWorld);
      const u=uv.getX(i),v=uv.getY(i),expected=Math.max(2/atlasWidth,Math.min(1-2/atlasWidth,(point.x+.676)/1.256));
      assert.ok(Number.isFinite(u)&&Number.isFinite(v));
      maxLongitudinalError=Math.max(maxLongitudinalError,Math.abs(u-expected));if(v<0||v>1)unwrapped++;vertices++;
    }
    const count=index?index.count:position.count;
    for(let i=0;i<count;i+=3){
      const ids=[0,1,2].map(k=>index?index.getX(i+k):i+k),vs=ids.map(j=>uv.getY(j));
      const span=Math.max(...vs)-Math.min(...vs);maxVSpan=Math.max(maxVSpan,span);triangles++;
      if(check)assert.ok(span<=.500001,`${mesh.name} triangle cannot interpolate the long way through the circumference (${span})`);
      if(mesh.name.startsWith('Koi_skin'))for(let k=0;k<3;k++){
        const a=ids[k],b=ids[(k+1)%3],xa=position.getX(a),xb=position.getX(b);
        if(xa<-.59&&xb<-.59)mouthEdgeResidual=Math.max(mouthEdgeResidual,Math.abs((uv.getX(b)-uv.getX(a))-(xb-xa)/1.256));
      }
    }
  }
  if(check){assert.ok(maxLongitudinalError<2e-6,'body, head and scales share actual rest-X pigment coordinates');assert.ok(unwrapped>0,'actual seam-crossing corners use integer V lifts');}
  return {vertices,triangles,unwrapped,maxVSpan,maxLongitudinalError,mouthEdgeResidual};
}

export function assertRefinedSkinPBR(material,metadata){
  const pbr=material.pbrMetallicRoughness;
  assert.equal(material.alphaMode??'OPAQUE','OPAQUE');assert.equal((pbr.baseColorFactor??[1,1,1,1])[3],1);
  assert.deepEqual(pbr.baseColorFactor??[1,1,1,1],[1,1,1,1],'opaque albedo is not multiplied by an undeclared global tint');
  assert.ok(pbr.baseColorTexture&&pbr.metallicRoughnessTexture);assert.notEqual(pbr.baseColorTexture.index,pbr.metallicRoughnessTexture.index,'roughness has an independent map');
  assert.equal(pbr.baseColorTexture.texCoord??0,0);assert.equal(pbr.metallicRoughnessTexture.texCoord??0,0);
  assert.ok(near(pbr.roughnessFactor??1,1),'the exported green channel supplies full roughness');assert.equal(pbr.metallicFactor??1,0);
  const extensions=material.extensions??{},expected={ior:1.04,coat:.08,coat_roughness:.35,subsurface:.08,metallic:0,specular:.6};
  for(const [key,value] of Object.entries(expected))assert.ok(near(metadata.pbr[key],value),`manifest PBR ${key} is explicit`);
  for(const [actual,wanted,label] of [[extensions.KHR_materials_ior?.ior,expected.ior,'IOR'],[extensions.KHR_materials_specular?.specularFactor,expected.specular,'specular'],[extensions.KHR_materials_clearcoat?.clearcoatFactor,expected.coat,'coat'],[extensions.KHR_materials_clearcoat?.clearcoatRoughnessFactor,expected.coat_roughness,'coat roughness']])assert.ok(near(actual,wanted),`actual exported ${label} matches manifest`);
  return {pbr,extensions,metallicFactor:pbr.metallicFactor??1};
}

export function assertRefinedTextureUV({asset,metadata}){
  const scene=asset.scene??asset;scene.updateMatrixWorld(true);
  const meshes=meshList(scene),skin=meshes.filter(mesh=>skinMaterial(mesh,metadata.material));
  for(const prefix of ['Koi_skin','Overlapping_scales','Rostral_lip_attachment','Orbital_dermal_annulus','Opercular_plate','Narial_dividing_crest','Narial_integrated_collar'])assert.ok(skin.some(mesh=>mesh.name.startsWith(prefix)),`actual ${prefix} uses shared dermis`);
  const loaded=skinMaterial(skin[0],metadata.material);
  for(const mesh of skin){const current=skinMaterial(mesh,metadata.material);assert.equal(current,loaded,'dermal submeshes share the exported material');assert.equal(current.map,loaded.map);assert.equal(current.roughnessMap,loaded.roughnessMap);assert.equal(current.transparent,false);assert.equal(current.opacity,1);}
  assert.ok(loaded.map&&loaded.roughnessMap);assert.equal(loaded.map.colorSpace,SRGBColorSpace);assert.equal(loaded.roughnessMap.colorSpace,NoColorSpace,'roughness is not decoded with an sRGB transfer');
  return {uv:uvMetrics(skin,{check:true,atlasWidth:metadata.size[0]}),skinMeshes:skin.length};
}

export function assertRefinedTexture({bytes,asset,originalBytes,originalAsset,assetPath,manifest}){
  assertAtlasPNGDecoder();
  const glb=parseGLB(bytes),metadata=manifest.skin_surface;
  assert.ok(metadata,'published manifest describes the actual continuous skin');
  assert.equal(hash(bytes),manifest.geometry_sha256,'manifest identifies these actual GLB bytes');
  assert.deepEqual(metadata.size,[4096,2048]);
  for(const key of ['albedo','roughness'])assert.ok(typeof metadata[key]==='string'&&!/[\\/]/u.test(metadata[key]),'public atlas metadata contains a basename');
  assert.match(metadata.channels,/opaque RGB.*sRGB.*non-color/);assert.match(metadata.uv,/periodic V.*white belly/);
  const matches=glb.json.materials.map((material,index)=>({material,index})).filter(({material})=>material.name===metadata.material);
  assert.equal(matches.length,1,'one material binds continuous body, head and scale pigment');
  const {pbr,extensions,metallicFactor}=assertRefinedSkinPBR(matches[0].material,metadata);
  const albedo=embeddedTexture(glb,pbr.baseColorTexture.index),roughness=embeddedTexture(glb,pbr.metallicRoughnessTexture.index);
  for(const texture of [albedo,roughness])assert.deepEqual([texture.image.width,texture.image.height],metadata.size);
  assert.equal(hash(albedo.bytes),metadata.albedo_sha256,'embedded albedo is exactly the published authored atlas');
  const location=assetPath instanceof URL?fileURLToPath(assetPath):String(assetPath).startsWith('file:')?fileURLToPath(assetPath):assetPath;
  assert.ok(typeof location==='string','assetPath locates the published atlas provenance');
  const roughSourceBytes=readFileSync(resolve(dirname(location),metadata.roughness));
  assert.equal(hash(roughSourceBytes),metadata.roughness_sha256,'published source roughness matches manifest provenance');
  const roughSource=decodeAtlasPNG(roughSourceBytes);
  assert.deepEqual([roughSource.width,roughSource.height],metadata.size);
  let greenError=0,grayError=0;
  for(let i=0;i<roughSource.width*roughSource.height;i++){
    const source=i*roughSource.channels,packed=i*roughness.image.channels,value=roughSource.pixels[source+1];
    greenError=Math.max(greenError,Math.abs(value-roughness.image.pixels[packed+1]));
    grayError=Math.max(grayError,Math.abs(value-roughSource.pixels[source]),Math.abs(value-roughSource.pixels[source+2]));
  }
  assert.equal(grayError,0,'authored roughness is a scalar map');
  assert.equal(greenError,0,'every actual exported GREEN roughness texel preserves the authored non-color scalar');
  const color=assertAlbedoPixels(albedo.image),rough=pixelStats(roughness.image);
  assert.ok(color.red>color.count*.01&&color.white>color.count*.25,'atlas contains distinct red islands and white dermis');
  assert.ok(rough.min[1]/255>.30&&rough.max[1]/255<.44&&rough.max[1]-rough.min[1]>1,'actual metallic-roughness GREEN is a restrained, nonconstant linear surface signal');
  const {uv,skinMeshes}=assertRefinedTextureUV({asset,metadata});
  assert.ok(originalBytes&&originalAsset,'texture join compares the actual previous mouth UVs');parseGLB(originalBytes);
  const originalScene=originalAsset.scene??originalAsset;originalScene.updateMatrixWorld(true);
  const oldUV=uvMetrics(meshList(originalScene).filter(mesh=>mesh.name.startsWith('Koi_skin')));
  assert.ok(oldUV.mouthEdgeResidual>1e-3&&uv.mouthEdgeResidual<oldUV.mouthEdgeResidual*.01,'new actual mouth UV edges remove the previous side-projection discontinuity');
  return {assetPath,skinMeshes,uv,previousMouthEdgeResidual:oldUV.mouthEdgeResidual,
    albedo:{size:metadata.size,min:color.min,max:color.max,minimumRGBPeak:color.minimumRGBPeak,redFraction:color.red/color.count,whiteFraction:color.white/color.count,mips:seamMips(albedo.image,[0,1,2],true)},
    roughness:{green:[rough.min[1]/255,rough.max[1]/255],metallicFactor,mips:seamMips(roughness.image,[1])},
    pbr:{ior:extensions.KHR_materials_ior.ior,specular:extensions.KHR_materials_specular.specularFactor,coat:extensions.KHR_materials_clearcoat.clearcoatFactor,
      subsurface:'authored Blender metadata only; no glTF subsurface transport asserted'}};
}
