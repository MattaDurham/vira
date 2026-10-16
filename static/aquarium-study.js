import {tankCenterZ} from './aquarium-space.js?v=89';

const tickDuration=1/120;

export function createStudyRandom(seed=913){
  const initial=Number.isInteger(seed)&&seed>=0&&seed<=0xffffffff?seed:913;
  let value=initial;
  const random=()=>{value=(Math.imul(value,1664525)+1013904223)>>>0;return value/4294967296;};
  random.reset=()=>{value=initial;};
  return random;
}

// Real navigation commands exercise the same forces, gait and causal rig as
// free swimming. Fixed ticks make the route independent of display cadence.
export function createTurnStudy(swimmer,{random,onTick=()=>{},onReset=()=>{}}={}){
  let tick=0,accumulator=0;
  function reset(){
    random?.reset();swimmer.reset();tick=0;accumulator=0;
    Object.assign(swimmer.fish,{x:.55,y:.84,z:tankCenterZ,yaw:0});
    swimmer.navigate({x:-.85,y:.84,z:tankCenterZ});
    onReset(swimmer.fish);
  }
  function step(elapsed){
    // Match the live swimmer's stall bound; a hidden or stalled page does not
    // replay a long backlog of unobserved simulation on its next frame.
    accumulator+=Math.max(0,Math.min(Number.isFinite(elapsed)?elapsed:0,.25));
    // Sub-nanosecond rounding at frame boundaries must not drop a whole tick.
    while(accumulator+1e-10>=tickDuration){
      if(tick===240)swimmer.navigate({x:.85,y:.84,z:tankCenterZ+.04});
      swimmer.step(tickDuration);tick++;
      accumulator=Math.max(0,accumulator-tickDuration);
      onTick(swimmer.fish,tick);
    }
  }
  return {reset,step,get tick(){return tick;},get accumulator(){return accumulator;}};
}
