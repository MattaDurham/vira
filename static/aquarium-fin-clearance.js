/* Authored paired-fin coordination for this rig's tightly curved turns.
 * This is a geometric clearance policy, not a measured koi reflex. */
const onset=1.6,folded=2.2;
const regionFactor=value=>{
  const bend=Math.abs(Number.isFinite(value)?value:0);
  if(bend<=onset)return 1;
  if(bend>=folded)return 0;
  const t=(bend-onset)/(folded-onset);
  return 1-t*t*t*(10+t*(-15+6*t));
};

// Multiplying compact C2 factors avoids a hard maximum's derivative cusp
// when differently moving regions exchange which has the greatest bend.
// Observe issued regions, not root yaw or the desired course. A held study
// omits the elapsed turning field and must not inherit a live tuck.
export function finClearanceFactor(fish){
  if(fish.poseHistory===false)return 1;
  // Urgent episodes can issue a C2 pre-tuck before loading the body. Their
  // planned weight reaches this same command path before paddle history is
  // observed; it does not erase a response already travelling down the fin.
  const planned=Number.isFinite(fish.turnFinClearance)?Math.max(0,Math.min(1,fish.turnFinClearance)):1;
  return planned*regionFactor(fish.turnBend1??fish.turnBend??0)
    *regionFactor(fish.turnBend2??0)*regionFactor(fish.turnBend3??0);
}

// A tight turn also folds the complete foil cranially about its existing
// basal joint. Merely removing the active stroke leaves the authored ray
// tubes in the curved body's path. This finite 0.30 radian presentation is
// an authored clearance pose, not a force or a measured biological reflex.
export function finClearanceFold(factor,pectoral=true){
  const weight=Number.isFinite(factor)?Math.max(0,Math.min(1,factor)):1;
  if(weight===1)return 0;
  return -.30*(1-weight)*(pectoral?1:.35);
}
