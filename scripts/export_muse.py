"""Private migration export, never uploads or prints credentials."""
import argparse
import json
import os
from pathlib import Path
import sys
sys.dont_write_bytecode=True

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from azusa_bot.migration import export_bundle

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--include-private-credentials',action='store_true',required=True)
    args=parser.parse_args();os.umask(0o077)
    try:result=export_bundle(ROOT,args.output)
    except Exception:raise SystemExit('Private export failed; no credential values were printed.') from None
    print(json.dumps(result,ensure_ascii=False))
