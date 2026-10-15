/* Metre-scaled water volume shared by navigation, rendering and collision. */
export const waterMin=Object.freeze([-2,.02,-1.455]);
export const waterMax=Object.freeze([2,1.79,1.045]);
export const tankCenterZ=-.205;
export const tankDepth=waterMax[2]-waterMin[2];
export const tankWidth=waterMax[0]-waterMin[0];
export const waterHeight=waterMax[1]-waterMin[1];
