import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import {projectedDiameterPx,finRayContrast} from '../static/aquarium-fin-detail.js';

const near=(actual,expected,message)=>assert.ok(Math.abs(actual-expected)<1e-12,message);

export function assertFinDetail(project=projectedDiameterPx,contrast=finRayContrast){
  const base={renderHeight:1080,projectionScaleY:47*(16/9)/18,cameraDepth:4};
  near(project(base),.00026*1920*47/36/4,'desktop projection uses actual render pixels and camera depth');
  near(project({...base,diameter:.00064}),project(base)*.00064/.00026,'tube diameter controls its projected scale');
  near(project({...base,renderHeight:2160}),project(base)*2,'doubling target resolution doubles projected diameter');
  near(project({...base,cameraDepth:8}),project(base)/2,'doubling camera depth halves projected diameter');
  near(project({...base,projectionScaleY:base.projectionScaleY*2}),project(base)*2,
    'narrower camera projection magnifies geometric detail');
  near(project({...base,renderHeight:2160,projectionScaleY:base.projectionScaleY/2}),project(base),
    'equivalent focal scales agree across different target and projection combinations');

  // Different cameras and encoder extents can describe the same physical ray.
  // At 1080p this model's normal view is deeply subpixel; the 10-degree 5K
  // closeup resolves enough branch relief to retain most geometric contrast.
  assert.equal(contrast(base),0,'unresolved normal-view branches hand detail to the membrane map');
  const closeup={renderHeight:2880,projectionScaleY:1/Math.tan(Math.PI/36),cameraDepth:3.85};
  assert.ok(contrast(closeup)>.5,'resolved closeup branches retain geometric relief');
  assert.ok(contrast({...closeup,renderHeight:1080})<contrast(closeup),
    '1080p recording uses its own sampling budget instead of native display dimensions');

  const unit={renderHeight:2000,projectionScaleY:1,cameraDepth:1};
  near(contrast({...unit,diameter:.0005}),0,'the authored fade begins at half a pixel');
  near(contrast({...unit,diameter:.001}),.5,'the contrast transition is smooth and centred at one pixel');
  near(contrast({...unit,diameter:.0015}),1,'resolved rays retain full material contrast');
  let previous=-1;
  for(let i=0;i<=1000;i++){
    const options={...unit,diameter:i*.000003};
    const value=contrast(options);
    assert.ok(Number.isFinite(value)&&value>=0&&value<=1,'contrast stays finite and bounded');
    assert.ok(value>=previous,'larger projected rays never lose contrast');
    previous=value;
  }
  let far=1;
  for(let depth=.1;depth<=20;depth+=.1){
    const value=contrast({...closeup,cameraDepth:depth});
    assert.ok(value<=far,'receding geometry never gains contrast');far=value;
  }
  const epsilon=1e-5;
  for(const diameter of [.0005,.0015]){
    const center=contrast({...unit,diameter});
    assert.ok(Math.abs(contrast({...unit,diameter:diameter+epsilon*.001})-center)<1e-8,
      'the contrast handoff has no hard pop at its upper or lower threshold');
  }

  for(const key of ['renderHeight','projectionScaleY','cameraDepth','diameter']){
    for(const invalid of [0,-1,NaN,Infinity,-Infinity,undefined]){
      if(key==='diameter'&&invalid===undefined)continue; // The documented branch default.
      const options={...base,diameter:.00026,[key]:invalid};
      assert.equal(project(options),0,`invalid ${key} produces no projected detail`);
      assert.equal(contrast(options),0,`invalid ${key} produces no geometric contrast`);
    }
  }
  for(const options of [undefined,null,{}, {renderHeight:Number.MAX_VALUE,
    projectionScaleY:Number.MAX_VALUE,cameraDepth:Number.MIN_VALUE,diameter:Number.MAX_VALUE}]){
    assert.ok(Number.isFinite(project(options)),'missing or overflowing projection inputs stay finite');
    assert.ok(Number.isFinite(contrast(options)),'missing or overflowing contrast inputs stay finite');
  }
}

if(process.argv[1]&&import.meta.url===pathToFileURL(process.argv[1]).href){
  assertFinDetail();
  console.log('Fin detail passed: projection, resolution, camera depth, smooth contrast handoff and invalid inputs.');
}
