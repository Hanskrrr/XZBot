"""Private export/restore tests use synthetic accounts and fake credentials only."""
import json
import os
from pathlib import Path
import sqlite3
import zipfile
import pytest
from azusa_bot.migration import export_bundle,restore_bundle,verify_bundle
from azusa_bot.portable_credentials import PrivateFileBackend,MARKER
from azusa_bot.credential_store import CredentialStore,CredentialStoreError
from azusa_bot.settings import Settings,write_settings,private_write
from azusa_bot.memory import MemoryStore
from azusa_bot.bot_control import read_control
from azusa_bot.accounts import AccountRegistry

# Capture production implementation before autouse replaces OS credential I/O.
REAL_BACKEND=CredentialStore.backend
SECOND='b'*32
PROFILE='c'*32
SECRET='synthetic-private-export-value'


def make_source(tmp_path):
 root=tmp_path/'source';root.mkdir()
 write_settings(root,Settings(transport='onebot',onebot_self_id='10001',onebot_allowed_groups=['30001'],read_existing_dsh=False))
 private_write(root/'config/example.json','{}')
 private_write(root/'docs/handoffs/muse-private-migration.md','# 合成迁移说明')
 private_write(root/'src/module.py','x=1')
 private_write(root/'README.md','# 合成项目')
 for directory in [root,root/'accounts'/SECOND]:
  (directory/'data').mkdir(mode=0o700,parents=True,exist_ok=True);(directory/'data').chmod(0o700)
  write_settings(directory,Settings(transport='onebot',onebot_self_id='10001' if directory==root else '10002',onebot_allowed_groups=['30001'],fish_reference_id='synthetic-voice'))
  private_write(directory/'config/group-presets.json','{"version":1,"presets":[]}')
  private_write(directory/'config/runtime.json','{"mode":"lima"}')
  private_write(directory/'config/memory-presets.json','{"version":1,"presets":[]}')
  private_write(directory/'content/persona.md','独立角色文本')
  private_write(directory/'content/prompts.json','{"synthetic":"角色提示词"}')
  private_write(directory/'content/conversation.json','{"synthetic":"预设对话"}')
  private_write(directory/'data/profiles.json',json.dumps({'schema':1,'active_id':'default','profiles':[{'id':'default'},{'id':PROFILE}]}))
  private_write(directory/'data/admin.token',SECRET)
  private_write(directory/'data/profiles'/PROFILE/'content/persona.md','额外的合成角色')
  for p in [directory,directory/'data/profiles'/PROFILE]:
   m=MemoryStore(p/'data/memory.sqlite3');m.set_consent('onebot:10001:group:30001','20001',True)
   m.add_history('onebot:10001:group:30001','20001','user','用于迁移验证的记录','synthetic-1');m.close()
 private_write(root/'accounts'/SECOND/'config/instance.json',json.dumps({'schema':1,'slot':1,'manager_root':str(root)}))
 private_write(root/'data/accounts.json',json.dumps({'schema':1,'accounts':[{'id':SECOND,'slot':1,'name':'合成次账号'}]}))
 private_write(root/'data/core.log','excluded')
 private_write(root/'runtime/qq-login','excluded')
 return root


def fake_credentials(root):
 return {'schema':1,'entries':[{'name':'deepseek_key','value':SECRET,'base_url':''},{'name':'compatible_key','value':SECRET+'-compatible','base_url':'https://example.test/v1'}],
         'effective_voice':{'fish_model':'effective-legacy-model','fish_reference_id':'effective-voice'},'legacy_official':{}}


def test_roundtrip_includes_every_profile_credential_and_preserves_source(tmp_path,monkeypatch):
 root=make_source(tmp_path);output=tmp_path/'private.zip'
 result=export_bundle(root,output,credential_reader=fake_credentials)
 assert SECRET not in json.dumps(result) and result['accounts']==2 and result['profiles']==4
 assert output.stat().st_mode&0o077==0
 with zipfile.ZipFile(output) as z:
  assert z.testzip() is None
  assert not any('/runtime/qq-login' in n or n.endswith('.log') or '-wal' in n for n in z.namelist())
  z.extractall(tmp_path/'unpacked')
 bundle=tmp_path/'unpacked/XZBot-muse';assert verify_bundle(bundle)['contains_secrets']
 monkeypatch.setattr('azusa_bot.migration.platform.system',lambda:'Linux')
 dest=tmp_path/'restored';restored=restore_bundle(bundle,dest)
 assert restored['all_accounts_paused'] and SECRET not in json.dumps(restored)
 assert AccountRegistry(dest).get(SECOND)[1]==dest/'accounts'/SECOND
 monkeypatch.setattr(CredentialStore,'backend',REAL_BACKEND)
 for directory in [dest,dest/'accounts'/SECOND]:
  assert not read_control(directory)['enabled']
  assert CredentialStore(directory).get('deepseek_key')==SECRET
  assert CredentialStore(directory).get('compatible_key','https://example.test/v1')==SECRET+'-compatible'
  assert not CredentialStore(directory).get('compatible_key','https://different.test/v1')
  for p in [directory,directory/'data/profiles'/PROFILE]:
   with sqlite3.connect(p/'data/memory.sqlite3') as db:assert db.execute('select count(*) from history').fetchone()[0]==1
  assert (directory/'config/group-presets.json').exists()
  assert (directory/'config/memory-presets.json').exists()
  assert json.loads((directory/'config/runtime.json').read_text())=={'mode':'external'}
  settings=json.loads((directory/'config/local.json').read_text())
  assert settings['fish_model']=='effective-legacy-model' and settings['fish_model_override']
  assert settings['fish_reference_id']=='effective-voice'
  assert (directory/'content/prompts.json').read_text()=='{"synthetic":"角色提示词"}'
  assert (directory/'content/conversation.json').read_text()=='{"synthetic":"预设对话"}'
  assert (directory/'data/admin.token').read_text()==SECRET
 assert read_control(root)['enabled']  # Export did not stop the original.
 with pytest.raises(ValueError,match='destination_exists'):restore_bundle(bundle,dest)
 (bundle/'project/README.md').write_text('changed')
 with pytest.raises(ValueError,match='integrity_failed'):restore_bundle(bundle,tmp_path/'other')
 assert not (tmp_path/'other').exists()


def test_private_backend_explicit_marker_permissions_and_no_secret_errors(tmp_path,monkeypatch):
 (tmp_path/'data').mkdir(mode=0o700)
 private_write(tmp_path/'data/credential-backend.json',json.dumps(MARKER))
 monkeypatch.setattr(CredentialStore,'backend',REAL_BACKEND)
 store=CredentialStore(tmp_path);assert store.storage_kind()=='private_file'
 store.set('bilibili_buvid3',SECRET);assert store.get('bilibili_buvid3')==SECRET
 store.set('fish_key',SECRET);assert store.get('fish_key')==SECRET
 store.set('fish_key','');assert store.get('fish_key')==''
 store.set('fish_key',SECRET)
 (tmp_path/'data/portable-credentials.json').chmod(0o644)
 with pytest.raises(CredentialStoreError) as exc:store.get('fish_key')
 assert SECRET not in str(exc.value)
 (tmp_path/'data/portable-credentials.json').unlink()
 outside=tmp_path/'outside';outside.write_text('{}');(tmp_path/'data/portable-credentials.json').symlink_to(outside)
 with pytest.raises(CredentialStoreError):store.get('fish_key')
 assert outside.read_text()=='{}'


def test_export_rejects_content_symlinks(tmp_path):
 root=make_source(tmp_path)
 (root/'content/linked').symlink_to(tmp_path/'outside')
 with pytest.raises(ValueError,match='symlink'):export_bundle(root,tmp_path/'bad.zip',credential_reader=fake_credentials)
 assert not (tmp_path/'bad.zip').exists()
