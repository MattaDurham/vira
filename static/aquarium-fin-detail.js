/* Engineering contrast LOD for the complete geometric fin-ray set. The
 * filtered membrane map retains distant ray detail. This handoff reduces
 * unresolved tube contrast; it is not pixel coverage or integral antialiasing.
 * The conservative default is the authored fine-branch diameter, in metres. */
export function projectedDiameterPx(options={}){
  const {renderHeight,projectionScaleY,cameraDepth,diameter=.00026}=options??{};
  if(!Number.isFinite(renderHeight)||renderHeight<=0||
    !Number.isFinite(projectionScaleY)||projectionScaleY<=0||
    !Number.isFinite(cameraDepth)||cameraDepth<=0||
    !Number.isFinite(diameter)||diameter<=0)return 0;
  // Use target pixels, the active projection matrix's Y scale, and positive
  // camera-space depth. CSS dimensions and Euclidean distance are different.
  const pixels=diameter*renderHeight*projectionScaleY/(2*cameraDepth);
  return Number.isNaN(pixels)?0:Math.min(Number.MAX_VALUE,pixels);
}

export function finRayContrast(options){
  // Half-pixel tubes are unresolved here; at 1.5 pixels retain their contrast.
  // These transition bounds are an authored detail policy, not a sampling law.
  const t=Math.max(0,Math.min(1,projectedDiameterPx(options)-.5));
  return t*t*(3-2*t);
}
