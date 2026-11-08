"""Named memory policies shared by profiles within one QQ account; no member data."""
from pathlib import Path
import json
import re
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .memory_policies import FIELDS, revision, validate_values, save_policy, describe as describe_policy
from .settings import private_write, settings_lock

MAX_PRESETS = 30


def path(root):
    target=Path(root)/'config/memory-presets.json'
    if target.parent.is_symlink() or target.is_symlink():raise ValueError('memory_presets_path_invalid')
    return target


def read(root):
    target=path(root)
    if not target.exists():return {'version':1,'presets':[]}
    if target.stat().st_size>250000:raise ValueError('memory_presets_invalid')
    data=json.loads(target.read_text())
    if not isinstance(data,dict) or set(data)!={'version','presets'} or data['version']!=1:
        raise ValueError('memory_presets_invalid')
    rows=data['presets']
    if not isinstance(rows,list) or len(rows)>MAX_PRESETS:raise ValueError('memory_presets_invalid')
    for row in rows:
        if (not isinstance(row,dict) or not re.fullmatch('[a-f0-9]{32}',str(row.get('id','')))
            or not isinstance(row.get('settings'),dict) or set(row['settings'])-FIELDS):
            raise ValueError('memory_presets_invalid')
    return data


class PresetInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    name: str = Field(min_length=1,max_length=60)
    settings: dict
    revision: str = Field(max_length=64)


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(min_length=1, max_length=512)
    memory_revision: str = Field(max_length=64)
    preset_revision: str = Field(max_length=64)


def create_router(engine):
    router=APIRouter(prefix='/admin/memory-presets')

    def describe(data):
        return {'presets':data['presets'],'revision':revision(data),'max_presets':MAX_PRESETS}

    @router.get('')
    def get():
        with settings_lock:return describe(read(engine.current.root))

    def save(item,number=None):
        current=engine.current;name=item.name.strip()
        if not name or any(ord(c)<32 or ord(c)==127 for c in name):raise HTTPException(422,'preset_name_invalid')
        with settings_lock:
            data=read(current.root)
            if item.revision!=revision(data):raise HTTPException(409,'memory_presets_changed_reload')
            rows=data['presets'];old=next((r for r in rows if r['id']==number),None)
            if number and old is None:raise HTTPException(404,'memory_preset_not_found')
            if any(r['name'].casefold()==name.casefold() and r['id']!=number for r in rows):
                raise HTTPException(409,'memory_preset_name_exists')
            if old is None and len(rows)>=MAX_PRESETS:raise HTTPException(400,'memory_presets_limit')
            values=validate_values(current.settings(),item.settings)
            now=time.time()
            record={'id':number or uuid.uuid4().hex,'name':name,'settings':values,
                    'created_at':old['created_at'] if old else now,'updated_at':now}
            if old:rows[rows.index(old)]=record
            else:rows.append(record)
            private_write(path(current.root),json.dumps(data,ensure_ascii=False,indent=2)+'\n')
            return describe(data)|{'saved_id':record['id']}

    @router.post('')
    def create(item: PresetInput):return save(item)

    @router.patch('/{number}')
    def update(number: str,item: PresetInput):return save(item,number)

    @router.post('/{number}/apply')
    def apply(number: str, item: ApplyInput):
        current = engine.current
        with settings_lock:
            data = read(current.root)
            if item.preset_revision != revision(data):
                raise HTTPException(409, 'memory_presets_changed_reload')
            selected = next((row for row in data['presets'] if row['id'] == number), None)
            if selected is None:
                raise HTTPException(404, 'memory_preset_not_found')
            changed = save_policy(current, item.scope, selected['settings'], item.memory_revision)
            return {'scope':item.scope, 'preset_id':number, 'preset_name':selected['name'],
                    'changed':changed, 'revision':describe_policy(current,item.scope)['revision']}

    @router.delete('/{number}')
    def delete(number: str,revision_token: str):
        with settings_lock:
            root=engine.current.root;data=read(root)
            if revision_token!=revision(data):raise HTTPException(409,'memory_presets_changed_reload')
            if not any(r['id']==number for r in data['presets']):raise HTTPException(404,'memory_preset_not_found')
            data['presets']=[r for r in data['presets'] if r['id']!=number]
            private_write(path(root),json.dumps(data,ensure_ascii=False,indent=2)+'\n')
            return describe(data)

    return router
