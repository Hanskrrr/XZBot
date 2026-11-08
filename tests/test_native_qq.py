import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from azusa_bot import native_qq


def prepare(root, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: root.parent / 'home')
    base = native_qq.directory(root)
    data = native_qq.data_directory(root)
    marker = json.dumps({'schema': 2, 'root': str(root.resolve())})
    for path, value in [(base/'prepared.json', marker), (data/'owner.json', marker),
                        (native_qq.executable(root), 'fixture'),
                        (data/'napcat/napcat.mjs', ''),
                        (data/'napcat-work/config/webui.json', '{}')]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    return base, data


def test_native_uses_separate_sandbox_paths_per_account(tmp_path, monkeypatch):
    root = tmp_path/'account'
    base, data = prepare(root, monkeypatch)
    assert native_qq.prepared(root)
    assert data != native_qq.data_directory(tmp_path/'other-account')
    assert data.is_relative_to(Path.home()/'Library/Containers/com.tencent.qq/Data/Documents/XZBot')
    launch = Mock(return_value=SimpleNamespace(pid=12345))
    monkeypatch.setattr(native_qq.subprocess, 'Popen', launch)
    monkeypatch.setattr(native_qq, '_record', lambda _: None)
    monkeypatch.setattr(native_qq, '_identity', lambda _: str(native_qq.executable(root)))
    monkeypatch.setattr('azusa_bot.host.require_free_loopback_port', lambda _: None)
    native_qq.start(root)
    args, kwargs = launch.call_args
    assert '--user-data-dir='+str(data/'electron-data') in args[0]
    assert kwargs['env']['NAPCAT_QQ_DATA_DIR'] == str(data/'qq-data')
    assert kwargs['env']['NAPCAT_WORKDIR'] == str(data/'napcat-work')
    assert kwargs['env']['NAPCAT_DISABLE_BYPASS'] == '1'
    assert kwargs['cwd'] == data


@pytest.mark.parametrize('invalid', ['legacy', 'wrong_owner', 'missing_script', 'symlink'])
def test_unverified_or_mismatched_native_runtime_never_launches(tmp_path, monkeypatch, invalid):
    root = tmp_path/'account'
    base, data = prepare(root, monkeypatch)
    if invalid == 'legacy':
        (base/'prepared.json').write_text(json.dumps({'schema': 1, 'root': str(root.resolve())}))
    elif invalid == 'wrong_owner':
        (data/'owner.json').write_text(json.dumps({'schema': 2, 'root': str(tmp_path/'other')}))
    elif invalid == 'missing_script':
        (data/'napcat/napcat.mjs').unlink()
    else:
        moved = data.with_name(data.name+'-moved')
        data.rename(moved)
        data.symlink_to(moved, target_is_directory=True)
    launch = Mock()
    monkeypatch.setattr(native_qq.subprocess, 'Popen', launch)
    assert not native_qq.prepared(root)
    with pytest.raises(RuntimeError, match='runtime_not_prepared'):
        native_qq.start(root)
    launch.assert_not_called()
