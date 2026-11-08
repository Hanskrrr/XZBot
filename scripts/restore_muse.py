"""Run from the extracted bundle's project directory on Linux or WSL2."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.dont_write_bytecode=True
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from azusa_bot.migration import restore_bundle,verify_bundle

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--bundle',type=Path,default=ROOT.parent)
    parser.add_argument('--destination',type=Path)
    parser.add_argument('--verify-only',action='store_true')
    args=parser.parse_args();os.umask(0o077)
    try:
        if args.verify_only:
            info=verify_bundle(args.bundle);result={'integrity':'ok','accounts':len(info['accounts']),'version':info['version']}
        else:
            if not args.destination:raise ValueError('destination_required')
            result=restore_bundle(args.bundle,args.destination)
    except Exception:raise SystemExit('Migration check/restore failed; no credentials were printed. Use a new destination and an intact private bundle on Linux/WSL2.') from None
    print(json.dumps(result,ensure_ascii=False))
