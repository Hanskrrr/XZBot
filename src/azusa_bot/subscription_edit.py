"""Edit a source identity while retaining its consumers and historical provenance.

SQLite commits the identity change and a small profile-config redo journal
together. Profile writes are atomic and replayable after an interrupted save.
"""
from contextlib import ExitStack
import json
import uuid

from . import lore, role_activity, source_catalog
from .group_chat import GroupConversation
from .subscription_sources import source_id


def initialize(service):
    with service.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS subscription_source_edits (id TEXT PRIMARY KEY, edits TEXT NOT NULL)')


def recover(service, profiles):
    with profiles.lock:
        with service.db() as db:pending=list(db.execute('SELECT id,edits FROM subscription_source_edits'))
        for number, raw in pending:
            changes=json.loads(raw)
            existing={row['id'] for row in profiles.list()['profiles']}
            for change in changes:
                if change['profile_id'] not in existing:continue
                root=profiles.directory(change['profile_id'])
                with lore._store(root).locked():
                    current=role_activity.config(root)
                    if role_activity.revision(current)==role_activity.revision(change['after']):continue
                    after=change['after']
                    if role_activity.revision(current)!=change['before']:
                        old_key,new_key=change['old_key'],change['new_key']
                        if old_key not in current['sources']:continue  # Already moved or deliberately unbound.
                        if new_key in current['sources']:raise ValueError('subscription_source_edit_conflict')
                        after=current|{'sources':[new_key if k==old_key else k for k in current['sources']]}
                        filters=dict(current.get('clip_keywords',{}))
                        if old_key in filters:filters[new_key]=filters.pop(old_key)
                        after['clip_keywords']=filters
                    lore._atomic(root/'content/role_activity.json',json.dumps(after,ensure_ascii=False).encode())
            with service.db() as db:db.execute('DELETE FROM subscription_source_edits WHERE id=?',(number,))


async def update(service, profiles, key, body):
    kind, old_number=source_catalog.validate_key(key)
    if (not isinstance(body,dict) or set(body)!={'source_id','name','revision'}
            or type(body['revision']) is not int or body['revision']<1
            or not isinstance(body['name'],str) or len(body['name'])>80 or '\x00' in body['name']
            or GroupConversation.contains_credentials(body['name'])):
        raise ValueError('subscription_source_edit_invalid')
    number=source_id(kind,body['source_id'])
    new_key=kind+':'+number;old_room=source_catalog.room_id(kind,old_number);new_room=source_catalog.room_id(kind,number)
    label=body['name'].strip()
    async with service._operations:
        live_uid=''
        if kind=='bilibili_live' and new_key!=key:
            info=await service.resolve({'type':kind,'input_type':'room','value':number})
            number=info['room_id'];live_uid=info['uid']
            new_key=kind+':'+number;new_room=number
        with profiles.lock, ExitStack() as locks:
            recover(service,profiles)
            changes=[]
            if new_key!=key:
                for profile in profiles.list()['profiles']:
                    root=profiles.directory(profile['id'])
                    locks.enter_context(lore._store(root).locked())
                    cfg=role_activity.config(root)
                    if key not in cfg['sources']:continue
                    if new_key in cfg['sources']:raise ValueError('subscription_source_exists')
                    new=cfg|{'sources':[new_key if k==key else k for k in cfg['sources']]}
                    filters=dict(cfg.get('clip_keywords',{}))
                    if key in filters:filters[new_key]=filters.pop(key)
                    new['clip_keywords']=filters
                    changes.append({'profile_id':profile['id'],'before':role_activity.revision(cfg),'after':new,'old_key':key,'new_key':new_key})
            with service.db() as db:
                db.execute('BEGIN IMMEDIATE')
                old=db.execute('SELECT * FROM rooms WHERE room_id=?',(old_room,)).fetchone()
                if old is None:raise ValueError('subscription_source_not_found')
                if old['source_revision']!=body['revision']:raise ValueError('subscription_source_changed_reload')
                if new_room!=old_room and db.execute('SELECT 1 FROM rooms WHERE room_id=?',(new_room,)).fetchone():
                    raise ValueError('subscription_source_exists')
                if new_key!=key:
                    # Freeze old provenance before moving the live source identity.
                    db.execute("UPDATE outbox SET source_key=?,source_name=? WHERE room_id=? AND source_key=''",(key,old['name'],old_room))
                    db.execute("UPDATE outbox SET status='cancelled',completed_at=? WHERE room_id=? AND status IN ('pending','claimed')",(service.clock(),old_room))
                    db.execute("UPDATE subscriptions SET room_id=?,revision=revision+1,needs_rebaseline=1,last_status=0,last_session='',updated_at=? WHERE room_id=?",(new_room,service.clock(),old_room))
                    db.execute("""UPDATE rooms SET room_id=?,uid=?,name=?,name_override=?,source_revision=source_revision+1,
                        title='',session='',live_status=0,last_check=0,next_poll=0,failures=0,last_error='',payload='{}' WHERE room_id=?""",
                        (new_room,live_uid if kind=='bilibili_live' else number,label,label,old_room))
                else:
                    db.execute('UPDATE rooms SET name=?,name_override=?,source_revision=source_revision+1 WHERE room_id=?',
                               (label or (old['name'] if not old['name_override'] else ''),label,old_room))
                if changes:
                    db.execute('INSERT INTO subscription_source_edits VALUES (?,?)',(uuid.uuid4().hex,json.dumps(changes)))
            # A failed file write leaves the redo journal for retry/startup.
            recover(service,profiles)
    return {'key':new_key,'identity_changed':new_key!=key,'roles_updated':len(changes)}
