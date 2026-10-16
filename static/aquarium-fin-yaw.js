/* Authored basal-motor steering response, not a pressure or CFD solver.
 * The same clearance-scaled paddle/fore-aft angles drive the rendered foil.
 * Desired heading, root yaw, passive lag and flow-driven cup are not inputs. */
const clamp=(x,a,b)=>Math.max(a,Math.min(b,x));
const ease=x=>{const t=clamp(x,0,1);return t*t*t*(10+t*(-15+6*t));};
const positive=(x,width)=>{
  if(x<=0)return 0;if(x>=width)return x;
  const t=x/width;return width*t*t*t*(6+t*(-8+3*t));
};

// A bounded nonreciprocal trim path: present, load, reorient, retrieve.
// Radians/seconds and the modest bilateral difference are authored for this
// model, not measured koi kinematics. Both attachment points remain fixed.
export function trimFinAngles(age,strength,leading){
  const t=age-(leading?0:.08),amplitude=.025*strength*(leading?1:.9);
  const loaded=ease((t-.06)/.12),returned=ease((t-.26)/.28);
  return {paddle:amplitude*loaded*(1-returned),
    foreAft:(leading?1:-1)*.10*strength*ease(t/.06)*(1-ease((t-.18)/.08))};
}

// sample(age, plus, minus) consumes ACTUAL basal actuation after clearance,
// before heading integration. Each control has {paddle,foreAft} in radians.
// Returned moment is angular acceleration (rad/s²), damping is 1/s. Output is
// reused; retained snapshots must copy it. The .24s tissue histories remain
// in the fin renderer and are not reset or substituted by this approximation.
export function createFinYawController(){
  const output={moment:0,damping:0,loadingPlus:0,loadingMinus:0};
  const previous=new Float64Array(2);let age;
  function reset(){
    age=undefined;previous.fill(0);
    Object.assign(output,{moment:0,damping:0,loadingPlus:0,loadingMinus:0});return output;
  }
  reset();
  return {reset,sample(now,plus,minus){
    if(!Number.isFinite(now)||now===age)return output;
    const dt=age===undefined?0:now-age;
    if(dt<0||dt>.25)reset();
    output.moment=output.damping=output.loadingPlus=output.loadingMinus=0;
    for(let i=0;i<2;i++){
      const side=i===0?1:-1,control=i===0?plus:minus;
      const paddle=control.paddle,foreAft=control.foreAft;
      const rate=dt>0&&dt<=.25?(paddle-previous[i])/dt:0;previous[i]=paddle;
      // Only a moving, positively loaded stroke provides active steering.
      // The bounded presentation factor supplies its signed bilateral couple.
      const loaded=positive(rate,.02)*ease(paddle/.002);
      const orientation=foreAft/Math.hypot(foreAft,.002);
      const moment=2.5*side*orientation*loaded;
      output.moment+=moment;output[i===0?'loadingPlus':'loadingMinus']=moment;
      // Reoriented retrieval can dissipate existing yaw, never start it.
      const moving=Math.hypot(rate,.001)-.001;
      output.damping+=.8*(ease(-paddle/.004)*ease(moving/.05)
        +ease(paddle/.004)*ease(positive(-rate,.02)/.05));
    }
    age=now;return output;
  }};
}
