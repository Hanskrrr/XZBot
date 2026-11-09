"""Private updater child, executed while the parent holds the startup guards."""
import sys
from start_onebot import ROOT, ensure_core, ensure_bridge

if __name__=='__main__':
    if sys.argv[1:] == ['core']:ensure_core(ROOT)
    elif sys.argv[1:] == ['bridge']:ensure_bridge(ROOT)
    else:raise SystemExit(2)
