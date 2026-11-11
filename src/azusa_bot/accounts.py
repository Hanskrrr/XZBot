"""Independent QQ instances sharing installed code, never login/runtime data."""
from __future__ import annotations
from .host import python_at
from .host import private_info, private_path, protect, link_directory
from .host import safe_open, NOFOLLOW

import asyncio
from contextlib import contextmanager
from .host import file_locks as fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
from .native_process import terminate_helper
import threading
import time
import uuid
import unicodedata

import httpx
from fastapi import APIRouter, HTTPException, Request

from . import lore
from .bot_control import read_control
from .profiles import ProfileStore
from .runtime_layout import runtime_layout
from .settings import private_write, read_settings, token, write_settings


class AccountError(ValueError):
    pass


class AccountRegistry:
    MAX_CHILDREN = 3

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.layout = runtime_layout(self.root)
        self.manager_root = self.layout.manager_root
        self.path = self.manager_root / "data/accounts.json"
        self.lock = threading.RLock()

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
            raise AccountError("account_id_invalid")
        return value

    def child_root(self, number):
        self.identifier(number)
        base = self.manager_root / "accounts"
        child = base / number
        if base.is_symlink() or child.is_symlink():
            raise AccountError("account_directory_invalid")
        return child

    def read(self):
        if not self.path.exists() and not self.path.is_symlink():
            return []
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 65536 or not private_path(self.path):
                raise ValueError()
            data = json.loads(self.path.read_text())
            rows = data["accounts"]
            if data.get("schema") != 1 or not isinstance(rows, list) or len(rows) > self.MAX_CHILDREN:
                raise ValueError()
            ids, slots = set(), set()
            for row in rows:
                number = self.identifier(row["id"])
                if (number in ids or type(row["slot"]) is not int or not 1 <= row["slot"] <= self.MAX_CHILDREN
                        or row["slot"] in slots or not isinstance(row["name"], str) or not 1 <= len(row["name"]) <= 60):
                    raise ValueError()
                ids.add(number); slots.add(row["slot"])
                child_layout = runtime_layout(self.child_root(number))
                if child_layout.slot != row["slot"] or child_layout.manager_root != self.manager_root:
                    raise ValueError()
            return rows
        except (OSError, KeyError, TypeError, ValueError):
            raise AccountError("accounts_manifest_invalid") from None

    @contextmanager
    def transaction(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock:
            descriptor = safe_open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                yield

    def write(self, rows):
        private_write(self.path, json.dumps({"schema": 1, "accounts": rows}, ensure_ascii=False))

    def get(self, number):
        if number == "primary":
            return {"id": "primary", "name": "主账号", "slot": 0}, self.manager_root
        self.identifier(number)
        row = next((row for row in self.read() if row["id"] == number), None)
        if row is None:
            raise AccountError("account_not_found")
        return row, self.child_root(number)


def managed_qq_ids(root):
    """Trusted configured bot accounts, used to bound peer interaction."""
    registry = AccountRegistry(root)
    roots = [registry.manager_root, *(registry.child_root(row["id"]) for row in registry.read())]
    return {settings.onebot_self_id for directory in roots
            if (settings := read_settings(directory)).transport == "onebot" and settings.onebot_self_id}


def managed_bot_names(root) -> dict[str, tuple[str, ...]]:
    """Read active managed role names without initializing another account."""
    from .group_addressing import address_names
    registry = AccountRegistry(root)
    roots = [registry.manager_root, *(registry.child_root(row["id"]) for row in registry.read())]
    result = {}
    for directory in roots:
        settings = read_settings(directory)
        if settings.transport != "onebot" or not re.fullmatch(r"[1-9][0-9]{0,19}",settings.onebot_self_id):
            continue
        names = ()
        try:
            path = directory / "data/profiles.json"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 256*1024:
                raise ValueError()
            descriptor = safe_open(path,os.O_RDONLY | NOFOLLOW)
            with os.fdopen(descriptor,"r",encoding="utf-8") as stream:
                source = stream.read(256*1024+1)
            if len(source) > 256*1024:
                raise ValueError()
            data = json.loads(source)
            if (not isinstance(data,dict) or data.get("schema") != 1
                    or not isinstance(data.get("profiles"),list) or not 1 <= len(data["profiles"]) <= 32):
                raise ValueError()
            ids = [row["id"] for row in data["profiles"]]
            if (any(not isinstance(number,str) or not re.fullmatch(r"default|[a-f0-9]{32}",number) for number in ids)
                    or len(set(ids)) != len(ids) or "default" not in ids or data.get("active_id") not in ids):
                raise ValueError()
            active = next(row for row in data["profiles"] if row["id"] == data["active_id"])
            aliases = address_names(active.get("address_names",[]))
            display = active["name"]
            if (not isinstance(display,str) or not 1 <= len(display.strip()) <= 60
                    or any(unicodedata.category(c).startswith("C") for c in display)):
                raise ValueError()
            # Keep administrator spelling while collapsing compatibility forms.
            unique = {}
            for name in [*aliases,display.strip()]:
                unique.setdefault(unicodedata.normalize("NFKC",name).casefold(),name)
            names = tuple(unique.values())
        except (OSError,ValueError,KeyError,TypeError,UnicodeError):
            pass
        result[settings.onebot_self_id] = names
    return result


def assert_account_available(root, qq_number):
    registry = AccountRegistry(root)
    roots = [registry.manager_root, *(registry.child_root(row["id"]) for row in registry.read())]
    if any(directory.resolve() != Path(root).resolve() and read_settings(directory).onebot_self_id == qq_number
           for directory in roots):
        raise AccountError("qq_account_already_managed")


def _copy_content(source, destination):
    """Copy role files only, excluding every account's history/keys/login data."""
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_dir():
        raise AccountError("account_role_copy_failed")
    # Knowledge and asset mutations share this interprocess snapshot guard.
    # The copied manifest must reference exactly the blobs in this generation.
    with lore.backup_guard(source.parent):
        files, total = [], 0
        for path in source.rglob("*"):
            if path.is_symlink():
                raise AccountError("account_role_copy_failed")
            if path.is_file():
                total += path.stat().st_size
                files.append(path)
                if total > 250 * 1024 * 1024 or len(files) > 5000:
                    raise AccountError("account_role_too_large")
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        for path in files:
            output = destination / path.relative_to(source)
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with path.open("rb") as reader, output.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
            output.chmod(0o600)


class AccountManager:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.registry = AccountRegistry(self.root)
        self.tasks = {}
        self.operations = {}
        self.operation_lock = asyncio.Lock()

    def require_manager(self):
        if self.registry.layout.is_managed:
            raise AccountError("account_manager_only")

    def context(self):
        """Navigation metadata only; child consoles never receive parent credentials."""
        managed = self.registry.layout.is_managed
        entry, _ = self.registry.get(self.root.name if managed else "primary")
        return {"id": entry["id"], "name": entry["name"], "is_manager": not managed,
                "manager_url": runtime_layout(self.registry.manager_root).core_url}

    def _child(self, number):
        self.require_manager()
        if number == "primary":
            raise AccountError("account_use_current_controls")
        return self.registry.get(number)[1]

    @staticmethod
    def _profiles(root):
        # All account creation writes a manifest before publication; no Engine
        # is instantiated for inspection (doing so could interrupt live traces).
        return ProfileStore(root).list()

    async def _request(self, root, path, *, body=None):
        if path not in {"/admin/status", "/admin/qq-status", "/admin/bot-power/stop", "/admin/profiles/import-from-manager"} and not re.fullmatch(
                r"/admin/profiles/(?:default|[a-f0-9]{32})/activate", path):
            raise AccountError("account_operation_invalid")
        credential = token(root, "admin")
        try:
            async with httpx.AsyncClient(base_url=runtime_layout(root).core_url,
                    timeout=185 if path.endswith("/stop") else 60 if path.endswith("/import-from-manager") else 5,
                    trust_env=False, follow_redirects=False) as client:
                response = await client.request("GET" if body is None else "POST", path,
                    headers={"Authorization": "Bearer " + credential}, **({"json": body} if body is not None else {}))
                if response.status_code != 200:
                    raise AccountError("account_service_unavailable")
                data = response.json()
                if not isinstance(data, dict):
                    raise ValueError()
                if path == "/admin/status" and data.get("project_path") != str(root):
                    raise AccountError("account_service_mismatch")
                return data
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, AccountError):
                raise
            raise AccountError("account_service_unavailable") from None

    async def _status(self, entry, root):
        profiles = await asyncio.to_thread(self._profiles, root)
        settings = await asyncio.to_thread(read_settings, root)
        active = next(p for p in profiles["profiles"] if p["id"] == profiles["active_id"])
        try:
            await self._request(root, "/admin/status")
            running = True
        except AccountError:
            running = False
        connection = {'state':'unknown','message':'管理服务不可达，QQ 状态无法确认'}
        if running:
            try:
                connection = await self._request(root, '/admin/qq-status')
            except AccountError:
                connection = {'state':'unknown','message':'QQ 状态读取失败'}
        operation = self.operations.get(entry["id"], {})
        power = read_control(root)
        from .qq_runtime import describe as describe_runtime
        qq_runtime=describe_runtime(root)
        runtime_ready=(qq_runtime['environment']=='external' or qq_runtime['prepared'].get(
            qq_runtime['environment']+'_'+qq_runtime['adapter'],False))
        return dict(entry, core_port=runtime_layout(root).core_port, core_running=running, qq_connection=connection,
            prepared=runtime_ready, qq_runtime=qq_runtime['environment']+'/'+qq_runtime['adapter'],
            busy=entry["id"] in self.tasks, operation=operation.get("operation", ""),
            error_code=operation.get("error_code", ""), qq_number=settings.onebot_self_id,
            active_profile_id=active["id"], active_profile_name=active["name"],
            profiles=[{"id": p["id"], "name": p["name"]} for p in profiles["profiles"]],
            power_state=power["state"], power_error_code=power["error_code"],
            replies_enabled=settings.onebot_enabled if settings.transport == "onebot" else settings.qq_enabled)

    async def status(self):
        managed = self.registry.layout.is_managed
        if managed:
            entry, root = self.registry.get(self.root.name)
            rows = [(entry, root)]
        else:
            rows = [self.registry.get("primary"), *(self.registry.get(row["id"]) for row in self.registry.read())]
        profiles = await asyncio.to_thread(self._profiles, self.root)
        return {"is_manager": not managed, "context": self.context(),
            "accounts": await asyncio.gather(*(self._status(row, root) for row, root in rows)),
            "seed_profiles": [{"id": p["id"], "name": p["name"]} for p in profiles["profiles"]]}

    async def management_url(self, number):
        """The manager may open a child, but opening never enables QQ activity.

        Let the requesting browser navigate, rather than launching the host's
        default browser via AppleScript. The fragment is consumed immediately
        by the child UI and must never be persisted or logged.
        """
        root = self._child(number)
        try:
            await self._request(root, "/admin/status")
        except AccountError:
            async with self.operation_lock:
                if number in self.tasks:
                    raise AccountError("account_busy")
                self.tasks[number] = asyncio.current_task()
            try:
                await self._run(root, "ensure-core")
                await self._request(root, "/admin/status")
            finally:
                self.tasks.pop(number, None)
        return {"url": runtime_layout(root).core_url + "/#token=" + token(root, "admin")}

    async def prepare_login(self):
        if not self.registry.layout.is_managed:
            raise AccountError("account_use_current_controls")
        number = self.root.name
        async with self.operation_lock:
            if number in self.tasks:
                raise AccountError("account_busy")
            self.operations[number] = {"operation": "prepare", "error_code": ""}
            self.tasks[number] = asyncio.create_task(self._operation(number, "prepare", self.root, {}))
        return {"accepted": True, "operation": "prepare"}

    def create(self, body):
        self.require_manager()
        if not isinstance(body, dict) or set(body) != {"name", "profile_id"}:
            raise AccountError("account_fields_invalid")
        name = body["name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name):
            raise AccountError("account_name_invalid")
        source = ProfileStore(self.root)
        profile = source.get(body["profile_id"])
        with self.registry.transaction(), source.lock:
            rows = self.registry.read()
            if len(rows) >= self.registry.MAX_CHILDREN:
                raise AccountError("account_limit_reached")
            slot = next(slot for slot in range(1, 4) if slot not in {r["slot"] for r in rows})
            number = uuid.uuid4().hex
            directory = self.registry.child_root(number)
            directory.mkdir(parents=True, mode=0o700)
            try:
                private_write(directory / "config/instance.json", json.dumps({"schema": 1, "slot": slot, "manager_root": str(self.root)}))
                from .external_runtime import enabled as external_runtime
                if external_runtime(self.root):private_write(directory/'config/runtime.json',json.dumps({'mode':'external'}))
                _copy_content(source.directory(profile["id"]) / "content", directory / "content")
                original = read_settings(self.root)
                values = original.model_dump() | {"transport": "onebot", "app_id": "", "allowed_groups": [],
                    "allowed_dm_users": [], "owners": [], "qq_enabled": False, "onebot_enabled": False,
                    "onebot_self_id": "", "onebot_allowed_groups": [], "onebot_allowed_dm_users": [], "onebot_owners": []}
                values.update(profile["voice"])
                write_settings(directory, type(original).model_validate(values))
                initial = dict(profile, id="default", created_at=time.time(), updated_at=time.time())
                private_write(directory / "data/profiles.json", json.dumps({"schema": 1, "active_id": "default", "profiles": [initial]}, ensure_ascii=False))
                private_write(directory / "data/bot-control.json", json.dumps({"schema": 1, "state": "stopped", "epoch": uuid.uuid4().hex, "updated_at": time.time(), "error_code": ""}))
                for filename in ("src", "scripts", "ui", ".venv", "astrbot_plugin_azusa_onebot", "astrbot_plugin_azusa_bridge"):
                    target = self.root / filename
                    if target.exists():
                        link_directory(directory / filename,target)
                # Reuse installed code, never the primary AstrBot data/config.
                for filename in ("astrbot-source.path", "keychain-service"):
                    path = self.root / "data" / filename
                    if path.is_file() and not path.is_symlink():
                        private_write(directory / "data" / filename, path.read_text())
                if not (directory / "data/keychain-service").exists():
                    from .credential_store import CredentialStore
                    private_write(directory / "data/keychain-service", CredentialStore(self.root).service)
                (directory / "runtime").mkdir(mode=0o700)
                for filename in ("downloads", "gemini-balance", "gemini-balance-venv"):
                    target = self.root / "runtime" / filename
                    if target.is_dir():
                        link_directory(directory / 'runtime' / filename,target)
                for purpose in ("admin", "bridge", "onebot"):
                    token(directory, purpose)
                entry = {"id": number, "name": name.strip(), "slot": slot}
                self.registry.write([*rows, entry])
                return entry
            except Exception:
                shutil.rmtree(directory)
                raise

    async def copy_profile(self, number, body):
        root = self._child(number)
        if not isinstance(body, dict) or set(body) != {"profile_id"}:
            raise AccountError("account_fields_invalid")
        ProfileStore(self.root).get(body["profile_id"])
        # The destination process owns its ProfileStore lock. Never write its
        # manifest from the manager while that UI could be editing it.
        async with self.operation_lock:
            if number in self.tasks:
                raise AccountError("account_busy")
            self.tasks[number] = asyncio.current_task()
            self.operations[number] = {"operation": "copy-profile", "error_code": ""}
        try:
            await self._run(root, "ensure-core")
            return await self._request(root, "/admin/profiles/import-from-manager", body=body)
        finally:
            self.tasks.pop(number, None)

    async def _run(self, root, action):
        if action not in {"open", "ensure-core", "prepare"}:
            raise AccountError("account_operation_invalid")
        script = root / "scripts/account_instance_action.py"
        python = python_at(root / ".venv")
        if not script.is_file() or not python.is_file():
            raise AccountError("account_runtime_unavailable")
        environment = os.environ.copy()
        environment.update(AZUSA_ROOT=str(root), PYTHONPATH=str(root / "src"))
        process = await asyncio.create_subprocess_exec(str(python), str(script), action, cwd=root, env=environment,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True)
        try:
            result = await asyncio.wait_for(process.wait(), timeout=1200 if action == "prepare" else 60)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            try:
                terminate_helper(process)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                try:
                    terminate_helper(process,force=True)
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
        if result:
            raise AccountError("account_operation_failed")

    async def _operation(self, number, action, root, body):
        try:
            if action in {"open", "prepare"}:
                await self._run(root, action)
            else:
                await self._run(root, "ensure-core")
                if action == "stop":
                    await self._request(root, "/admin/bot-power/stop", body={})
                elif action == "profile":
                    profile_id = body.get("profile_id")
                    ProfileStore(root).get(profile_id)
                    await self._request(root, "/admin/profiles/" + profile_id + "/activate", body={})
            self.operations[number] = {"operation": action, "error_code": ""}
        except asyncio.CancelledError:
            self.operations[number] = {"operation": action, "error_code": "account_operation_interrupted"}
            raise
        except Exception as error:
            code = str(error) if isinstance(error, AccountError) else "account_operation_failed"
            self.operations[number] = {"operation": action, "error_code": code}
        finally:
            self.tasks.pop(number, None)

    async def operate(self, number, action, body):
        root = self._child(number)
        if action not in {"open", "prepare", "stop", "profile"} or not isinstance(body, dict) or set(body) != ({"profile_id"} if action == "profile" else set()):
            raise AccountError("account_operation_invalid")
        if action == "profile":
            ProfileStore(root).get(body["profile_id"])
        async with self.operation_lock:
            if number in self.tasks:
                raise AccountError("account_busy")
            self.operations[number] = {"operation": action, "error_code": ""}
            self.tasks[number] = asyncio.create_task(self._operation(number, action, root, body))
        return {"accepted": True, "operation": action}

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_router(manager):
    router = APIRouter(prefix="/admin/accounts")

    async def body(request):
        try:
            value = await request.json()
        except ValueError:
            raise HTTPException(400, "account_fields_invalid") from None
        if not isinstance(value, dict):
            raise HTTPException(400, "account_fields_invalid")
        return value

    @router.get("")
    async def listing():
        return await manager.status()

    @router.post("")
    async def create(request: Request):
        return await asyncio.to_thread(manager.create, await body(request))

    @router.post("/{number}/copy-profile")
    async def copy_profile(number: str, request: Request):
        return await manager.copy_profile(number, await body(request))

    @router.post("/{number}/management")
    async def management(number: str, request: Request):
        if await body(request):
            raise HTTPException(400, "account_fields_invalid")
        return await manager.management_url(number)

    @router.post("/current/prepare")
    async def prepare_current(request: Request):
        if await body(request):
            raise HTTPException(400, "account_fields_invalid")
        return await manager.prepare_login()

    @router.post("/{number}/{action}", status_code=202)
    async def operation(number: str, action: str, request: Request):
        return await manager.operate(number, action, await body(request))

    return router


def import_manager_profile(root, target, body):
    """Run in the destination core under its existing in-process profile lock."""
    layout = runtime_layout(root)
    if not layout.is_managed or not isinstance(body, dict) or set(body) != {"profile_id"}:
        raise AccountError("account_operation_invalid")
    source = ProfileStore(layout.manager_root)
    profile = source.get(body["profile_id"])
    with target.lock:
        data = target._read()
        if len(data["profiles"]) >= 32:
            raise AccountError("profile_limit_reached")
        new_id = uuid.uuid4().hex
        destination = target.directory(new_id)
        try:
            _copy_content(source.directory(profile["id"]) / "content", destination / "content")
            clone = dict(profile, id=new_id, created_at=time.time(), updated_at=time.time())
            data["profiles"].append(clone)
            target._write(data)
        except Exception:
            if destination.exists():
                shutil.rmtree(destination)
            raise
        return {"copied": True, "profile_id": new_id, "activated": False}
