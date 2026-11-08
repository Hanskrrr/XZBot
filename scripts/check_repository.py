"""Reject private deployment files and recognizable secrets in Git objects.

Only filenames and rule names are reported. Scan the index or a commit range,
not arbitrary runtime files. This is a guardrail, not a complete secret audit.
"""
from __future__ import annotations
import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys

ROOT_FILES={'README.md','LICENSE','pyproject.toml','requirements-lock.txt','requirements-astrbot.lock','requirements-gemini-balance.lock','.gitignore','.gitattributes','启动QQ角色助手.command','启动管理台.command','停止QQ角色助手.command'}
SOURCE_DIRS={'src','scripts','tests','ui','examples','astrbot_plugin_azusa_bridge','astrbot_plugin_azusa_onebot','.githooks','.github'}
DOCS={'architecture.md','getting-started.md','synchronization.md','privacy.md','platform-support.md','public-migration.md'}
BLOCKED_PARTS={'data','accounts','content','runtime','.runtime','dist','node_modules','.venv','__pycache__','.pytest_cache','.git'}
RULES={
 'private_key':re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
 'github_token':re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b'),
 'api_token':re.compile(rb'\bsk-[A-Za-z0-9_-]{24,}\b'),
 'google_api_key':re.compile(rb'\bAIza[A-Za-z0-9_-]{30,}\b'),
 'aws_access_key':re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
}


def allowed_path(name):
 p=PurePosixPath(name)
 if p.is_absolute() or '..' in p.parts or '\\' in name:return False
 if any(part in BLOCKED_PARTS or part.endswith('.egg-info') for part in p.parts):return False
 lower=p.name.lower()
 if lower.startswith('.env') or re.search(r'\.(?:sqlite\w*|db(?:-\w+)?|token|log|pem|key|p12|pfx|zip|bundle|bak)$',lower):return False
 if p.parts[0]=='examples':return name=='examples/persona.md'
 if name in ROOT_FILES or name=='config/example.json':return True
 if p.parts[0]=='docs':return len(p.parts)==2 and p.name in DOCS
 return len(p.parts)>1 and p.parts[0] in SOURCE_DIRS


def git(*args):
 return subprocess.check_output(['git',*args],stderr=subprocess.DEVNULL)


def entries(rev=None):
 if rev is None:
  for row in git('ls-files','--stage','-z').split(b'\0'):
   if not row:continue
   meta,name=row.split(b'\t',1);mode,oid,stage=meta.split()
   if stage!=b'0':raise ValueError('unmerged_index')
   yield name.decode(),mode.decode(),oid.decode()
 else:
  for row in git('ls-tree','-r','-z',rev).split(b'\0'):
   if not row:continue
   meta,name=row.split(b'\t',1);mode,kind,oid=meta.split()
   yield name.decode(),mode.decode(),oid.decode()


def inspect(rows,cache=None):
 cache={} if cache is None else cache;failures=[];count=0
 for name,mode,oid in rows:
  count+=1
  if not allowed_path(name):failures.append((name,'private_or_unreviewed_path'));continue
  if mode not in {'100644','100755'}:failures.append((name,'symlink_or_submodule'));continue
  if oid not in cache:
   data=git('cat-file','blob',oid)
   cache[oid]=['oversized_source'] if len(data)>2_000_000 else [rule for rule,pattern in RULES.items() if pattern.search(data)]
  failures.extend((name,rule) for rule in cache[oid])
 return count,failures


def main():
 parser=argparse.ArgumentParser();mode=parser.add_mutually_exclusive_group(required=True)
 mode.add_argument('--staged',action='store_true');mode.add_argument('--ref');mode.add_argument('--pre-push',action='store_true')
 args=parser.parse_args();cache={};failures=[];count=0
 try:
  if args.staged:count,failures=inspect(entries(),cache)
  elif args.ref:count,failures=inspect(entries(args.ref),cache)
  else:
   commits=set()
   for line in sys.stdin:
    local_ref,local_sha,remote_ref,remote_sha=line.split()
    if set(local_sha)=={'0'}:continue
    command=['rev-list',local_sha]
    if set(remote_sha)!={'0'}:
     if subprocess.run(['git','cat-file','-e',remote_sha+'^{commit}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0:command.append('^'+remote_sha)
    commits.update(git(*command).decode().splitlines())
   for rev in commits:
    n,bad=inspect(entries(rev),cache);count+=n;failures.extend(bad)
 except Exception:
  print('Repository check could not complete; push/commit refused.',file=sys.stderr);return 1
 for name,rule in sorted(set(failures)):print(f'{rule}: {name}',file=sys.stderr)
 if failures:return 1
 print(f'Repository check passed ({count} file entries).');return 0

if __name__=='__main__':raise SystemExit(main())
