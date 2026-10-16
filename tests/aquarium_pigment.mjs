import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {vec3} from '../static/vendor/three.tsl.js';
import {pigmentColorNode,pigmentMasks,evaluatePigment,pigmentFinPalette} from '../static/aquarium-pigment.js';

const source=[.83,.47,.22],sourceNode=vec3(...source),positionNode=vec3(-.3,.1,.08);
assert.equal(evaluatePigment('kohaku',source,[-.3,.1,.08]),source,'Kohaku keeps the exact original color argument');
assert.equal(pigmentColorNode('kohaku',sourceNode,positionNode),sourceNode,'Kohaku keeps the exact original shader node');
for(const kind of ['showa','ogon'])assert.ok(pigmentColorNode(kind,sourceNode,positionNode).isNode,'new pigments create a valid TSL graph');
assert.throws(()=>pigmentMasks('missing',[0,0,0]),RangeError);

// Evaluate the actual skin's exported rest positions, including both flanks
// and the head, rather than a rectangular box unrelated to visible tissue.
const bytes=await readFile(new URL('../static/aquarium/koi.glb',import.meta.url));
const jsonLength=bytes.readUInt32LE(12),document=JSON.parse(bytes.subarray(20,20+jsonLength).toString('utf8'));
const binary=bytes.subarray(28+jsonLength),mesh=document.meshes.find(mesh=>mesh.name==='Koi skin');
assert.ok(mesh,'the shipped skin is exercised');
const accessor=document.accessors[mesh.primitives[0].attributes.POSITION],view=document.bufferViews[accessor.bufferView];
assert.equal(accessor.componentType,5126);assert.equal(accessor.type,'VEC3');
const stride=view.byteStride??12,offset=(view.byteOffset??0)+(accessor.byteOffset??0);
const points=Array.from({length:accessor.count},(_,i)=>[0,1,2].map(axis=>binary.readFloatLE(offset+i*stride+axis*4)));
const dominant={ivory:0,orange:0,black:0},difference=[];
let coloredHead=0;
for(const point of points){
  const mask=pigmentMasks('showa',point);
  assert.ok(Object.values(mask).every(value=>Number.isFinite(value)&&value>=0&&value<=1),'bounded masks on real tissue');
  assert.ok(Math.abs(Object.values(mask).reduce((a,b)=>a+b)-1)<1e-12,'pigment weights partition the tissue');
  const name=['ivory','orange','black'].reduce((a,b)=>mask[a]>=mask[b]?a:b);dominant[name]++;
  if(point[0]<-.48&&mask.black>.75)coloredHead++;
  const mirrored=pigmentMasks('showa',[point[0],point[1],-point[2]]);
  difference.push(Math.abs(mask.black-mirrored.black)+Math.abs(mask.orange-mirrored.orange));
  for(const kind of ['showa','ogon']){
    const value=evaluatePigment(kind,source,point);
    assert.ok(value.every(channel=>Number.isFinite(channel)&&channel>0&&channel<1),'finite bounded linear color');
    // A source red/white recolor would fail: source chroma has no power over
    // either new pigment layout or color when the neutral carrier is equal.
    assert.deepEqual(evaluatePigment(kind,[.8,.02,.005],point),evaluatePigment(kind,[.8,.8,.75],point),'new pattern is independent of old red/ivory chroma');
    if(kind==='ogon')assert.ok(value[0]>value[1]&&value[1]>value[2]&&value[2]>.04,'continuous gold leaves no old black or ivory islands');
  }
}
for(const [name,count] of Object.entries(dominant))assert.ok(count>points.length*.04,`${name} occupies visible actual skin rather than a missing/off-body mask`);
assert.ok(coloredHead>30,'Showa carries sumi into the head');
assert.ok(difference.some(value=>value>.5),'left/right patterns are independently spatial rather than mirrored UV islands');

// Tiny rest-space changes produce tiny color changes across every boundary;
// pigmentation should not snap when adjacent face and scale samples differ.
let maximumJump=0;
for(const point of points.filter((_,i)=>i%17===0)){
  const a=evaluatePigment('showa',source,point);
  for(let axis=0;axis<3;axis++){
    const adjacent=point.slice();adjacent[axis]+=1e-6;
    const b=evaluatePigment('showa',source,adjacent);
    maximumJump=Math.max(maximumJump,...a.map((value,i)=>Math.abs(value-b[i])));
  }
}
assert.ok(maximumJump<.001,'scalloped pigment boundaries remain continuous in rest space');
assert.deepEqual(pigmentFinPalette('kohaku'),[1,1,1]);
assert.ok(pigmentFinPalette('ogon')[0]>pigmentFinPalette('ogon')[2]);
console.log(JSON.stringify({skinVertices:points.length,dominant,headSumi:coloredHead,maximumJump}));
