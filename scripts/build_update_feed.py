"""Create an origin-independent feed beside verified ZIPs, ready for static hosting."""
import hashlib
import json
from pathlib import Path


def build(directory):
    directory=Path(directory);releases=[]
    for path in sorted(directory.glob('*.verification.json')):
        report=json.loads(path.read_text())
        if any(report.get(k)!='passed' for k in ('archive_install_test','core_bridge_test','unicode_directory_test')):
            raise ValueError('Only verified packages can be included in the update feed.')
        archive=path.with_name(path.name.replace('.verification.json','.zip'))
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=report['zip_sha256']:
            raise ValueError('The package differs from its verification report.')
        releases.append({k:report[k] for k in ('version','platform','arch','commit')}|
                        {'sha256':report['zip_sha256'],'url':path.name.replace('.verification.json','.zip')})
    if not releases:raise ValueError('No verified packages.')
    (directory/'updates.json').write_text(json.dumps({'schema':1,'releases':releases},indent=2)+'\n')

if __name__=='__main__':
    import sys
    build(sys.argv[1] if len(sys.argv)>1 else 'dist/release')
