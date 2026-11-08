"""Authenticated, fixed-loopback proxy; credentials and QR URLs stay off logs."""
import asyncio
import hashlib
import io
from .host import safe_open, NOFOLLOW
from .host import private_info, private_path, protect, link_directory
import os
from pathlib import Path
import re
import stat
import time
from urllib.parse import urlsplit

import httpx
import qrcode
from qrcode.image.svg import SvgPathImage
from qrcode.exceptions import DataOverflowError

from .settings import read_settings, write_settings, settings_lock
from .runtime_layout import runtime_layout
from .accounts import AccountRegistry, AccountError, assert_account_available


class NapCatError(RuntimeError):
    pass


class NapCatAdmin:
    BASE = "http://127.0.0.1:6200"

    def __init__(self, root: Path):
        self.root = root
        self.BASE = runtime_layout(root).napcat_url
        self._request_lock = asyncio.Lock()
        self._credential = None
        self._credential_at = 0
        self._credential_wall = 0
        self._credential_digest = None
        self._auth_retry_after = 0
        self._auth_retry_code = 'napcat_webui_rate_limited'

    def _token(self):
        path = self.root / "data/napcat-webui.token"
        try:
            fd = safe_open(path, os.O_RDONLY | NOFOLLOW)
            with os.fdopen(fd) as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or not private_info(st,f.fileno()):
                    raise ValueError()
                value = f.read(1024).strip()
                if not 32 <= len(value) < 1024:
                    raise ValueError()
                return value
        except (OSError,ValueError):
            raise NapCatError("napcat_not_prepared") from None

    @staticmethod
    def _data(response):
        try:
            if response.status_code in {401,403}:
                raise NapCatError('napcat_webui_unauthorized')
            if response.status_code == 429:
                raise NapCatError('napcat_webui_rate_limited')
            if response.status_code != 200:
                raise ValueError()
            data = response.json()
            if isinstance(data,dict) and data.get('code') != 0:
                if data.get('message') == 'login rate limit':
                    raise NapCatError('napcat_webui_rate_limited')
                if data.get('message') == 'Unauthorized':
                    raise NapCatError('napcat_webui_unauthorized')
            if not isinstance(data,dict) or data.get("code") != 0 or not isinstance(data.get("data"),dict):
                raise ValueError()
            return data["data"]
        except (ValueError,TypeError):
            raise NapCatError("napcat_invalid_response") from None

    async def _request(self, endpoint: str):
        if endpoint not in {"CheckLoginStatus","GetQQLoginInfo","GetQQLoginQrcode","RefreshQRcode"}:
            raise NapCatError("napcat_endpoint_denied")
        async with self._request_lock:
            return await self._request_locked(endpoint)

    async def _request_locked(self, endpoint):
        digest = hashlib.sha256((self._token()+".napcat").encode()).hexdigest()
        if digest != self._credential_digest:
            self._credential = None
            self._credential_digest = digest
            self._auth_retry_after = 0
        if not (0 <= time.monotonic()-self._credential_at < 3000 and 0 <= time.time()-self._credential_wall < 3000):
            self._credential = None
        try:
            async with httpx.AsyncClient(timeout=8,trust_env=False,follow_redirects=False) as client:
                # NapCat WebUI credentials last one hour. Reuse them in memory,
                # rather than consuming its login-rate allowance on every poll.
                for attempt in range(2):
                    if self._credential is None:
                        if time.monotonic() < self._auth_retry_after:
                            raise NapCatError(self._auth_retry_code)
                        auth = self._data(await client.post(self.BASE+"/api/auth/login",json={"hash":digest}))
                        credential = auth.get('Credential')
                        if not isinstance(credential,str) or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,10000}",credential):
                            raise NapCatError('napcat_auth_failed')
                        self._credential = credential
                        self._credential_at,self._credential_wall=time.monotonic(),time.time()
                    try:
                        return self._data(await client.post(self.BASE+"/api/QQLogin/"+endpoint,
                            json={},headers={"Authorization":"Bearer "+self._credential}))
                    except NapCatError as exc:
                        if str(exc)!='napcat_webui_unauthorized':
                            raise
                        self._credential=None
                        if attempt:
                            raise NapCatError('napcat_auth_failed') from None
                        # A rejected credential means the action did not run;
                        # retry auth once after an upstream worker restart.
        except NapCatError as exc:
            if str(exc) in {'napcat_webui_rate_limited','napcat_auth_failed','napcat_webui_unauthorized'}:
                self._credential=None
                self._auth_retry_code = 'napcat_auth_failed' if str(exc)=='napcat_webui_unauthorized' else str(exc)
                if time.monotonic() >= self._auth_retry_after:
                    self._auth_retry_after=time.monotonic()+65
            raise
        except httpx.HTTPError:
            raise NapCatError("napcat_unavailable") from None

    async def status(self):
        data = await self._request("CheckLoginStatus")
        logged = data.get("isLogin") is True
        phase = data.get("loginPhase")
        out = {"available":True,"logged_in":False,"offline":data.get("isOffline") is True,
               "core_ready":data.get("coreReady") is True,"checked_at":time.time(),
               "state":"unknown","reason_code":"status_incomplete"}
        info = {}
        if logged:
            info = await self._request("GetQQLoginInfo")
            number = str(info.get("uin", ""))
            if re.fullmatch(r"[1-9][0-9]{0,19}",number):
                out.update(qq_number=number,nickname=str(info.get("nick", ""))[:100])
        if data.get('restarting') is True or phase == 'reconnecting':
            out.update(state='reconnecting',reason_code='reconnecting')
        elif out['offline'] or info.get('online') is False or phase == 'offline':
            out.update(state='offline',offline=True,reason_code='qq_offline')
        elif data.get('qrLoginAccepted') is True or phase in {'initializing','qrcode_scanned'}:
            out.update(state='initializing',reason_code='login_initializing')
        elif logged and out['core_ready'] and data.get('isOffline') is False and info.get('online') is True and out.get('qq_number'):
            out.update(state='online',logged_in=True,reason_code='online')
        elif data.get('isLogin') is False:
            out.update(state='login_required',reason_code='login_required')
        # Return fixed categories only; upstream loginError can include URLs or identifiers.
        error = data.get('loginError')
        if isinstance(error,str) and error:
            out['login_error_code'] = ('account_restricted' if any(word in error for word in ('风控','风险','冻结','限制登录')) else
                'session_expired' if any(word in error for word in ('失效','重新登录','被踢','其他设备')) else 'login_error')
        return out

    async def qr_svg(self) -> bytes:
        data = await self._request("GetQQLoginQrcode")
        return self._render_qr(data.get("qrcode"))

    async def refresh_qr_svg(self) -> bytes:
        """Explicitly refresh only an idle logged-out session; never restart it."""
        status = await self._request("CheckLoginStatus")
        if status.get("isLogin") is True:
            raise NapCatError("napcat_already_logged_in")
        if status.get("isLogin") is not False:
            raise NapCatError("napcat_login_status_unknown")
        phase = status.get("loginPhase")
        if phase is not None and not isinstance(phase,str):
            raise NapCatError("napcat_invalid_response")
        if any(status.get(field) is not None and not isinstance(status[field],bool) for field in ("restarting", "qrLoginAccepted")):
            raise NapCatError("napcat_invalid_response")
        if phase == "reconnecting" or status.get("restarting") is True:
            raise NapCatError("napcat_restarting")
        if status.get("qrLoginAccepted") is True or phase in {"qrcode_scanned", "initializing"}:
            raise NapCatError("napcat_login_in_progress")
        data = await self._request("RefreshQRcode")
        if data.get("restarting") is True:
            raise NapCatError("napcat_restarting")
        if data.get("restarting") is not None and data.get("restarting") is not False:
            raise NapCatError("napcat_invalid_response")
        return self._render_qr(data.get("qrcodeurl"))

    @staticmethod
    def _render_qr(value) -> bytes:
        if not isinstance(value,str) or not 1 <= len(value) <= 4096:
            raise NapCatError("napcat_qr_unavailable")
        try:
            parsed = urlsplit(value)
            if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port or not (
                parsed.hostname == "qq.com" or (parsed.hostname or "").endswith(".qq.com")
            ):
                raise ValueError()
        except ValueError:
            raise NapCatError("napcat_qr_untrusted") from None
        # Encode the URL as bits locally, never navigate to or fetch that URL.
        try:
            qr = qrcode.make(value,image_factory=SvgPathImage,border=4)
        except (DataOverflowError,ValueError):
            raise NapCatError("napcat_qr_unavailable") from None
        target = io.BytesIO()
        qr.save(target)
        return target.getvalue()

    async def bind_logged_account(self):
        status = await self.status()
        number = status.get("qq_number")
        if not status.get("logged_in") or status.get("offline") or not number:
            raise NapCatError("napcat_login_required")
        await asyncio.to_thread(self._bind_account, number)
        return {"bound":True,"qq_number":number,"auto_reply":False}

    def _bind_account(self, number):
        # Serialize the check and write across processes, not only requests in
        # this core. Two freshly logged-in instances cannot claim one QQ ID.
        try:
            with AccountRegistry(self.root).transaction(), settings_lock:
                assert_account_available(self.root, number)
                settings = read_settings(self.root)
                if settings.onebot_self_id != number:
                    settings.onebot_allowed_groups = []
                    settings.onebot_allowed_dm_users = []
                    settings.onebot_owners = []
                settings.transport = "onebot"
                settings.onebot_self_id = number
                settings.onebot_enabled = False
                settings.qq_enabled = False
                write_settings(self.root,settings)
        except AccountError as error:
            raise NapCatError(str(error)) from None
