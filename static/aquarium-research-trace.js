// A short history of the authored axial orientation field, not measured
// kinematics, EMG, or the shape of a reconstructed biological centerline.
export const traceAxes=['lateral','vertical','twist'];
export const traceLimit=Math.PI/4;

// Fin control and body frame attitude use different fixed scales. These are
// live authored controls and rig angles, never reconstructed biological data.
export const pitchTraceLimits=[.15,Math.PI/6];
export function createPitchResearchTrace(duration=4){
  const samples=[];let previous=-Infinity;
  return {
    samples,
    record(age,fin,head,tail){
      if(![age,fin,head,tail].every(Number.isFinite))return;
      if(age<previous){samples.length=0;previous=-Infinity;}
      if(age===previous)return;
      samples.push({age,gap:age-previous>.3,values:[fin,head,tail]});previous=age;
      while(samples.length&&samples[0].age<age-duration)samples.shift();
      while(samples.length>256)samples.shift();
    },
    clear(){samples.length=0;previous=-Infinity;},
    paths(station,now){
      let path='';
      samples.forEach((sample,i)=>{
        const x=58+Math.max(0,1-(now-sample.age)/duration)*228;
        const row=station===0?0:1,limit=pitchTraceLimits[row];
        const y=25+row*36-Math.max(-1,Math.min(1,sample.values[station]/limit))*12;
        path+=`${i===0||sample.gap?'M':'L'}${x.toFixed(2)},${y.toFixed(2)}`;
      });
      return path;
    },
  };
}

export function createResearchTrace(duration=4){
  const samples=[];
  let previous=-Infinity;
  return {
    samples,
    record(age,field){
      if(!Number.isFinite(age)||!field[24]||!field.at(-1))return;
      // Replays rewind simulation time. Paused renders must not manufacture
      // additional time or replace the history with duplicate samples.
      if(age<previous){samples.length=0;previous=-Infinity;}
      if(age===previous)return;
      const gap=age-previous>.3;
      samples.push({age,gap,values:traceAxes.map(axis=>[field[24][axis],field.at(-1)[axis]])});
      previous=age;
      while(samples.length&&samples[0].age<age-duration)samples.shift();
      // Sampling follows the visible 20 Hz annotations. Bound storage if an
      // accelerated study advances its clock by unusually small increments.
      while(samples.length>256)samples.shift();
    },
    clear(){samples.length=0;previous=-Infinity;},
    paths(axis,station,now){
      let path='';
      samples.forEach((sample,i)=>{
        const x=58+Math.max(0,1-(now-sample.age)/duration)*228;
        const y=25+axis*30-Math.max(-1,Math.min(1,sample.values[axis][station]/traceLimit))*11;
        path+=`${i===0||sample.gap?'M':'L'}${x.toFixed(2)},${y.toFixed(2)}`;
      });
      return path;
    },
  };
}
