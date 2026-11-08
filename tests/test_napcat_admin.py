"""NapCat login UI proxy tests; every request is intercepted in memory."""

import base64
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient
import httpx
import pytest

from support_content import seed_content

from azusa_bot.app import create_app
from azusa_bot.napcat_admin import NapCatAdmin, NapCatError
from azusa_bot.settings import Owner, Settings, read_settings, write_settings


TOKEN = "test-only-private-napcat-token-00000000"
CREDENTIAL = "dGVzdC1vbmx5+bmFwY2F0L3Nlc3Npb24/00000000=="  # Standard base64 from real NapCat, including + / =.
QR_URL = "https://ssl.ptlogin2.qq.com/ptqrlogin?test=one-time-login"


@pytest.fixture
def root(tmp_path):
    (tmp_path / "data").mkdir()
    path = tmp_path / "data/napcat-webui.token"
    path.write_text(TOKEN)
    path.chmod(0o600)
    write_settings(tmp_path, Settings(app_id="official-app", onebot_self_id="10001",
                                     onebot_allowed_groups=["30001"], onebot_allowed_dm_users=["20001"],
                                     onebot_owners=[Owner(kind="dm", scope_id="20001", user_id="20001")]))
    return tmp_path


@pytest.fixture
def transport(monkeypatch):
    original = httpx.AsyncClient
    requests = []
    client_options = []

    def install(handler):
        def record(request):
            requests.append(request)
            assert request.url.host == "127.0.0.1" and request.url.port == 6200
            assert request.method == "POST"
            return handler(request)

        def factory(**options):
            client_options.append(options)
            return original(transport=httpx.MockTransport(record), **options)

        monkeypatch.setattr("azusa_bot.napcat_admin.httpx.AsyncClient", factory)
        return requests, client_options

    return install


def responder(*, logged=True, offline=False, number="10001", qr=QR_URL, credential=CREDENTIAL):
    def handle(request):
        if request.url.path == "/api/auth/login":
            assert json.loads(request.content) == {"hash": hashlib.sha256((TOKEN + ".napcat").encode()).hexdigest()}
            assert "authorization" not in request.headers
            return httpx.Response(200, json={"code": 0, "data": {"Credential": credential}})
        assert request.headers["Authorization"] == "Bearer " + credential
        assert json.loads(request.content) == {}
        data = {
            "/api/QQLogin/CheckLoginStatus": {"isLogin": logged, "isOffline": offline, "coreReady": True},
            "/api/QQLogin/GetQQLoginInfo": {"uin": number, "nick": "本机测试账号", "online": True},
            "/api/QQLogin/GetQQLoginQrcode": {"qrcode": qr},
        }[request.url.path]
        return httpx.Response(200, json={"code": 0, "data": data})
    return handle


@pytest.mark.asyncio
async def test_status_is_authenticated_loopback_read_only_and_secret_safe(root, transport, caplog):
    requests, options = transport(responder())
    before = (root / "config/local.json").read_bytes()
    status = await NapCatAdmin(root).status()
    assert status.pop('checked_at')>0
    assert status == {"available": True, "logged_in": True, "offline": False, "core_ready": True,
                      "qq_number": "10001", "nickname": "本机测试账号",'state':'online','reason_code':'online'}
    assert (root / "config/local.json").read_bytes() == before
    assert (root / "data/napcat-webui.token").read_text() == TOKEN
    assert [r.url.path for r in requests] == ["/api/auth/login", "/api/QQLogin/CheckLoginStatus",
                                            "/api/QQLogin/GetQQLoginInfo"]
    assert all(o["follow_redirects"] is False and o["trust_env"] is False for o in options)
    assert TOKEN not in repr(status) + caplog.text and CREDENTIAL not in repr(status) + caplog.text


@pytest.mark.asyncio
async def test_qr_is_locally_rendered_svg_without_fetching_external_url(root, transport):
    requests, _ = transport(responder(logged=False))
    before = (root / "config/local.json").read_bytes()
    svg = await NapCatAdmin(root).qr_svg()
    assert ET.fromstring(svg).tag.endswith("svg")
    assert b"<script" not in svg and QR_URL.encode() not in svg
    assert [r.url.path for r in requests] == ["/api/auth/login", "/api/QQLogin/GetQQLoginQrcode"]
    assert (root / "config/local.json").read_bytes() == before


@pytest.mark.asyncio
async def test_redirects_and_remote_errors_do_not_expose_tokens(root, transport, caplog):
    requests, options = transport(lambda request: httpx.Response(302, headers={"Location": "https://untrusted.example/"}, text=TOKEN))
    with pytest.raises(NapCatError, match="^napcat_invalid_response$"):
        await NapCatAdmin(root).status()
    assert len(requests) == 1 and options[0]["follow_redirects"] is False

    def failure(request):
        raise httpx.ReadTimeout("private debug data " + TOKEN + CREDENTIAL, request=request)

    transport(failure)
    with pytest.raises(NapCatError) as error:
        await NapCatAdmin(root).status()
    assert str(error.value) == "napcat_unavailable"
    assert TOKEN not in str(error.value) + caplog.text
    assert CREDENTIAL not in str(error.value) + caplog.text


@pytest.mark.asyncio
async def test_malformed_response_shapes_return_fixed_errors(root, transport):
    for payload in (None, [], {"code": "0", "data": {}}, {"code": 0, "data": []}, {"code": 1, "data": {}, "message": TOKEN}):
        transport(lambda request, data=payload: httpx.Response(200, json=data))
        with pytest.raises(NapCatError, match="^napcat_invalid_response$"):
            await NapCatAdmin(root).status()
    for credential in (None, "short", [CREDENTIAL], "x" * 16 + "\n", "x" * 16 + "你好"):
        transport(responder(credential=credential))
        with pytest.raises(NapCatError, match="^napcat_auth_failed$"):
            await NapCatAdmin(root).status()
    transport(lambda request: httpx.Response(200, content=b"not-json"))
    with pytest.raises(NapCatError, match="^napcat_invalid_response$"):
        await NapCatAdmin(root).status()


@pytest.mark.asyncio
async def test_qr_rejects_untrusted_and_malformed_urls_with_fixed_codes(root, transport):
    for url in ("http://ssl.ptlogin2.qq.com/login", "https://qq.com.untrusted.example/", "https://qq.com@untrusted.example/",
                "https://user:pass@qq.com/", "https://qq.com:443/", "javascript:alert(1)",
                "https://qq.com:notaport/", "https://[invalid/"):
        transport(responder(qr=url))
        with pytest.raises(NapCatError) as error:
            await NapCatAdmin(root).qr_svg()
        assert str(error.value) == "napcat_qr_untrusted"
        assert url not in str(error.value)
    for unavailable in (None, "", "https://qq.com/" + "x" * 4080):
        transport(responder(qr=unavailable))
        with pytest.raises(NapCatError) as error:
            await NapCatAdmin(root).qr_svg()
        assert str(error.value) in {"napcat_qr_unavailable", "napcat_qr_untrusted"}


@pytest.mark.asyncio
async def test_unlogged_offline_or_invalid_identity_cannot_bind(root, transport):
    before = (root / "config/local.json").read_bytes()
    for response in (responder(logged=False), responder(logged="true"), responder(offline=True),
                     responder(number="not-a-qq-id"), responder(number="00123"), responder(number=True)):
        transport(response)
        with pytest.raises(NapCatError, match="^napcat_login_required$"):
            await NapCatAdmin(root).bind_logged_account()
        assert (root / "config/local.json").read_bytes() == before


@pytest.mark.asyncio
async def test_binding_uses_logged_identity_and_disables_replies(root, transport):
    transport(responder(number="10002"))
    result = await NapCatAdmin(root).bind_logged_account()
    assert result == {"bound": True, "qq_number": "10002", "auto_reply": False}
    saved = read_settings(root)
    assert saved.transport == "onebot" and saved.onebot_self_id == "10002"
    assert saved.onebot_enabled is False and saved.qq_enabled is False
    assert saved.onebot_allowed_groups == saved.onebot_allowed_dm_users == saved.onebot_owners == []
    assert saved.app_id == "official-app"
    assert TOKEN not in (root / "config/local.json").read_text()
    # Rebinding the same account retains intentionally configured targets.
    saved.onebot_allowed_dm_users = ["20002"]
    write_settings(root, saved)
    await NapCatAdmin(root).bind_logged_account()
    assert read_settings(root).onebot_allowed_dm_users == ["20002"]


@pytest.mark.asyncio
async def test_token_file_must_be_private_and_endpoint_allowlist_is_read_only(root, transport):
    requests, _ = transport(responder())
    admin = NapCatAdmin(root)
    with pytest.raises(NapCatError, match="^napcat_endpoint_denied$"):
        await admin._request("Logout")
    path = root / "data/napcat-webui.token"
    path.chmod(0o644)
    with pytest.raises(NapCatError, match="^napcat_not_prepared$"):
        await admin.status()
    path.unlink()
    with pytest.raises(NapCatError, match="^napcat_not_prepared$"):
        await admin.status()
    other = root / "other-token"
    other.write_text(TOKEN)
    other.chmod(0o600)
    path.symlink_to(other)
    with pytest.raises(NapCatError, match="^napcat_not_prepared$"):
        await admin.status()
    assert requests == []


def test_http_routes_require_admin_and_do_not_accept_caller_supplied_identity(root, transport):
    seed_content(root)
    requests, _ = transport(responder(number="10002"))
    app = create_app(root)
    admin = {"Authorization": "Bearer " + (root / "data/admin.token").read_text()}
    bridge = {"Authorization": "Bearer " + (root / "data/bridge.token").read_text()}
    try:
        with TestClient(app) as client:
            for headers in ({}, bridge):
                assert client.get("/admin/napcat/status", headers=headers).status_code == 401
                assert client.get("/admin/napcat/qr", headers=headers).status_code == 401
                assert client.post("/admin/napcat/bind", headers=headers).status_code == 401
            assert requests == []
            before = (root / "config/local.json").read_bytes()
            status = client.get("/admin/napcat/status", headers=admin)
            assert status.status_code == 200 and status.json()["qq_number"] == "10002"
            assert (root / "config/local.json").read_bytes() == before
            qr = client.get("/admin/napcat/qr", headers=admin)
            assert qr.status_code == 200 and qr.headers["content-type"].startswith("image/svg+xml")
            assert qr.headers["cache-control"] == "no-store"
            bound = client.post("/admin/napcat/bind", headers=admin, json={"qq_number": "99999", "auto_reply": True})
            assert bound.status_code == 200 and bound.json()["qq_number"] == "10002"
            assert not bound.json()["auto_reply"]
            assert TOKEN not in status.text + bound.text
    finally:
        app.state.engine.memory.close()


@pytest.mark.asyncio
async def test_standard_base64_credential_is_accepted_without_logging(root, transport, caplog):
    credential = base64.b64encode(b"\xfb\xff" * 95).decode("ascii")
    assert len(credential) == 256 and all(character in credential for character in "+/=")
    requests, _ = transport(responder(logged=False, credential=credential))
    status = await NapCatAdmin(root).status()
    assert status["logged_in"] is False
    assert requests[-1].headers["Authorization"] == "Bearer " + credential
    assert credential not in repr(status) + caplog.text


def refresh_responder(*, status=None, refreshed=None):
    base = responder(logged=False)

    def handler(request):
        if request.url.path == "/api/QQLogin/CheckLoginStatus" and status is not None:
            assert request.headers["Authorization"] == "Bearer " + CREDENTIAL
            return httpx.Response(200, json={"code": 0, "data": status})
        if request.url.path == "/api/QQLogin/RefreshQRcode":
            assert request.headers["Authorization"] == "Bearer " + CREDENTIAL
            assert json.loads(request.content) == {}
            return httpx.Response(200, json={"code": 0, "data": refreshed if refreshed is not None else {"qrcodeurl": QR_URL + "-refreshed"}})
        return base(request)

    return handler


@pytest.mark.asyncio
async def test_explicit_refresh_uses_upstream_refresh_endpoint_and_local_svg(root, transport):
    requests, _ = transport(refresh_responder())
    before = (root / "config/local.json").read_bytes()
    svg = await NapCatAdmin(root).refresh_qr_svg()
    assert ET.fromstring(svg).tag.endswith("svg")
    assert QR_URL.encode() not in svg
    assert [request.url.path for request in requests] == [
        "/api/auth/login", "/api/QQLogin/CheckLoginStatus", "/api/QQLogin/RefreshQRcode"]
    assert (root / "config/local.json").read_bytes() == before


@pytest.mark.asyncio
async def test_refresh_refuses_logged_in_restarting_and_in_progress_without_mutation(root, transport):
    requests, _ = transport(refresh_responder())
    states = [
        ({"isLogin": True}, "napcat_already_logged_in"),
        ({"isLogin": "false"}, "napcat_login_status_unknown"),
        ({}, "napcat_login_status_unknown"),
        ({"isLogin": False, "loginPhase": "reconnecting"}, "napcat_restarting"),
        ({"isLogin": False, "restarting": True}, "napcat_restarting"),
        ({"isLogin": False, "loginPhase": "qrcode_scanned"}, "napcat_login_in_progress"),
        ({"isLogin": False, "qrLoginAccepted": True}, "napcat_login_in_progress"),
        ({"isLogin": False, "loginPhase": "initializing"}, "napcat_login_in_progress"),
        ({"isLogin": False, "loginPhase": []}, "napcat_invalid_response"),
        ({"isLogin": False, "qrLoginAccepted": "true"}, "napcat_invalid_response"),
        ({"isLogin": False, "restarting": "true"}, "napcat_invalid_response"),
    ]
    before = (root / "config/local.json").read_bytes()
    for state, code in states:
        transport(refresh_responder(status=state))
        with pytest.raises(NapCatError) as error:
            await NapCatAdmin(root).refresh_qr_svg()
        assert str(error.value) == code
    assert all(request.url.path != "/api/QQLogin/RefreshQRcode" for request in requests)
    assert (root / "config/local.json").read_bytes() == before


@pytest.mark.asyncio
async def test_refresh_response_restart_and_invalid_qr_are_fixed_safe_errors(root, transport):
    for result, expected in (
        ({"restarting": True, "qrcodeurl": ""}, "napcat_restarting"),
        ({"restarting": "true", "qrcodeurl": QR_URL}, "napcat_invalid_response"),
        ({"qrcode": QR_URL}, "napcat_qr_unavailable"),
        ({"qrcodeurl": "https://untrusted.example/" + TOKEN}, "napcat_qr_untrusted"),
    ):
        transport(refresh_responder(refreshed=result))
        with pytest.raises(NapCatError) as error:
            await NapCatAdmin(root).refresh_qr_svg()
        assert str(error.value) == expected
        assert TOKEN not in str(error.value)


def test_refresh_route_is_admin_post_only_with_conflict_responses(root, transport):
    seed_content(root)
    requests, _ = transport(refresh_responder())
    app = create_app(root)
    admin = {"Authorization": "Bearer " + (root / "data/admin.token").read_text()}
    bridge = {"Authorization": "Bearer " + (root / "data/bridge.token").read_text()}
    try:
        with TestClient(app) as client:
            path = "/admin/napcat/qr/refresh"
            assert client.post(path).status_code == 401
            assert client.post(path, headers=bridge).status_code == 401
            assert client.get(path, headers=admin).status_code == 405
            assert requests == []
            response = client.post(path, headers=admin)
            assert response.status_code == 200 and response.headers["content-type"].startswith("image/svg+xml")
            assert response.headers["cache-control"] == "no-store"
            transport(refresh_responder(status={"isLogin": True}))
            conflict = client.post(path, headers=admin)
            assert conflict.status_code == 409 and conflict.json()["detail"] == "napcat_already_logged_in"
            transport(refresh_responder(refreshed={"restarting": True, "qrcodeurl": ""}))
            conflict = client.post(path, headers=admin)
            assert conflict.status_code == 409 and conflict.json()["detail"] == "napcat_restarting"
    finally:
        app.state.engine.memory.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('status_fields,info_fields,expected',[
    ({'isOffline':True},{},'offline'),
    ({},{'online':False},'offline'),
    ({},{'online':None},'unknown'),
    ({'coreReady':False},{},'unknown'),
    ({'loginPhase':'reconnecting'},{},'reconnecting'),
    ({'isLogin':False,'loginPhase':'initializing','qrLoginAccepted':True},{},'initializing'),
    ({'isLogin':False},{},'login_required'),
])
async def test_contradictory_or_incomplete_login_flags_are_not_online(root,transport,status_fields,info_fields,expected):
    original=responder()
    def reply(request):
        response=original(request);data=response.json()
        if request.url.path.endswith('CheckLoginStatus'):data['data'].update(status_fields)
        if request.url.path.endswith('GetQQLoginInfo'):data['data'].update(info_fields)
        return httpx.Response(200,json=data)
    transport(reply)
    result=await NapCatAdmin(root).status()
    assert result['state']==expected and result['logged_in'] is False


@pytest.mark.asyncio
async def test_login_errors_are_categories_without_secret_payload(root,transport):
    original=responder(logged=False)
    def reply(request):
        response=original(request);data=response.json()
        if request.url.path.endswith('CheckLoginStatus'):
            data['data']['loginError']='登录已失效 '+TOKEN+' https://qq.com/private'
        return httpx.Response(200,json=data)
    transport(reply);result=await NapCatAdmin(root).status()
    assert result['login_error_code']=='session_expired'
    assert TOKEN not in repr(result) and 'https://' not in repr(result)


@pytest.mark.asyncio
async def test_status_reuses_one_memory_session_across_repeated_and_concurrent_checks(root,transport):
    import asyncio
    requests,_=transport(responder());admin=NapCatAdmin(root)
    before=set(p.name for p in (root/'data').iterdir())
    await asyncio.gather(*(admin.status() for _ in range(12)))
    assert sum(r.url.path=='/api/auth/login' for r in requests)==1
    assert sum(r.url.path=='/api/QQLogin/CheckLoginStatus' for r in requests)==12
    assert sum(r.url.path=='/api/QQLogin/GetQQLoginInfo' for r in requests)==12
    assert set(p.name for p in (root/'data').iterdir())==before
    admin._credential_at-=3001;admin._credential_wall-=3001
    await admin.status()
    assert sum(r.url.path=='/api/auth/login' for r in requests)==2


@pytest.mark.asyncio
async def test_upstream_restart_reauthenticates_once_without_qq_login(root,transport):
    base=responder();reject=[False]
    def handler(request):
        if reject[0] and request.url.path.endswith('/CheckLoginStatus'):
            reject[0]=False
            return httpx.Response(200,json={'code':-1,'message':'Unauthorized'})
        return base(request)
    requests,_=transport(handler);admin=NapCatAdmin(root)
    await admin.status();reject[0]=True
    assert (await admin.status())['logged_in']
    assert sum(r.url.path=='/api/auth/login' for r in requests)==2
    assert all(r.url.path in {'/api/auth/login','/api/QQLogin/CheckLoginStatus','/api/QQLogin/GetQQLoginInfo'} for r in requests)


@pytest.mark.asyncio
async def test_webui_rate_limit_is_distinct_and_backs_off_without_more_auth_calls(root,transport,caplog):
    requests,_=transport(lambda request:httpx.Response(200,json={'code':-1,'message':'login rate limit'}))
    admin=NapCatAdmin(root)
    deadline=None
    for _ in range(4):
        with pytest.raises(NapCatError,match='^napcat_webui_rate_limited$'):await admin.status()
        if deadline is None:deadline=admin._auth_retry_after
        assert admin._auth_retry_after==deadline  # polling must not prolong backoff forever
    assert len(requests)==1 and admin._credential is None
    assert TOKEN not in caplog.text and CREDENTIAL not in caplog.text


@pytest.mark.asyncio
async def test_invalidated_credentials_cannot_trigger_unbounded_reauth(root,transport):
    base=responder()
    def handler(request):
        if request.url.path=='/api/auth/login':return base(request)
        return httpx.Response(401,json={'private':TOKEN})
    requests,_=transport(handler);admin=NapCatAdmin(root)
    with pytest.raises(NapCatError,match='^napcat_auth_failed$'):await admin.status()
    assert sum(r.url.path=='/api/auth/login' for r in requests)==2
    count=len(requests)
    with pytest.raises(NapCatError):await admin.status()
    assert len(requests)==count


@pytest.mark.asyncio
async def test_private_token_permission_is_checked_even_with_a_cached_session(root,transport):
    requests,_=transport(responder());admin=NapCatAdmin(root);await admin.status();count=len(requests)
    (root/'data/napcat-webui.token').chmod(0o644)
    with pytest.raises(NapCatError,match='napcat_not_prepared'):await admin.status()
    assert len(requests)==count
