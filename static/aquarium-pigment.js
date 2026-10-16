import {float,vec3,sin,smoothstep,clamp} from './vendor/three.tsl.js';

/* Art-directed koi pigmentation, not a measured chromatophore model.
 * Positions are the exported, unposed glTF tissue coordinates in metres:
 * X runs snout to peduncle, Y is dorsal, and Z separates the flanks. This
 * field must receive positionGeometry, never world or skinned positions.
 * It only replaces linear diffuse/subsurface color. Tissue geometry, fine
 * normals, roughness and the skin-water specular interface stay independent.
 */
const palette=Object.freeze({
  ivory:Object.freeze([.69,.66,.52]),
  orange:Object.freeze([.66,.095,.013]),
  black:Object.freeze([.011,.015,.019]),
  gold:Object.freeze([.62,.36,.065]),
});
const cpu={
  add:(a,b)=>a+b,sub:(a,b)=>a-b,mul:(a,b)=>a*b,
  sin:Math.sin,clamp:(a,low,high)=>Math.max(low,Math.min(high,a)),
  smooth:(low,high,a)=>{const t=Math.max(0,Math.min(1,(a-low)/(high-low)));return t*t*(3-2*t);},
};
const gpu={
  add:(a,b)=>float(a).add(b),sub:(a,b)=>float(a).sub(b),mul:(a,b)=>float(a).mul(b),
  sin,clamp,smooth:smoothstep,
};
function checkKind(kind){
  if(!['kohaku','showa','ogon'].includes(kind))throw new RangeError(`Unknown koi pigment: ${kind}`);
}

// CPU inspection and GPU shading execute this same arithmetic. Quadratic
// patches with a small fixed contour warp give coherent pigment islands,
// including unequal left/right markings, without following source red pixels.
function masks(kind,[x,y,z],op){
  if(kind==='ogon')return {ivory:0,orange:0,black:0,gold:1};
  if(kind==='kohaku')return {ivory:1,orange:0,black:0,gold:0};
  const {add,sub,mul,sin,smooth}=op;
  const sum=(...values)=>values.reduce(add);
  const square=value=>mul(value,value);
  const warp=mul(.10,mul(sin(sum(mul(x,31),mul(y,17),mul(z,23))),sin(sum(mul(x,19),mul(y,-41),mul(z,13)))));
  const patch=(cx,cy,cz,rx,ry,rz,tilt=0)=>{
    const a=mul(sum(sub(x,cx),mul(z,tilt),mul(y,tilt*.45)),1/rx);
    const b=mul(sub(y,cy),1/ry),c=mul(sub(z,cz),1/rz);
    return sub(1,smooth(.77,1.03,sum(square(a),square(b),square(c),warp)));
  };
  const union=(a,b)=>sub(1,mul(sub(1,a),sub(1,b)));
  const black=union(union(
    patch(-.535,-.010,.028,.080,.106,.120,.22),
    patch(-.105,.015,-.015,.115,.190,.165,-.30)),
    patch(.325,.005,.005,.085,.110,.090,.18));
  const hi=union(patch(-.355,.060,-.010,.165,.165,.205,-.10),
    patch(.145,.025,.008,.155,.155,.155,.12));
  const orange=mul(hi,sub(1,black));
  return {ivory:mul(sub(1,hi),sub(1,black)),orange,black,gold:0};
}
function color(kind,source,position,op){
  const weights=masks(kind,position,op),{add,mul,sin,clamp}=op;
  const [x,y,z]=position;
  // Red-channel detail is deliberately bounded: both source ivory and red
  // retain strong red reflectance. Chromatic source channels do not select
  // the new pigment, so old Kohaku patches are not relabeled as Showa sumi.
  // This is a modest neutral detail carrier, not full albedo separation.
  const grain=mul(.012,mul(sin(add(add(mul(x,317),mul(y,211)),mul(z,89))),sin(add(mul(y,137),mul(z,-293)))));
  const detail=add(add(.95,mul(.10,clamp(source[0],0,1))),grain);
  return [0,1,2].map(channel=>mul(detail,
    ['ivory','orange','black','gold'].map(name=>mul(weights[name],palette[name][channel])).reduce(add)));
}

export function pigmentMasks(kind,position){
  checkKind(kind);return masks(kind,position,cpu);
}
export function evaluatePigment(kind,sourceRGB,position){
  checkKind(kind);return kind==='kohaku'?sourceRGB:color(kind,sourceRGB,position,cpu);
}
export function pigmentColorNode(kind,sourceTextureRGB,restPosition){
  checkKind(kind);
  if(kind==='kohaku')return sourceTextureRGB;
  return vec3(...color(kind,[sourceTextureRGB.r,sourceTextureRGB.g,sourceTextureRGB.b],
    [restPosition.x,restPosition.y,restPosition.z],gpu));
}

// Optional restrained membrane colors; keep radial alpha/ray detail intact.
// These are linear colors, not metalness or a body-atlas lookup for fin UVs.
export function pigmentFinPalette(kind){
  checkKind(kind);
  return kind==='ogon'?[.72,.56,.27]:kind==='showa'?[.68,.65,.54]:[1,1,1];
}
