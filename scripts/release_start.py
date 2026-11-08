"""Double-click native entry: owned core + bridge, with a visible local console."""
import os
from start_onebot import ROOT,ensure_core,ensure_bridge,open_management,startup_guard,start_vm
from azusa_bot.bot_control import activity_allowed


def main():
    with startup_guard(ROOT):
        ensure_core(ROOT)
        if activity_allowed(ROOT):
            ensure_bridge(ROOT)
            start_vm(ROOT)
    open_management(ROOT)

if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('XZBot could not start. Check data/core.log and data/onebot-launch.log; no other application was stopped.') from None
