"""Local, opt-in Gemini Balance lifecycle and write-only Keychain configuration.

No method here contacts a model API. Installation fetches pinned public source
and packages; health checks address only the child on 127.0.0.1.
"""
from __future__ import annotations
from .host import python_at

import asyncio
import json
import os
from pathlib import Path
import socket
import sys

import httpx
from fastapi import APIRouter, HTTPException, Request

from .credential_store import CredentialStore, CredentialStoreError
from .settings import Settings, private_write, read_settings, settings_lock, token, write_settings
from .runtime_layout import runtime_layout

COMMIT = "5512f7ff218a85970ecb643e2d04e44e7de96efd"
VERSION = "2.2.8"
PORT = 6201
BASE_URL = f"http://127.0.0.1:{PORT}/openai/v1"


class GatewayError(RuntimeError):
    """Only fixed, non-sensitive error codes leave this boundary."""


def validate_keys(value) -> list[str]:
    if (not isinstance(value, list) or not 1 <= len(value) <= 50
        or any(not isinstance(key, str) or not 10 <= len(key) <= 4096
               or any(not 33 <= ord(c) <= 126 for c in key) for key in value)):
        raise GatewayError("invalid_gemini_keys")
    return list(dict.fromkeys(value))


def saved_keys(root: Path) -> list[str]:
    raw = CredentialStore(root).get("gemini_keys")
    if not raw:
        return []
    try:
        return validate_keys(json.loads(raw))
    except (ValueError, TypeError, GatewayError):
        raise GatewayError("gemini_credentials_invalid") from None


class GeminiGateway:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.layout = runtime_layout(self.root)
        self.source = self.layout.manager_root / "runtime/gemini-balance"
        self.venv = self.layout.manager_root / "runtime/gemini-balance-venv"
        self.runtime = self.root / "runtime/gemini-gateway"
        self.process = None
        self.lock = asyncio.Lock()
        self.last_error = ""
        self.starting = False

    def installed(self):
        marker = self.layout.manager_root / "runtime/gemini-gateway/installation.json"
        try:
            value = json.loads(marker.read_text())
            return (value == {"commit": COMMIT, "version": VERSION}
                    and (python_at(self.venv)).exists()
                    and (self.source / "app/router/openai_compatiable_routes.py").is_file())
        except (OSError, ValueError):
            return False

    def enabled(self):
        try:
            return json.loads((self.runtime / "control.json").read_text()) == {"enabled": True}
        except (OSError, ValueError):
            return False

    def _set_enabled(self, enabled):
        private_write(self.runtime / "control.json", json.dumps({"enabled": enabled}))

    def describe(self):
        keys = saved_keys(self.root)
        running = self.process is not None and self.process.returncode is None
        if self.process is not None and self.process.returncode is not None:
            self.last_error = "gemini_process_exited"
        installed = self.installed()
        state = ("starting" if self.starting else "running" if running else
                 "error" if self.last_error else "stopped" if installed else "not_installed")
        return {"settings": {"model": read_settings(self.root).gemini_model},
                "credentials": {"configured": bool(keys), "key_count": len(keys), "storage": "迁移凭据文件（未加密）" if CredentialStore(self.root).storage_kind()=="private_file" else "macOS Keychain"},
                "runtime": {"installed": installed, "running": running, "state": state,
                            "enabled": self.enabled(),
                            "port": self.layout.gemini_port, "base_url": self.layout.gemini_url, "version": VERSION,
                            "commit": COMMIT, "last_error": self.last_error}}

    async def _run(self, *args, cwd=None, timeout=300):
        child = await asyncio.create_subprocess_exec(*map(str, args), cwd=cwd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            env={k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "SSL_CERT_FILE"}})
        try:
            code = await asyncio.wait_for(child.wait(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            child.kill()
            await child.wait()
            raise
        if code:
            raise GatewayError("gemini_install_command_failed")

    async def _verify_source(self):
        child = await asyncio.create_subprocess_exec("git", "rev-parse", "HEAD", cwd=self.source,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        output, _ = await child.communicate()
        if child.returncode or output.decode().strip() != COMMIT:
            raise GatewayError("gemini_source_modified")
        child = await asyncio.create_subprocess_exec("git", "diff", "--quiet", COMMIT, "--", cwd=self.source,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        if await child.wait():
            raise GatewayError("gemini_source_modified")

    async def install(self):
        if self.layout.is_managed:
            if not self.installed():
                raise GatewayError("gemini_install_requires_manager")
            return await asyncio.to_thread(self.describe)
        async with self.lock:
            if self.process is not None and self.process.returncode is None:
                raise GatewayError("gemini_stop_before_install")
            try:
                self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
                if not self.source.exists():
                    await self._run("git", "clone", "--no-checkout", "https://github.com/snailyp/gemini-balance.git", self.source)
                # Never update a moving branch or silently overwrite a modified checkout.
                check = await asyncio.create_subprocess_exec("git", "diff", "--quiet", COMMIT, "--",
                    cwd=self.source, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                if await check.wait():
                    # A fresh --no-checkout clone has no working tree yet.
                    if (self.source / "app").exists():
                        raise GatewayError("gemini_source_modified")
                await self._run("git", "checkout", "--detach", COMMIT, cwd=self.source)
                if not (python_at(self.venv)).exists():
                    await self._run(sys.executable, "-m", "venv", self.venv)
                await self._run(python_at(self.venv), "-m", "pip", "install", "--disable-pip-version-check",
                                "--no-cache-dir", "-r", self.root / "requirements-gemini-balance.lock")
                private_write(self.runtime / "installation.json", json.dumps({"commit": COMMIT, "version": VERSION}))
                self.last_error = ""
            except GatewayError:
                self.last_error = "gemini_install_failed"
                raise
            except Exception:
                self.last_error = "gemini_install_failed"
                raise GatewayError(self.last_error) from None
            return await asyncio.to_thread(self.describe)

    async def save(self, body: dict):
        if not isinstance(body, dict) or set(body) - {"settings", "keys", "clear_keys"}:
            raise GatewayError("invalid_gemini_fields")
        changes = body.get("settings", {})
        if not isinstance(changes, dict) or set(changes) - {"model"} or type(body.get("clear_keys", False)) is not bool:
            raise GatewayError("invalid_gemini_fields")
        if "keys" in body and body.get("clear_keys"):
            raise GatewayError("invalid_gemini_fields")
        keys = validate_keys(body["keys"]) if "keys" in body else None
        try:
            model = Settings(gemini_model=changes["model"]).gemini_model if "model" in changes else None
        except ValueError:
            raise GatewayError("invalid_gemini_model") from None
        async with self.lock:
            replace = keys is not None or body.get("clear_keys", False)
            store = CredentialStore(self.root)
            previous = await asyncio.to_thread(store.get, "gemini_keys") if replace else None
            if replace:
                await self._stop()
                self._set_enabled(False)
            try:
                if replace:
                    await asyncio.to_thread(store.set, "gemini_keys", json.dumps(keys) if keys else "")
                if model is not None:
                    with settings_lock:
                        current = read_settings(self.root)
                        current.gemini_model = model
                        write_settings(self.root, current)
            except Exception:
                if replace:
                    try:
                        await asyncio.to_thread(store.set, "gemini_keys", previous)
                    except Exception:
                        raise GatewayError("gemini_save_rollback_incomplete") from None
                raise GatewayError("gemini_save_failed") from None
            self.last_error = ""
            return await asyncio.to_thread(self.describe)

    async def start(self):
        async with self.lock:
            if self.process is not None and self.process.returncode is None:
                return await asyncio.to_thread(self.describe)
            if not self.installed():
                raise GatewayError("gemini_not_installed")
            await self._verify_source()
            keys = await asyncio.to_thread(saved_keys, self.root)
            if not keys:
                raise GatewayError("gemini_keys_required")
            # Refuse to contact or terminate any other process on this port.
            try:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", self.layout.gemini_port))
            except OSError:
                raise GatewayError("gemini_port_in_use") from None
            self.starting = True
            try:
                self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
                access = token(self.runtime, "access")
                self.process = await asyncio.create_subprocess_exec(python_at(self.venv), "-I",
                    Path(__file__).with_name("gemini_bootstrap.py"), str(self.source), str(self.runtime),
                    cwd=self.runtime, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "SSL_CERT_FILE"}})
                # Anonymous inherited pipe: neither argv nor env nor disk contains cloud keys.
                self.process.stdin.write((json.dumps({"keys": keys, "token": access, "port": self.layout.gemini_port}) + "\n").encode())
                await self.process.stdin.drain()
                async with httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
                    for _ in range(80):
                        if self.process.returncode is not None:
                            raise GatewayError("gemini_start_failed")
                        try:
                            response = await client.get(f"http://127.0.0.1:{self.layout.gemini_port}/health",
                                headers={"Authorization": "Bearer " + access})
                            if response.status_code == 200 and response.json() == {"status": "ready", "commit": COMMIT}:
                                self.last_error = ""
                                self._set_enabled(True)
                                return await asyncio.to_thread(self.describe)
                        except (httpx.HTTPError, ValueError):
                            pass
                        await asyncio.sleep(0.1)
                raise GatewayError("gemini_start_timeout")
            except BaseException as error:
                await self._stop()
                self.last_error = "gemini_start_failed"
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise GatewayError(self.last_error) from None
            finally:
                self.starting = False

    async def _stop(self):
        child = self.process
        if child is not None and child.returncode is None:
            if child.stdin:
                child.stdin.close()
            try:
                await asyncio.wait_for(child.wait(), 3)
            except asyncio.TimeoutError:
                child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), 3)
                except asyncio.TimeoutError:
                    child.kill()
                    await child.wait()
        self.process = None

    async def stop(self):
        async with self.lock:
            await self._stop()
            self._set_enabled(False)
            self.last_error = ""
            return await asyncio.to_thread(self.describe)

    async def restore(self):
        """Restore a previous explicit start; never test keys or call the cloud."""
        if self.enabled():
            try:
                await self.start()
            except Exception:
                self.last_error = "gemini_restore_failed"

    async def close(self):
        async with self.lock:
            await self._stop()


def create_router(gateway: GeminiGateway) -> APIRouter:
    """Caller MUST mount this router with its existing admin dependency."""
    router = APIRouter(prefix="/admin/gemini")

    async def action(operation, *args):
        try:
            return await operation(*args)
        except (GatewayError, CredentialStoreError) as error:
            raise HTTPException(400, str(error)) from None
        except Exception:
            raise HTTPException(500, "gemini_internal_error") from None

    @router.get("")
    async def describe():
        try:
            return await asyncio.to_thread(gateway.describe)
        except (GatewayError, CredentialStoreError) as error:
            raise HTTPException(400, str(error)) from None

    @router.put("")
    async def save(request: Request):
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise HTTPException(400, "invalid_gemini_fields") from None
        return await action(gateway.save, body)

    @router.post("/install")
    async def install():
        return await action(gateway.install)

    @router.post("/start")
    async def start():
        return await action(gateway.start)

    @router.post("/stop")
    async def stop():
        return await action(gateway.stop)

    return router
