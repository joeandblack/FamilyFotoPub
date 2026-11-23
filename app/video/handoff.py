"""Retired library-wide launcher; all handoffs are now per import."""
import argparse


def run(job_id,convert_after=False):
    raise RuntimeError("Legacy handoff retired; use per-import automatic stages in Imports")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job",type=int,required=True)
    parser.add_argument("--convert-after",action="store_true")
    args=parser.parse_args();run(args.job,args.convert_after)
