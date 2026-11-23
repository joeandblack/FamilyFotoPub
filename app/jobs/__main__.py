import argparse
from . import STAGES, run, __doc__ as description

parser=argparse.ArgumentParser(description=description)
parser.add_argument('--job',type=int,required=True)
parser.add_argument('--stage',choices=STAGES,required=True)
parser.add_argument('--handoff')
args=parser.parse_args();run(args.job,args.stage,args.handoff)
