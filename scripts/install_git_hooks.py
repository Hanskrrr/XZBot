"""Explicitly install repository-scoped hooks, preserving other hook setups."""
import subprocess
from pathlib import Path

root=Path(__file__).resolve().parents[1]
result=subprocess.run(['git','config','--get','core.hooksPath'],cwd=root,capture_output=True,text=True)
if result.returncode not in (0,1):raise SystemExit('Cannot read repository hook settings.')
current=result.stdout.strip()
if current and current!='.githooks':raise SystemExit('Existing hooksPath found; not overwritten. Integrate these checks manually.')
if not current:
 folder=root/'.git/hooks'
 if any(p.is_file() and not p.name.endswith('.sample') for p in folder.glob('*')):
  raise SystemExit('Existing Git hooks found; not overwritten. Integrate these checks manually.')
for name in ('pre-commit','pre-push'):(root/'.githooks'/name).chmod(0o755)
subprocess.run(['git','config','--local','core.hooksPath','.githooks'],cwd=root,check=True)
print('Repository-local commit and push checks installed.')
