"""Reuse saved public source identities, without copying credentials or delivery state."""
from contextlib import closing
import sqlite3
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .accounts import AccountRegistry
from .bilibili import MAX_BATCH_TARGETS, MAX_SUBSCRIPTIONS
from .settings import read_settings, settings_lock
from .subscription_sources import TYPES, source_id
from . import source_catalog


def saved_sources(root):
    registry = AccountRegistry(root)
    settings = read_settings(root)
    allowed = {('group', value) for value in settings.onebot_allowed_groups}
    allowed |= {('dm', value) for value in settings.onebot_allowed_dm_users if value != settings.onebot_self_id}
    result, unavailable = [], []
    for account in [{'id': 'primary', 'name': '主账号'}, *registry.read()]:
        _, directory = registry.get(account['id'])
        path = directory / 'data/bilibili.sqlite3'
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or path.parent.is_symlink():
            unavailable.append(account['name'])
            continue
        try:
            # No foreign service/Engine is instantiated; this must remain read-only.
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute('''SELECT s.id,s.kind,s.target_id,s.include_image,
                    r.room_id,r.uid,r.name,r.type FROM rooms r
                    LEFT JOIN subscriptions s USING(room_id) ORDER BY r.type,r.room_id,s.created_at,s.id LIMIT ?''', (MAX_SUBSCRIPTIONS+source_catalog.MAX_SOURCES,)).fetchall()
            grouped = {}
            for row in rows:
                kind = row['type']
                if kind not in TYPES:
                    continue
                source = source_id(kind, row['room_id'] if kind == 'bilibili_live' else row['uid'])
                key = kind + ':' + source
                item = grouped.setdefault(key, {
                    'account_id': account['id'], 'account_name': account['name'],
                    'subscription_id': row['id'], 'source_key':key, 'type': kind, 'source_id': source,
                    'uid': str(row['uid'] or ''),
                    'name': str(row['name'] or '')[:80], 'include_image': bool(row['include_image']) if row['include_image'] is not None else True,
                    'matching_targets': [],
                })
                target = (row['kind'], row['target_id'])
                if target in allowed:
                    value = {'kind': target[0], 'target_id': target[1]}
                    if value not in item['matching_targets']:
                        item['matching_targets'].append(value)
            result.extend(grouped.values())
        except (sqlite3.Error, ValueError, TypeError):
            unavailable.append(account['name'])
    return {'sources': result, 'unavailable_accounts': unavailable}


class Target(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str = Field(pattern=r'^(group|dm)$')
    target_id: str = Field(pattern=r'^[1-9][0-9]{0,19}$')


class ReuseInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_id: str = Field(pattern=r'^(primary|[a-f0-9]{32})$')
    subscription_id: str | None = Field(default=None,pattern=r'^[a-f0-9]{32}$')
    source_key: str | None = Field(default=None,max_length=80)
    targets: list[Target] = Field(min_length=1, max_length=MAX_BATCH_TARGETS)
    include_image: bool = Field(strict=True)

    @model_validator(mode='after')
    def one_source(self):
        if bool(self.subscription_id)==bool(self.source_key):raise ValueError('select_one_source')
        if self.source_key:source_catalog.validate_key(self.source_key)
        return self


async def reuse(service, item):
    async with service._operations:
        catalog = saved_sources(service.root)
        source = next((row for row in catalog['sources'] if row['account_id'] == item.account_id
                       and (row['source_key']==item.source_key if item.source_key else row['subscription_id'] == item.subscription_id)), None)
        if source is None:
            raise HTTPException(409, 'subscription_library_source_changed')
        kind, number = source['type'], source['source_id']
        room = source_catalog.room_id(kind,number)
        with settings_lock, service.db() as db:
            identities = list(dict.fromkeys(service._target(target.kind, target.target_id) for target in item.targets))
            db.execute('BEGIN IMMEDIATE')
            existing = {tuple(row) for row in db.execute(
                'SELECT self_id,kind,target_id FROM subscriptions WHERE room_id=?', (room,))}
            additions = [identity for identity in identities if identity not in existing]
            if db.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] + len(additions) > MAX_SUBSCRIPTIONS:
                raise HTTPException(400, 'bilibili_subscription_limit')
            created = []
            if additions:
                # Unknown until explicitly enabled and refreshed by the destination's provider.
                # Never import source cookies, payloads, stale sessions or pending notifications.
                db.execute('''INSERT OR IGNORE INTO rooms
                    (room_id,uid,name,title,live_status,session,last_check,next_poll,type,payload)
                    VALUES (?,?,?,'',0,'',0,0,?,'{}')''', (room, source['uid'], source['name'], kind))
                now = service.clock()
                for identity in additions:
                    new_id = uuid.uuid4().hex
                    db.execute('''INSERT INTO subscriptions
                        (id,room_id,self_id,kind,target_id,enabled,last_status,last_session,revision,created_at,updated_at,include_image)
                        VALUES (?,?,?,?,?,0,0,'',1,?,?,?)''',
                        (new_id, room, *identity, now, now, int(item.include_image)))
                    created.append(new_id)
        return {'created_count': len(created), 'existing_count': len(identities)-len(created),
                'paused': True, 'subscriptions': [row for row in service.list() if row['id'] in created]}


class SourceInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_id: str | None = Field(default=None,pattern=r'^(primary|[a-f0-9]{32})$')
    source_key: str | None = Field(default=None,max_length=80)
    type: str | None = None
    as_type: str | None = None
    input_type: str = 'uid'
    value: str | None = Field(default=None,max_length=300)
    name: str = Field(default='',max_length=80)

    @model_validator(mode='after')
    def valid_source(self):
        if self.account_id:
            source_kind,_=source_catalog.validate_key(self.source_key)
            if self.as_type and not (self.as_type=='bilibili_video' and source_kind=='bilibili_dynamic'):raise ValueError('source_conversion_invalid')
            if self.type or self.value:raise ValueError('source_fields_invalid')
        elif self.as_type or self.source_key or self.type not in TYPES or not self.value:
            raise ValueError('source_fields_invalid')
        return self


class RefreshInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    sources: list[str] = Field(min_length=1,max_length=20)
    notify: bool = Field(default=False,strict=True)


class SourceEditInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    source_id: str = Field(min_length=1,max_length=300)
    name: str = Field(default='',max_length=80)
    revision: int = Field(ge=1,strict=True)


def create_router(service, profiles):
    from . import subscription_edit, subscription_journal
    subscription_edit.initialize(service)
    try:subscription_edit.recover(service,profiles)
    except (OSError,ValueError):pass  # Report pending recovery explicitly in the catalog.
    router = APIRouter(prefix='/admin/subscriptions')

    @router.post('/refresh')
    async def refresh(item: RefreshInput):
        try:return await service.refresh(item.sources,notify=item.notify)
        except ValueError as error:
            raise HTTPException(409,str(error) if str(error).startswith('subscription_') else 'subscription_refresh_invalid') from None

    @router.get('/runs')
    def runs(source: str | None=None,before: int | None=None,limit:int=30):
        try:return subscription_journal.list_runs(service,source=source,before=before,limit=limit)
        except ValueError:raise HTTPException(400,'subscription_journal_query_invalid') from None

    @router.patch('/sources/{key}')
    async def edit_source(key:str,item:SourceEditInput):
        try:return await subscription_edit.update(service,profiles,key,item.model_dump())
        except (ValueError,OSError) as error:
            code=str(error)
            raise HTTPException(409,code if code.startswith('subscription_') and len(code)<100 else 'subscription_source_edit_incomplete') from None

    @router.post('/recover-edits')
    async def recover_edits():
        async with service._operations:
            try:subscription_edit.recover(service,profiles)
            except (OSError,ValueError):raise HTTPException(409,'subscription_source_edit_conflict') from None
        return {'recovered':True}

    @router.get('/library')
    def library():
        return saved_sources(service.root)

    @router.get('/sources')
    def sources():
        with service.db() as db:pending=db.execute('SELECT COUNT(*) FROM subscription_source_edits').fetchone()[0]
        return source_catalog.describe(service)|{'pending_edits':pending}

    @router.post('/sources')
    async def add_source(item: SourceInput):
        async with service._operations:
            if item.account_id:
                source=next((r for r in saved_sources(service.root)['sources'] if r['account_id']==item.account_id and r['source_key']==item.source_key),None)
                if source is None:raise HTTPException(409,'subscription_library_source_changed')
                return source_catalog.remember(service,item.as_type or source['type'],source['source_id'],name=source['name'],uid=source['uid'])
            if item.type=='bilibili_live':
                info=await service.resolve({'type':item.type,'input_type':item.input_type,'value':item.value})
                # Live room aliases and UP UIDs must resolve to the canonical room.
                return source_catalog.remember(service,item.type,info['room_id'],name=item.name or info['name'],uid=info['uid'])
            number=source_id(item.type,item.value)
            return source_catalog.remember(service,item.type,number,name=item.name)

    @router.delete('/sources/{key}')
    async def delete_source(key:str):
        async with service._operations:
            return source_catalog.remove(service,key)

    @router.post('/reuse')
    async def copy(item: ReuseInput):
        return await reuse(service, item)

    return router
