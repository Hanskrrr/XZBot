"""Global role boundaries tested with synthetic local data and no network."""
import asyncio
import json
from pathlib import Path
import sqlite3
import time

from fastapi.testclient import TestClient
import pytest

from azusa_bot.app import create_app, backup_project
from azusa_bot.core import Incoming
from azusa_bot.memory import MemoryStore
from azusa_bot.profiles import ProfileEngineManager, ProfileError
from azusa_bot.providers import ProviderError
from azusa_bot.secrets import Credentials
from azusa_bot.settings import Settings, private_write, read_settings, write_settings


SCOPE="demo:dm:alice"

@pytest.fixture
def root(tmp_path):
    root=tmp_path/"bot"
    private_write(root/"content/persona.md","# 旧角色\n只在默认角色出现的专属人设。")
    (root/"content/lore").mkdir()
    (root/"ui").mkdir(); (root/"ui/index.html").write_text("<title>XZBot</title>")
    write_settings(root,Settings(min_interval_seconds=0,fish_reference_id="old-reference",fish_model="old-model",fish_model_override=True))
    yield root


def event(text="你好",event_id="event"):
    return Incoming(app_id="demo",kind="dm",scope_id="alice",user_id="alice",text=text,event_id=event_id)


def test_default_migration_preserves_existing_files_and_counts(root):
    memory=MemoryStore(root/"data/memory.sqlite3")
    memory.set_consent(SCOPE,"alice",True)
    memory.add_history(SCOPE,"alice","user","旧对话","old")
    memory.add_memory(SCOPE,"alice","默认专属偏好")
    before=memory.stats();memory.close()
    persona=(root/"content/persona.md").read_bytes()
    manager=ProfileEngineManager(root)
    assert manager.profile_id=="default"
    assert manager.memory.stats()==before
    assert (root/"content/persona.md").read_bytes()==persona
    assert manager.settings().fish_reference_id=="old-reference"
    manager.close()


def test_default_profile_snapshots_effective_legacy_voice_before_profile_edit(root,monkeypatch):
    write_settings(root,Settings(fish_model="unused-default",fish_model_override=False,read_existing_dsh=True))
    monkeypatch.setattr("azusa_bot.secrets.load_credentials",lambda _:Credentials(fish_model="working-legacy-model",fish_reference="working-legacy-reference"))
    manager=ProfileEngineManager(root)
    voice=manager.profiles.list()["profiles"][0]["voice"]
    assert voice["fish_model"]=="working-legacy-model" and voice["fish_reference_id"]=="working-legacy-reference"
    manager.profiles.update("default",{"name":"重命名","voice":voice})
    assert manager.settings().fish_model=="working-legacy-model" and manager.settings().fish_model_override
    manager.close()


@pytest.mark.asyncio
async def test_profile_context_knowledge_memory_voice_and_consent_are_isolated(root):
    from azusa_bot import lore
    manager=ProfileEngineManager(root)
    manager.memory.set_consent(SCOPE,"alice",True)
    manager.memory.add_memory(SCOPE,"alice","旧角色独有的猫偏好")
    lore.import_documents(root,[{"name":"old.md","text":"旧角色独有的猫背景。","category":"fiction"}])
    manager.groups.add("demo:group:g","alice","旧角色短期上下文","g0")
    new=manager.profiles.create({"name":"新角色","persona":"# 新角色\n新角色只说自己的设定。","voice":{"fish_reference_id":"new-reference","fish_model":"new-model"}})
    manager.activate(new["id"])
    assert not manager.memory.consent(SCOPE,"alice")
    assert manager.memory.stats()["memories"]==0
    assert manager.groups.snapshot("demo:group:g")==[]
    assert lore.search(manager.content_root,"猫")==[]
    assert manager.settings().fish_reference_id=="new-reference"
    provider=manager.providers(manager.settings().model_copy(update={"provider_mode":"cloud"}))
    assert provider.credentials.fish_reference=="new-reference" and provider.fish_model=="new-model"
    manager.memory.add_memory(SCOPE,"alice","新角色独有的狗偏好")
    manager.activate("default")
    assert manager.memory.consent(SCOPE,"alice")
    assert [r["text"] for r in manager.memory.retrieve(SCOPE,"alice","猫")]==["旧角色独有的猫偏好"]
    assert manager.memory.retrieve(SCOPE,"alice","狗")==[]
    assert manager.groups.snapshot("demo:group:g")==[]
    assert manager.settings().fish_reference_id=="old-reference"
    manager.close()


@pytest.mark.asyncio
async def test_switch_during_generation_discards_old_reply_and_never_writes_new_role(root):
    manager=ProfileEngineManager(root)
    manager.memory.set_consent(SCOPE,"alice",True)
    entered,release=asyncio.Event(),asyncio.Event()
    class Slow:
        async def complete(self,*args,**kwargs):
            entered.set(); await release.wait(); return {"text":"旧角色的输出","mode":"text"}
    manager.provider_override=Slow()
    pending=asyncio.create_task(manager.handle(event(),demo=True))
    await asyncio.wait_for(entered.wait(),2)
    role=manager.profiles.create({"name":"Second"})
    manager.activate(role["id"])
    release.set();reply=await pending
    assert reply["mode"]=="none" and reply["reason"]=="profile_changed"
    assert manager.memory.stats()["history"]==0
    manager.activate("default")
    records=manager.memory.recent_history(SCOPE,"alice")
    assert len(records)==1 and records[0]["role"]=="user"
    manager.close()


def test_old_prepared_reply_cannot_be_authorized_after_switch_or_restart(root):
    manager=ProfileEngineManager(root)
    reply=manager.ledger.reserve(event(),SCOPE,manager.profile_id,manager.profile_epoch)
    manager.ledger.mark(reply,"prepared","text")
    assert manager.authorize_reply(reply)
    new=manager.profiles.create({"name":"Second"});manager.activate(new["id"])
    assert not manager.authorize_reply(reply)
    manager.activate("default")
    assert not manager.authorize_reply(reply)
    other=manager.ledger.reserve(event(event_id="second"),SCOPE,manager.profile_id,manager.profile_epoch)
    manager.ledger.mark(other,"prepared","text")
    manager.close()
    restarted=ProfileEngineManager(root)
    assert not restarted.authorize_reply(other)
    restarted.close()


@pytest.mark.asyncio
async def test_cloud_failure_and_backoff_still_record_only_consented_observation(root):
    manager=ProfileEngineManager(root)
    write_settings(root,read_settings(root).model_copy(update={"provider_mode":"cloud"}))
    manager.memory.set_consent(SCOPE,"alice",True)
    class Broken:
        async def complete(self,*args,**kwargs):
            raise ProviderError("deepseek_http_503")
    manager.provider_override=Broken()
    assert (await manager.handle(event("已同意的第一条","one"),demo=True))["reason"]=="cloud_failure"
    assert (await manager.handle(event("退避期间第二条","two"),demo=True))["reason"]=="cloud_backoff"
    rows=manager.memory.recent_history(SCOPE,"alice")
    assert [r["text"] for r in rows]==["已同意的第一条","退避期间第二条"]
    assert all(r["role"]=="user" for r in rows)
    manager.close()


def test_admin_profiles_active_apis_stale_tab_and_delete_guards(root):
    app=create_app(root)
    headers={"Authorization":"Bearer "+(root/"data/admin.token").read_text(),"X-XZBot-Profile":"default"}
    with TestClient(app) as client:
        assert client.get("/admin/profiles").status_code==401
        listing=client.get("/admin/profiles",headers=headers).json()
        assert listing["active_id"]=="default" and len(listing["profiles"])==1
        first=client.post("/admin/profiles",headers=headers,json={"name":"角色乙","voice":{"fish_reference_id":"voice-b"}})
        assert first.status_code==200
        second=first.json()["id"]
        switched=client.post(f"/admin/profiles/{second}/activate",headers=headers,json={})
        assert switched.status_code==200 and switched.json()["active_id"]==second
        assert client.put("/admin/persona",headers=headers,json={"text":"这段旧页面输入不该覆盖乙角色。"}).status_code==409
        current={**headers,"X-XZBot-Profile":second}
        assert client.put("/admin/persona",headers=current,json={"text":"# 角色乙\n这是角色乙自己的完整人设。"}).status_code==200
        assert "角色乙" in client.get("/admin/persona",headers=current).json()["text"]
        assert client.delete(f"/admin/profiles/{second}",headers=current).status_code==409
        client.post("/admin/profiles/default/activate",headers=current,json={})
        assert "旧角色" in client.get("/admin/persona",headers=headers).json()["text"]
        assert client.delete(f"/admin/profiles/{second}",headers=headers).status_code==200
        assert client.delete("/admin/profiles/default",headers=headers).status_code==409
        assert not (root/"data/profiles"/second).exists()
    app.state.engine.close()


def test_provider_voice_edits_stay_in_profile_and_credentials_stay_global(root):
    app=create_app(root)
    headers={"Authorization":"Bearer "+(root/"data/admin.token").read_text()}
    with TestClient(app) as client:
        role=client.post("/admin/profiles",headers=headers,json={"name":"乙"}).json()["id"]
        client.post(f"/admin/profiles/{role}/activate",headers=headers,json={})
        saved=client.put("/admin/providers",headers=headers,json={"settings":{"fish_reference_id":"voice-b","fish_model":"model-b","deepseek_model":"shared-model"},"secrets":{"fish_key":"synthetic-local-test"}})
        assert saved.status_code==200 and "synthetic-local-test" not in saved.text
        assert saved.json()["effective"]=={"fish_reference_id":"voice-b","fish_model":"model-b"}
        assert read_settings(root).fish_reference_id=="old-reference"
        client.post("/admin/profiles/default/activate",headers=headers,json={})
        old=client.get("/admin/providers",headers=headers).json()
        assert old["effective"]=={"fish_reference_id":"old-reference","fish_model":"old-model"}
        assert old["settings"]["deepseek_model"]=="shared-model" and old["credentials"]["fish"]["configured"]
    app.state.engine.close()


def test_blank_new_profile_does_not_inherit_environment_voice(root,monkeypatch):
    monkeypatch.setenv("FISH_REFERENCE_ID","legacy-env-reference")
    monkeypatch.setenv("FISH_MODEL","legacy-env-model")
    manager=ProfileEngineManager(root)
    role=manager.profiles.create({"name":"空白"});manager.activate(role["id"])
    provider=manager.providers(manager.settings().model_copy(update={"provider_mode":"cloud"}))
    assert provider.credentials.fish_reference=="" and provider.fish_model=="s2.1-pro"
    manager.close()


def test_backup_includes_all_profiles_without_mixing_default_store(root):
    manager=ProfileEngineManager(root)
    manager.memory.add_memory(SCOPE,"alice","默认专属")
    role=manager.profiles.create({"name":"乙"});manager.activate(role["id"])
    manager.memory.add_memory(SCOPE,"alice","乙专属")
    backup=backup_project(root,manager)
    with sqlite3.connect(backup/"memory.sqlite3") as db:
        assert [r[0] for r in db.execute("SELECT text FROM memories")]==["默认专属"]
    with sqlite3.connect(backup/"profiles"/role["id"]/"memory.sqlite3") as db:
        assert [r[0] for r in db.execute("SELECT text FROM memories")]==["乙专属"]
    assert json.loads((backup/"profiles.json").read_text())["active_id"]==role["id"]
    assert not list(backup.rglob("*.token"))
    manager.close()


def test_profile_directory_and_schema_reject_traversal_and_unknown_fields(root):
    manager=ProfileEngineManager(root)
    with pytest.raises(ProfileError): manager.profiles.create({"name":"a","api_key":"x"})
    with pytest.raises(ProfileError): manager.profiles.directory("../escape")
    role=manager.profiles.create({"name":"a"})
    destination=root/"data/profiles"/role["id"]
    import shutil
    shutil.rmtree(destination);destination.symlink_to(root,target_is_directory=True)
    with pytest.raises(ProfileError): manager.activate(role["id"])
    manager.close()
