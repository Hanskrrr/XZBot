"""Account isolation checks: temporary files/fake processes; no QQ or cloud calls."""
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from azusa_bot.runtime_layout import runtime_layout
from azusa_bot.settings import private_write

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
import start_onebot
import prepare_onebot


def account(tmp_path, slot=1):
    manager = (tmp_path / "QQBot").resolve()
    root = manager / "accounts" / (str(slot) * 32)
    private_write(root / "config/instance.json", json.dumps({
        "schema": 1, "slot": slot, "manager_root": str(manager)}))
    return root


def test_default_and_three_accounts_have_disjoint_fixed_ports(tmp_path):
    default = runtime_layout(tmp_path)
    assert (default.core_port, default.dashboard_port, default.onebot_port,
            default.napcat_port, default.gemini_port) == (6196, 6197, 6199, 6200, 6201)
    assert default.lima_home == tmp_path.resolve() / "runtime/lima"
    used = {6196, 6197, 6199, 6200, 6201}
    for slot in (1, 2, 3):
        layout = runtime_layout(account(tmp_path, slot))
        ports = {layout.core_port, layout.dashboard_port, layout.onebot_port,
                 layout.napcat_port, layout.gemini_port}
        assert len(ports) == 5 and not used & ports
        assert layout.core_port == 6196 + 20 * slot
        assert layout.core_url == f"http://127.0.0.1:{layout.core_port}"
        assert layout.lima_home == layout.manager_root / "runtime/account-vms" / layout.root.name[:12]
        used |= ports


@pytest.mark.parametrize("change", [{"slot": True}, {"slot": 0}, {"slot": 4},
    {"slot": "1"}, {"schema": True}, {"core_port": 8000}, {"manager_root": "/tmp"}])
def test_manifest_cannot_override_ports_or_escape_manager(tmp_path, change):
    root = account(tmp_path)
    path = root / "config/instance.json"
    value = json.loads(path.read_text()); value.update(change)
    private_write(path, json.dumps(value))
    with pytest.raises(ValueError, match="runtime_instance_manifest_invalid"):
        runtime_layout(root)


def test_missing_or_public_or_symlink_manifest_never_falls_back(tmp_path):
    root = account(tmp_path)
    path = root / "config/instance.json"
    path.chmod(0o644)
    with pytest.raises(ValueError): runtime_layout(root)
    path.unlink()
    with pytest.raises(ValueError, match="missing"): runtime_layout(root)
    target = tmp_path / "foreign.json"
    private_write(target, json.dumps({"schema": 1, "slot": 1, "manager_root": str(root.parent.parent)}))
    path.symlink_to(target)
    with pytest.raises(ValueError, match="invalid"): runtime_layout(root)


def test_core_readiness_authenticates_only_its_own_port(tmp_path, monkeypatch):
    root = account(tmp_path, 2)
    private_write(root / "data/admin.token", "test-only-token")
    requests = []
    class Opener:
        def open(self, request, **_):
            requests.append(request)
            return io.BytesIO(json.dumps({"version": "test", "settings": {}, "project_path": str(root)}).encode())
    monkeypatch.setattr(start_onebot.urllib.request, "build_opener", lambda *_: Opener())
    assert start_onebot.core_ready(root)
    assert requests[0].full_url == "http://127.0.0.1:6236/admin/status"
    assert requests[0].headers["Authorization"] == "Bearer test-only-token"


def test_bridge_uses_account_ports_and_private_config(tmp_path, monkeypatch):
    root = account(tmp_path)
    monkeypatch.setattr(prepare_onebot, "ROOT", root)
    runtime = prepare_onebot.prepare()
    config = json.loads((runtime / "data/cmd_config.json").read_text())
    assert config["dashboard"]["port"] == 6217
    assert config["platform"][0]["ws_reverse_port"] == 6219
    bridge = json.loads((runtime / "data/config/astrbot_plugin_azusa_onebot_config.json").read_text())
    assert bridge["core_url"] == "http://127.0.0.1:6216"
    assert not bridge["enabled"]
    assert (root / "data/onebot.token").stat().st_mode & 0o077 == 0
    seen = []
    monkeypatch.setattr(start_onebot, "lock_held", lambda _: True)
    monkeypatch.setattr(start_onebot, "port_open", lambda port: seen.append(port) or True)
    monkeypatch.setattr(start_onebot, "spawn", lambda *a: pytest.fail("must reuse owned bridge"))
    start_onebot.ensure_bridge(root)
    assert seen == [6217, 6219]


def test_lima_paths_template_and_ws_target_are_isolated(tmp_path, monkeypatch):
    root = account(tmp_path, 3)
    (root / "scripts").symlink_to(PROJECT / "scripts", target_is_directory=True)
    monkeypatch.setenv("AZUSA_ROOT", str(root))
    monkeypatch.setenv("AZUSA_LIMA_HOME", str(tmp_path / "unrelated-primary"))
    spec = importlib.util.spec_from_file_location("account_lima_test", PROJECT / "scripts/napcat_runtime.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert module.ROOT == root
    assert module.LIMA_DIR == runtime_layout(root).lima_home
    module.prepare_paths()
    assert (root / "runtime/lima").resolve() == module.LIMA_DIR
    template = module.vm_template().read_text()
    assert "hostPort: 6260" in template and "hostPort: 6200" not in template
    calls = []
    monkeypatch.setattr(module, "lima", lambda *a, **k: calls.append((a, k)))
    module.configure()
    config = json.loads(calls[0][1]["input"])
    assert config["onebot11.json"]["network"]["websocketClients"][0]["url"] == "ws://host.lima.internal:6259/ws"


@pytest.mark.asyncio
async def test_native_runtime_overrides_inherited_primary_environment(tmp_path, monkeypatch):
    from azusa_bot.bot_runtime import NativeBotRuntime
    root = account(tmp_path)
    for rel in (".venv/bin/python", "scripts/bot_runtime_action.py"):
        file = root / rel; file.parent.mkdir(parents=True, exist_ok=True); file.touch()
    monkeypatch.setenv("AZUSA_ROOT", "/unrelated/primary")
    process = SimpleNamespace(wait=AsyncMock(return_value=0))
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("azusa_bot.bot_runtime.asyncio.create_subprocess_exec", spawn)
    await NativeBotRuntime(root)._run("stop")
    assert spawn.await_args.kwargs["env"]["AZUSA_ROOT"] == str(root)
    assert spawn.await_args.kwargs["env"]["PYTHONPATH"] == str(root / "src")
    assert spawn.await_args.args[-1] == "stop"


def test_napcat_and_autostart_never_target_primary(tmp_path):
    from azusa_bot.napcat_admin import NapCatAdmin
    from azusa_bot.desktop import DesktopService
    root = account(tmp_path)
    assert NapCatAdmin(root).BASE == "http://127.0.0.1:6220"
    service = DesktopService(root, tmp_path / "home")
    service.set_autostart({"enabled": True})
    data = plistlib.loads(service.agent.read_bytes())
    assert data["Label"] == "local.qqbot.service.account." + root.name[:12]
    assert data["EnvironmentVariables"]["AZUSA_ROOT"] == str(root)


def test_gemini_provider_and_gateway_share_code_but_isolate_runtime(tmp_path):
    from azusa_bot.gemini_gateway import GeminiGateway
    from azusa_bot.providers import CloudProviders
    from azusa_bot.secrets import Credentials
    root = account(tmp_path, 2)
    gateway = GeminiGateway(root)
    assert gateway.layout.gemini_port == 6241
    assert gateway.source == root.parent.parent / "runtime/gemini-balance"
    assert gateway.runtime == root / "runtime/gemini-gateway"
    provider = CloudProviders(Credentials(), "fake", "fake", root / "data/media", protocol="gemini_balance", runtime_root=root)
    assert provider.base_url == "http://127.0.0.1:6241/openai/v1"


def test_simultaneous_bind_cannot_claim_same_qq_across_accounts(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from azusa_bot.accounts import AccountRegistry
    from azusa_bot.napcat_admin import NapCatAdmin, NapCatError
    from azusa_bot.settings import read_settings
    first, second = account(tmp_path, 1), account(tmp_path, 2)
    registry = AccountRegistry(first)
    registry.write([{"id": root.name, "name": "test", "slot": slot}
                    for root, slot in ((first, 1), (second, 2))])
    def bind(root):
        try:
            NapCatAdmin(root)._bind_account("10001")
            return "bound"
        except NapCatError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(bind, (first, second)))
    assert sorted(outcomes) == ["bound", "qq_account_already_managed"]
    assert sum(read_settings(root).onebot_self_id == "10001" for root in (first, second)) == 1


def test_account_origin_accepts_own_page_and_rejects_primary(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from azusa_bot.app import create_app
    from azusa_bot.secrets import Credentials
    from azusa_bot.settings import Settings, write_settings
    from support_content import seed_content
    root = account(tmp_path)
    seed_content(root)
    write_settings(root, Settings(read_existing_dsh=False, fish_reference_id="test-reference", fish_model_override=True))
    monkeypatch.setattr("azusa_bot.app.load_credentials", lambda *_: Credentials())
    app = create_app(root)
    try:
        client = TestClient(app)
        assert client.get("/health", headers={"Origin": "http://127.0.0.1:6216"}).status_code == 200
        assert client.get("/health", headers={"Origin": "http://127.0.0.1:6196"}).status_code == 403
    finally:
        app.state.engine.close()
        app.state.subscriptions.close()


def test_owned_partial_vm_can_be_stopped_but_not_started(tmp_path, monkeypatch):
    import bot_runtime_action as actions
    root = account(tmp_path)
    directory = runtime_layout(root).lima_home
    (directory / "azusa").mkdir(parents=True)
    (root / "runtime").mkdir()
    (root / "runtime/lima").symlink_to(directory)
    (directory / ".azusa-project").write_text(str(root))
    (directory / "azusa/lima.yaml").write_text("test fixture")
    monkeypatch.setattr(actions, "LIMA_DIR", directory)
    monkeypatch.setattr(actions, "vm_prepared", lambda _: False)
    monkeypatch.setattr(actions, "lima", lambda *a, **k: SimpleNamespace(stdout=json.dumps({"name": "azusa", "status": "Running"})))
    assert actions.vm_state(root, allow_partial=True) == "Running"
    with pytest.raises(RuntimeError, match="ownership"):
        actions.vm_state(root)
    (directory / ".azusa-project").write_text("foreign")
    with pytest.raises(RuntimeError, match="ownership"):
        actions.vm_state(root, allow_partial=True)


def test_prepare_failure_keeps_stop_available(tmp_path, monkeypatch):
    from contextlib import nullcontext
    from azusa_bot.bot_control import read_control
    root = account(tmp_path)
    private_write(root / "data/bot-control.json", json.dumps({"schema": 1, "state": "stopped",
        "epoch": "a" * 32, "updated_at": 0, "error_code": ""}))
    spec = importlib.util.spec_from_file_location("account_action_test", PROJECT / "scripts/account_instance_action.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module.sys, "argv", ["helper", "prepare"])
    monkeypatch.setattr(module, "startup_guard", lambda _: nullcontext())
    monkeypatch.setattr(module, "ensure_core", lambda _: None)
    monkeypatch.setattr(module, "ensure_supervisor", lambda _: None)
    monkeypatch.setattr(module, "vm_prepared", lambda _: False)
    monkeypatch.setattr(module, "open_management", lambda _: pytest.fail("failed setup must not open UI"))
    def failed_setup(*a, **k):
        assert read_control(root)["state"] == "error"
        raise RuntimeError("setup failed")
    monkeypatch.setattr(module.subprocess, "run", failed_setup)
    with pytest.raises(RuntimeError, match="setup failed"):
        module.main()
    assert read_control(root)["state"] == "error"
    assert not read_control(root)["enabled"]
