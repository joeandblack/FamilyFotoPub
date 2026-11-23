"""MTS playback copies: preserve the original; copy compatible H.264 video."""
from . import flv

PROFILE={**flv.PROFILE,'format':'mts','version':1}


def plan(probe):
    try:selected=flv.plan(probe)
    except ValueError as error:raise ValueError(str(error).replace('FLV','MTS')) from error
    selected['profile']=dict(PROFILE)
    return selected


def command(tools,source,output,selected):
    args=flv.command(tools,source,output,selected)
    args[args.index('-format_whitelist')+1]='mpegts'
    return args


def convert_one(db,media,config,tools,monitor):
    from .avi import convert_one as lifecycle
    return lifecycle(db,media,config,tools,monitor,format_handler='mts')
