// Recorded mechanics keep their original immutable asset. The live and
// retargeted-animation views select a matching geometry/mass-profile pair.
export function getAquariumModel(search=''){
  const id=new URLSearchParams(search).get('model')??'refined';
  if(id!=='original'&&id!=='refined')throw new Error('Unknown aquarium model: '+id);
  const root=id==='refined'?'aquarium/refined/':'aquarium/';
  return Object.freeze({id,asset:root+'koi.glb?v=95',profile:root+'body-mass.json?v=95',manifest:root+'manifest.json?v=95'});
}
