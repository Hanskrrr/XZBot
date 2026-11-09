"""Reviewed public character seeds. Never export live deployment directories."""
import json
from pathlib import Path
import time

from . import lore,prompts
from .settings import private_write

IDS=('azusa','hazel')
HAZEL_ID='2b5ad4b6d89b4bb789bcf28ae609b521'


def load(key):
    if key not in IDS:raise ValueError('starter_profile_not_found')
    data=json.loads((Path(__file__).with_name('starter_profiles')/(key+'.json')).read_text(encoding='utf-8'))
    if data['schema']!=1 or data['id']!=key:raise ValueError('starter_profile_invalid')
    prompts._validate(data['prompts'])
    return data


def catalog():
    return [{'id':key,'name':data['name'],'documents':len(data['knowledge']),'notice':data['notice']}
            for key in IDS for data in [load(key)]]


def body(data):
    return {k:data[k] for k in ('name','description','persona','address_names')}|{
        'voice':data['voice']|{'voice_enabled':False,'voice_authorized':False}}


def populate(root,data):
    """Only fill missing seed files; resumed installation keeps existing edits."""
    files={'persona.md':data['persona'],
        'prompts.json':json.dumps({'schema':2,'defaults':data['prompts'],'overrides':{},'defaults_saved_at':time.time()},ensure_ascii=False),
        'conversation.json':json.dumps(data['conversation'],ensure_ascii=False),
        'role_activity.json':json.dumps(data['activity']|{'enabled':False,'since':0},ensure_ascii=False),
        'assets/manifest.json':'{}'}
    for name,text in files.items():
        path=root/'content'/name
        if not path.exists():private_write(path,text)
    with lore._store(root).locked():
        records=[];offset=0
        while True:
            page=lore.list_documents(root,limit=200,offset=offset);records.extend(page['documents'])
            offset+=len(page['documents'])
            if offset>=page['total']:break
        names={r['name'] for r in records}
        missing=[d for d in data['knowledge'] if d['name'] not in names]
        for i in range(0,len(missing),20):lore.import_documents(root,missing[i:i+20])


def register_sources(root,data):
    from .bilibili import BilibiliSubscriptions
    from .source_catalog import remember,validate_key
    service=BilibiliSubscriptions(root)
    for key in data['activity']['sources']:
        kind,number=validate_key(key)
        remember(service,kind,number)


def import_profile(store,key):
    data=load(key)
    register_sources(store.root,data)
    # ProfileStore publishes the manifest only after all content is written.
    return store.create(body(data),initialize=lambda root:populate(root,data))


def initialize(root):
    """Fresh releases start with two roles; upgrades never replace existing roles.

    The marker permits resuming interrupted first-run imports. Each content
    write and the final manifest are atomic, and present files stay untouched.
    """
    from .profiles import ProfileStore
    from .host import file_locks
    root=Path(root);(root/'data').mkdir(parents=True,exist_ok=True)
    with (root/'data/starter-profiles.lock').open('a') as lock:
        file_locks.flock(lock,file_locks.LOCK_EX)
        manifest=root/'data/profiles.json';marker=root/'data/starter-profiles-pending.json'
        if manifest.exists():return False
        if (root/'content/persona.md').exists() and not marker.exists():return False
        private_write(marker,'{"schema":1}\n')
        rows=[];now=time.time()
        for key,profile_id in [('azusa','default'),('hazel',HAZEL_ID)]:
            data=load(key);directory=root if profile_id=='default' else root/'data/profiles'/profile_id
            populate(directory,data);register_sources(root,data)
            rows.append({'id':profile_id,'name':data['name'],'description':data['description'],
                'created_at':now,'updated_at':now,'address_names':ProfileStore._address_names(data['address_names']),
                'voice':ProfileStore._voice(body(data)['voice'])})
        private_write(manifest,json.dumps({'schema':1,'active_id':'default','profiles':rows},ensure_ascii=False,indent=2)+'\n')
        marker.unlink(missing_ok=True)
        return True
