export function rotationStyle(rotation,width,height){
  const angle=((Number(rotation)||0)%360+360)%360;
  return {width:(angle%180?height:width)+'px',height:(angle%180?width:height)+'px',transform:`rotate(${angle}deg)`};
}
