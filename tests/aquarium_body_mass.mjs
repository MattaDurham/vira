import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {buildBodyMass} from '../scripts/aquarium/build_body_mass.mjs';
import {integrateVolume} from '../scripts/aquarium/neutral_skin_volume.mjs';
const vertices=new Float64Array([0,0,0,1,0,0,0,2,0,0,0,3]),faces=new Uint32Array([0,2,1,0,1,3,0,3,2,1,2,3]);
for(const origin of [[0,0,0],[.3,-.4,.2],[-3,4,5]]){
 const m=integrateVolume(vertices,faces,{origin});
 assert.ok(Math.abs(m.volume-1)<1e-12&&Math.abs(m.mass-1000)<1e-9);
 for(const [i,value] of [.25,.5,.75].entries())assert.ok(Math.abs(m.com[i]-value)<1e-12);
 const expected=[487.5,25,37.5,25,375,75,37.5,75,187.5];
 for(let i=0;i<9;i++)assert.ok(Math.abs(m.inertia[i]-expected[i])<1e-8,'signed closed-volume moments must match the analytic tetrahedron independent of origin');
}
const profile=JSON.parse(await readFile(new URL('../static/aquarium/body-mass.json',import.meta.url),'utf8'));
assert.deepEqual(await buildBodyMass(),profile,'committed masses must reproduce from the actual uploaded neutral skin and declared closure');
console.log('Analytic closed-volume moments, origin invariance, and actual-asset mass regeneration passed.');
