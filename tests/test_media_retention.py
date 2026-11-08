import json
import os
import time
from pathlib import Path
import pytest
from azusa_bot.core import Engine, Incoming


def test_forget_removes_only_scoped_member_audio_and_expiry(tmp_path):
    media = tmp_path / 'data/media'
    media.mkdir(parents=True)
    engine = Engine(tmp_path)
    event = Incoming(app_id='app',kind='group',scope_id='g',user_id='alice',event_id='e',text='hello')
    reply = engine.ledger.reserve(event,'qq:app:group:g')
    own = media / 'owned.wav'; own.write_bytes(b'own')
    other = media / 'other.wav'; other.write_bytes(b'other')
    with engine.ledger.db() as db:
        db.execute('UPDATE deliveries SET media_name=? WHERE reply_id=?',(own.name,reply))
    engine.forget_person('qq:app:group:g','alice')
    assert not own.exists() and other.exists()
    os.utime(other,(time.time()-90000,time.time()-90000))
    engine.prune_media()
    assert not other.exists()
    engine.memory.close()


def test_assets_require_explicit_manifest_and_cannot_escape(tmp_path):
    assets = tmp_path / 'content/assets'; assets.mkdir(parents=True)
    engine = Engine(tmp_path)
    assert engine.asset('hello')['mode'] == 'text'
    outside = tmp_path / 'outside.png'; outside.write_bytes(b'fixture')
    (assets / 'manifest.json').write_text(json.dumps({'bad':{'file':'../../outside.png','approved':True}}))
    result = engine.asset('bad')
    assert result['mode'] == 'text' and not result.get('media_path')
    assert outside.read_bytes() == b'fixture'
    from PIL import Image
    Image.new('RGB', (8, 8), 'red').save(assets / 'happy.png')
    (assets / 'manifest.json').write_text(json.dumps({'happy':{'file':'happy.png','approved':True}}))
    result = engine.asset('happy')
    assert result['mode']=='image' and result['text']==''
    assert Path(result['media_path']).parent == (tmp_path / 'data/media')
    engine.memory.close()


def test_cancelled_reply_cleanup_cannot_delete_unowned_paths_or_follow_symlinks(tmp_path):
    engine=Engine(tmp_path)
    media=tmp_path/"data/media"; media.mkdir(exist_ok=True)
    owned=media/("a"*32+".wav");owned.write_bytes(b"generated")
    outside=tmp_path/("b"*32+".wav");outside.write_bytes(b"unowned")
    target=media/("c"*32+".wav");target.write_bytes(b"another reply")
    link=media/("d"*32+".wav");link.symlink_to(target)
    directory=media/("e"*32+".wav");directory.mkdir()
    for path in (owned,outside,link,directory):
        engine.discard_generated_media({"media_path":str(path)})
    assert not owned.exists()
    assert outside.exists() and target.exists() and link.is_symlink() and directory.is_dir()
    engine.memory.close()
