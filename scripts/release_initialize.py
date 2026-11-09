"""Initialize missing public-release defaults, never overwrite a user's settings."""
import argparse
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
from azusa_bot.settings import private_write,token
from azusa_bot.host import protect


def initialize(root,astrbot=None):
    for folder in ('data','config','content','content/lore','content/assets'):
        path=root/folder;path.mkdir(parents=True,exist_ok=True);protect(path)
    for source,dest in [('config/example.json','config/local.json')]:
        if not (root/dest).exists():private_write(root/dest,(root/source).read_text(encoding='utf-8'))
    if not (root/'config/runtime.json').exists():
        private_write(root/'config/runtime.json',json.dumps({'mode':'lima' if os.sys.platform=='darwin' else 'external'}))
    if os.sys.platform.startswith('linux') and not (root/'data/credential-backend.json').exists():
        from azusa_bot.portable_credentials import MARKER
        private_write(root/'data/credential-backend.json',json.dumps(MARKER))
    for name in ('admin','bridge','onebot','napcat-webui'):token(root,name)
    from azusa_bot.starter_templates import initialize as initialize_starters
    initialize_starters(root)
    if not (root/'content/persona.md').exists():private_write(root/'content/persona.md',(root/'examples/persona.md').read_text(encoding='utf-8'))
    if astrbot:private_write(root/'data/astrbot-source.path',str(Path(astrbot).resolve()))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--astrbot');args=p.parse_args()
    initialize(ROOT,args.astrbot)
    print('XZBot initialized; private credentials and QQ sessions are never bundled.')
