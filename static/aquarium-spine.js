import {regionalTurnAngle} from './aquarium-turn.js?v=90';

const clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
export const axialRates=Object.freeze([14,12,10]),axialDelays=[0];
axialRates.forEach(rate=>axialDelays.push(axialDelays.at(-1)+1/rate));
const alongBody=(values,s)=>{
  const t=clamp(s,0,1)*3,i=Math.min(2,Math.floor(t));
  return values[i]+(values[i+1]-values[i])*(t-i);
};
export const turnCarrierShare=activity=>{
  const load=clamp(activity/.25,0,1);
  return 1-load*load*(3-2*load);
};


export function spinalField(fish,axial){
  const length=axial.at(-1)-axial[5];
  const stroke=fish.stroke??1,drive=clamp(fish.drive??fish.speed??0,0,.5);
  const lateralGain=fish.lateralGain??1,verticalGain=fish.verticalGain??1,twistGain=fish.twistGain??1;
  const history=(prefix,rate)=>fish.poseHistory===false||!Number.isFinite(fish[prefix+'1'])?
    axialDelays.map(delay=>-rate*delay):[0,fish[prefix+'1'],fish[prefix+'2'],fish[prefix+'3']];
  const yawHistory=history('yawLag',clamp(fish.yawRate??0,-1.3,1.3));
  const pitchHistory=history('pitchLag',clamp(fish.pitchRate??0,-.5,.5));
  const bankHistory=history('bankLag',clamp(fish.bankRate??0,-.5,.5));
  const turnHistory=fish.poseHistory===false?[0,0,0,0]:
    [fish.turnBend??0,fish.turnBend1??0,fish.turnBend2??0,fish.turnBend3??0];
  const recruitmentHistory=fish.poseHistory===false?[0,0,0,0]:
    [fish.turnRecruitment??0,fish.turnRecruitment1??0,fish.turnRecruitment2??0,fish.turnRecruitment3??0];
  // Authored studies have no elapsed history: show a steady bend at the given
  // angular rate and a full travelling stroke, rather than borrowing live state.
  const muscleHistory=fish.poseHistory===false||!Number.isFinite(fish.muscle1)?
    [stroke,stroke,stroke,stroke]:[stroke,fish.muscle1,fish.muscle2,fish.muscle3];
  return axial.map((x,i)=>{
    if(i<=4)return {lateral:0,vertical:0,twist:0};
    const s=clamp((x-axial[5])/length,0,1),phase=fish.phase-4.8*s;
    const muscle=alongBody(muscleHistory,s);
    // A body-driven turn replaces the independent cruise wave where it loads.
    // R=.25 denotes full carrier replacement. The finite controller scales
    // its C2 envelope by body-turn strength, so weak fin trim retains the
    // ordinary propulsive wave. These blend thresholds are authored.
    const carrier=turnCarrierShare(alongBody(recruitmentHistory,s));
    const amplitude=(.09+drive*.10)*muscle*carrier;
    const lateral=(Math.atan(amplitude/length*(2*s*Math.sin(phase)-4.8*s*s*Math.cos(phase)))
      +alongBody(yawHistory,s)+(fish.poseHistory!==false&&Number.isFinite(fish.turnAnterior)?regionalTurnAngle(s,[fish.turnAnterior,fish.turnTrunk,fish.turnPosterior]):-alongBody(turnHistory,s)*s*s*(3-2*s)))*lateralGain;
    // Carp propulsion remains predominantly lateral. Vertical flexion follows
    // pitch manoeuvres; the small secondary wave is compliance, not a dolphin kick.
    const vertical=(-alongBody(pitchHistory,s)+.018*muscle*s*s*Math.sin(phase+.7))*verticalGain;
    // A banking head and lagging tail put a small torsion into the trunk.
    const torsion=(alongBody(bankHistory,s)+(fish.torsionDrive??0)*s
      +.022*muscle*s*s*Math.sin(phase-1))*twistGain;
    return {lateral,vertical,twist:torsion};
  });
}
