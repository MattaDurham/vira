import {tankCenterZ} from './aquarium-space.js?v=89';

const tickDuration=1/120,duration=16,lastTick=duration*120;
const schedule=[
  {tick:240,correction:.06,height:.84,stage:'course-positive'},
  {tick:600,correction:-.10,height:.84,stage:'course-negative'},
  {tick:960,correction:.10,height:.92,stage:'climb'},
  {tick:1440,correction:-.06,height:.92,stage:'climb-glide'},
];

// A finite sequence of real navigation decisions, not an authored pose track.
// Every destination is issued once from the actual current position/heading.
// The live swimmer chooses forces, gait and attitude; observers receive every
// complete 120Hz tick for the same fin histories used by ordinary swimming.
export function createGentleStudy(swimmer,{random,onTick=()=>{},onReset=()=>{}}={}){
  let tick=0,accumulator=0,started=false,stage='ready',commands=[];
  function issue(correction,height,nextStage){
    const fish=swimmer.fish,bearing=fish.yaw+correction;
    const point={x:-1.05,y:height,z:fish.z+(fish.x+1.05)*Math.tan(bearing)};
    swimmer.navigate(point);stage=nextStage;
    commands.push({tick,age:fish.age,correction,bearing,from:{x:fish.x,y:fish.y,z:fish.z,yaw:fish.yaw},point});
  }
  function reset(){
    random?.reset?.();swimmer.reset();tick=0;accumulator=0;commands=[];started=true;
    Object.assign(swimmer.fish,{x:.85,y:.84,z:tankCenterZ,yaw:0});
    issue(0,.84,'level');onReset(swimmer.fish);
  }
  function step(elapsed){
    if(!started||tick===lastTick)return;
    accumulator+=Math.max(0,Math.min(Number.isFinite(elapsed)?elapsed:0,.25));
    while(tick<lastTick&&accumulator+1e-10>=tickDuration){
      const decision=schedule.find(event=>event.tick===tick);
      if(decision)issue(decision.correction,decision.height,decision.stage);
      swimmer.step(tickDuration);tick++;
      accumulator=Math.max(0,accumulator-tickDuration);onTick(swimmer.fish,tick);
    }
    if(tick===lastTick){accumulator=0;stage='complete';}
  }
  return {reset,step,get tick(){return tick;},get accumulator(){return accumulator;},get duration(){return duration;},
    get complete(){return started&&tick===lastTick;},get stage(){return stage;},
    get commands(){return commands.map(command=>({...command,from:{...command.from},point:{...command.point}}));}};
}
