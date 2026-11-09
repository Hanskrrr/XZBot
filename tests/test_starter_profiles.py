"""Public templates and native first launch; no real credentials or network."""
import json
import re
from pathlib import Path
import sys

import pytest

from azusa_bot import starter_templates as templates,lore,prompts
from azusa_bot.profiles import ProfileStore
from azusa_bot.settings import Settings,write_settings,private_write
from test_knowledge_admin import library


def test_public_template_boundary():
    for key in templates.IDS:
        data=templates.load(key)
        assert set(data)=={'schema','id','name','description','persona','address_names','voice','prompts','conversation','knowledge','activity','notice'}
        assert set(data['voice'])=={'fish_reference_id','fish_model'}
        assert set(data['activity'])=={'sources','clip_keywords'}
        raw=json.dumps(data,ensure_ascii=False)
        assert not re.search(r'/Users/|\bsk-[A-Za-z0-9_-]{24,}|\bSUB=|onebot_owners|onebot_self_id|allowed_groups',raw)
        assert all(not d.get('activity_source') and d.get('collection')!='角色动态' for d in data['knowledge'])
        names=[d['name'] for d in data['knowledge']]
        assert len(set(names))==len(names)
        assert not templates.body(data)['voice']['voice_authorized']


def test_first_install_has_two_complete_profiles_and_preserves_upgrades(tmp_path):
    write_settings(tmp_path,Settings(read_existing_dsh=False))
    assert templates.initialize(tmp_path)
    store=ProfileStore(tmp_path);rows=store.list()['profiles']
    assert [r['name'] for r in rows]==['阿梓','灰泽满']
    assert store.list()['active_id']=='default'
    for row,key in zip(rows,templates.IDS):
        root=store.directory(row['id']);data=templates.load(key)
        assert (root/'content/persona.md').read_text(encoding='utf-8')==data['persona']
        assert lore.list_documents(root)['total']==len(data['knowledge'])
        assert prompts.describe(root,Settings())['has_saved_defaults']
        assert not row['voice']['voice_enabled'] and not row['voice']['voice_authorized']
        assert json.loads((root/'content/role_activity.json').read_text())['enabled'] is False
    private_write(tmp_path/'content/persona.md','我自己的修改')
    assert templates.initialize(tmp_path) is False
    assert (tmp_path/'content/persona.md').read_text()=='我自己的修改'
    with templates.lore._store(tmp_path).db() as db:assert db.execute('SELECT count(*) FROM documents').fetchone()[0]==285


def test_existing_unmanaged_role_is_not_overwritten(tmp_path):
    private_write(tmp_path/'content/persona.md','已有的私人角色')
    assert not templates.initialize(tmp_path)
    assert not (tmp_path/'data/profiles.json').exists()


def test_interrupted_first_import_resumes_without_duplicates(tmp_path,monkeypatch):
    write_settings(tmp_path,Settings(read_existing_dsh=False));original=templates.populate
    def fail_second(root,data):
        if data['id']=='hazel':raise OSError('synthetic disk error')
        return original(root,data)
    monkeypatch.setattr(templates,'populate',fail_second)
    with pytest.raises(OSError):templates.initialize(tmp_path)
    assert not (tmp_path/'data/profiles.json').exists()
    private_write(tmp_path/'content/persona.md','中断后自行修改，必须保留')
    monkeypatch.setattr(templates,'populate',original)
    assert templates.initialize(tmp_path)
    assert lore.list_documents(tmp_path)['total']==len(templates.load('azusa')['knowledge'])
    assert (tmp_path/'content/persona.md').read_text()=='中断后自行修改，必须保留'


def test_template_import_admin_auth_and_profile_isolation(library):
    root,app,client,headers=library
    assert client.get('/admin/profile-templates').status_code==401
    original=client.get('/admin/profiles',headers=headers).json()
    route='/admin/profile-templates/hazel'
    assert client.post(route,json={}).status_code==401
    assert client.post(route,headers=headers|{'Origin':'https://foreign.invalid'},json={}).status_code==403
    assert client.post(route,headers=headers|{'X-XZBot-Profile':'wrong'},json={}).status_code==409
    imported=client.post(route,headers=headers,json={});assert imported.status_code==200
    rows=client.get('/admin/profiles',headers=headers).json()
    assert rows['active_id']==original['active_id']
    profile=app.state.engine.profiles.directory(imported.json()['id'])
    assert lore.list_documents(profile)['total']==2
    assert lore.list_documents(root)['total']==0
    assert client.post('/admin/profile-templates/unknown',headers=headers,json={}).status_code==404
