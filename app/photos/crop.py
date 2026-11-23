"""Normalized EXIF-oriented crop coordinates; original media is never rewritten."""
import math

def validate_crop(value):
    if value is None:
        return None
    if not isinstance(value,dict) or set(value)!={'x','y','width','height'}:
        raise ValueError('Crop requires x, y, width and height')
    if any(type(v) not in (int,float) or not math.isfinite(v) for v in value.values()):
        raise ValueError('Crop coordinates must be finite numbers')
    x,y,w,h=(value[k] for k in ('x','y','width','height'))
    if x<0 or y<0 or x>=1 or y>=1 or w<=0 or h<=0 or x+w>1.00000001 or y+h>1.00000001:
        raise ValueError('Crop must fit inside the image')
    return {'x':x,'y':y,'width':min(w,1-x),'height':min(h,1-y)}

def pixel_box(crop,width,height):
    x,y,w,h=(crop[k] for k in ('x','y','width','height'))
    left=min(width-1,int(x*width));top=min(height-1,int(y*height))
    return (left,top,max(left+1,min(width,round((x+w)*width))),max(top+1,min(height,round((y+h)*height))))
