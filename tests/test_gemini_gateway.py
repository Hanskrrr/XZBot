import asyncio
import json
from pathlib import Path
import subprocess

import httpx
import pytest
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from azusa_bot.credential_store import CredentialStore, CredentialStoreError
from azusa_bot.gemini_gateway import COMMIT, VERSION, GeminiGateway, GatewayError, create_router
from azusa_bot.providers import CloudProviders
from azusa_bot.secrets import Credentials
from azusa_bot.settings import private_write, read_settings, Settings, write_settings

PROJECT = Path(__file__).parents[1]
KEY = "synthetic-gemini-key-never-log"


def test_migrated_service_preserves_all_endpoint_keys(tmp_path):
    original, moved = tmp_path / "old", tmp_path / "new"
    old = CredentialStore(original)
    old.set("compatible_key", KEY, "https://models.example/v1")
    old.set("fish_key", KEY)
    private_write(moved / "data/keychain-service", old.service + "\n")
    assert CredentialStore(moved).get("compatible_key", "https://models.example/v1") == KEY
    assert CredentialStore(moved).get("fish_key") == KEY
    assert not CredentialStore(moved).get("compatible_key", "https://other.example/v1")
    private_write(moved / "data/keychain-service", "arbitrary-other-service")
    with pytest.raises(CredentialStoreError, match="invalid_keychain_service"):
        CredentialStore(moved)


@pytest.mark.asyncio
async def test_write_only_keys_model_and_explicit_clear(tmp_path, monkeypatch):
    gateway = GeminiGateway(tmp_path)
    result = await gateway.save({"keys": [KEY, KEY], "settings": {"model": "chosen-model"}})
    assert KEY not in json.dumps(result)
    assert result["credentials"]["key_count"] == 1
    assert read_settings(tmp_path).gemini_model == "chosen-model"
    stopped = []
    async def stop():
        stopped.append(True)
    monkeypatch.setattr(gateway, "_stop", stop)
    await gateway.save({"settings": {"model": "changed-model"}})
    assert not stopped and gateway.describe()["credentials"]["configured"]
    await gateway.save({"clear_keys": True})
    assert stopped and not gateway.describe()["credentials"]["configured"]
    assert KEY not in (tmp_path / "config/local.json").read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"keys": []}, {"keys": ["bad\nkey"]}, {"keys": [KEY], "clear_keys": True},
                                  {"unknown": KEY}, {"settings": {"model": "bad model"}}])
async def test_invalid_keys_and_fields_are_safe(tmp_path, body):
    with pytest.raises(GatewayError) as error:
        await GeminiGateway(tmp_path).save(body)
    assert KEY not in str(error.value)


@pytest.mark.asyncio
async def test_provider_gemini_uses_only_fixed_loopback_token(tmp_path, monkeypatch):
    requests = []
    original = httpx.AsyncClient
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": '{"text":"ok","mode":"text"}'}}]})
    def client(**kwargs):
        assert kwargs["trust_env"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr("azusa_bot.providers.httpx.AsyncClient", client)
    provider = CloudProviders(Credentials(deepseek_key="never-deepseek", compatible_key="never-compatible",
        gemini_balance_token="synthetic-local-token"), "chosen-model", "fish", tmp_path,
        protocol="gemini_balance", base_url="https://untrusted.example")
    assert await provider.complete([{"role": "user", "content": "json please"}]) == {"text": "ok", "mode": "text"}
    assert str(requests[0].url) == "http://127.0.0.1:6201/openai/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer synthetic-local-token"
    body = json.loads(requests[0].content)
    assert body["response_format"] == {"type": "json_object"} and "thinking" not in body


def test_router_uses_caller_admin_auth_and_never_echoes(tmp_path):
    app = FastAPI()
    def authorize(request: Request):
        if request.headers.get("authorization") != "Bearer synthetic-admin":
            raise HTTPException(401)
    app.include_router(create_router(GeminiGateway(tmp_path)), dependencies=[Depends(authorize)])
    with TestClient(app) as client:
        assert client.get("/admin/gemini").status_code == 401
        headers = {"authorization": "Bearer synthetic-admin"}
        result = client.put("/admin/gemini", headers=headers, json={"keys": [KEY]})
        assert result.status_code == 200 and KEY not in result.text
        result = client.put("/admin/gemini", headers=headers, json={"keys": [KEY + "\n"]})
        assert result.status_code == 400 and KEY not in result.text
        result = client.put("/admin/gemini", headers=headers, json=[KEY])
        assert result.status_code == 400 and KEY not in result.text
        assert client.post("/admin/gemini/start", headers=headers).json() == {"detail": "gemini_not_installed"}


@pytest.mark.asyncio
async def test_real_child_starts_and_stops_without_cloud(tmp_path):
    if not (PROJECT / "runtime/gemini-balance-venv/bin/python").exists():
        pytest.skip("Pinned optional gateway runtime not installed")
    gateway = GeminiGateway(tmp_path)
    gateway.source = PROJECT / "runtime/gemini-balance"
    gateway.venv = PROJECT / "runtime/gemini-balance-venv"
    private_write(gateway.runtime / "installation.json", json.dumps({"commit": COMMIT, "version": VERSION}))
    await gateway.save({"keys": [KEY]})
    try:
        result = await gateway.start()
        assert result["runtime"]["running"]
        assert KEY not in json.dumps(result)
        assert (gateway.runtime / "stats.sqlite3").exists()
        for path in gateway.runtime.rglob("*"):
            if path.is_file():
                assert KEY.encode() not in path.read_bytes()
    finally:
        await gateway.close()
    assert not gateway.describe()["runtime"]["running"]
    assert gateway.enabled()
    await gateway.restore()
    assert gateway.describe()["runtime"]["running"]
    await gateway.stop()
    assert not gateway.enabled()
    await gateway.restore()
    assert not gateway.describe()["runtime"]["running"]


def test_diagnostics_keep_safe_gemini_codes():
    from azusa_bot.diagnostics import safe_code, explanation
    for code in ("gemini_balance_http_503", "gemini_balance_network_or_format", "gemini_balance_incomplete"):
        assert safe_code(code) == code
        assert explanation(code)
    assert safe_code("gemini_balance_" + KEY) == "provider_unknown_error"


def test_pinned_upstream_rotation_json_and_safe_logs(tmp_path):
    python = PROJECT / "runtime/gemini-balance-venv/bin/python"
    if not python.exists():
        pytest.skip("Pinned optional gateway runtime not installed")
    # Separate interpreter keeps upstream modules/logging/global configuration
    # isolated from the main bot. Every outbound request uses MockTransport.
    script = r'''
import importlib.util,json,pathlib,sqlite3,sys,httpx
from fastapi.testclient import TestClient
source,runtime,bootstrap=map(pathlib.Path,sys.argv[1:])
spec=importlib.util.spec_from_file_location('gateway_bootstrap',bootstrap)
mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
keys=['synthetic-key-one','synthetic-key-two'];local='synthetic-local-token'
app=mod.build_app(source,runtime,keys,local)
calls=[]
def handle(request):
 calls.append(request)
 body=json.loads(request.content)
 assert body['response_format']=={'type':'json_object'}
 assert 'thinking' not in body
 assert str(request.url)=='https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
 if body['model']=='fail-model':return httpx.Response(503,text='sensitive upstream response '+keys[0])
 return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"text":"ok"}'}}]})
original=httpx.AsyncClient
httpx.AsyncClient=lambda **kw:original(transport=httpx.MockTransport(handle),**kw)
with TestClient(app) as client:
 assert not calls
 assert client.get('/health').status_code==401
 headers={'authorization':'Bearer '+local}
 assert client.get('/health',headers=headers).status_code==200
 assert not calls
 body={'model':'chosen-model','messages':[{'role':'user','content':'test json'}],'stream':False,'response_format':{'type':'json_object'}}
 for _ in range(2):assert client.post('/openai/v1/chat/completions',headers=headers,json=body).status_code==200
 assert [r.headers['authorization'] for r in calls]==['Bearer '+k for k in keys]
 body['model']='fail-model'
 response=client.post('/openai/v1/chat/completions',headers=headers,json=body)
 assert response.status_code==503 and 'sensitive' not in response.text and keys[0] not in response.text
 assert len(calls)==4
 assert client.get('/config',headers=headers).status_code==404
for path in runtime.rglob('*'):
 if path.is_file():
  data=path.read_bytes()
  assert all(k.encode() not in data for k in keys)
  assert b'test json' not in data and b'sensitive upstream' not in data
with sqlite3.connect(runtime/'stats.sqlite3') as db:
 assert db.execute('SELECT sum(count) FROM counters').fetchone()[0]==4
print('upstream mocked rotation and JSON verified')
'''
    result = subprocess.run([str(python), "-c", script, str(PROJECT / "runtime/gemini-balance"), str(tmp_path),
                             str(PROJECT / "src/azusa_bot/gemini_bootstrap.py")],
                            cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert "synthetic-key" not in result.stdout + result.stderr
