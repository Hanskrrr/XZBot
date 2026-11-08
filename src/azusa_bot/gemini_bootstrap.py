"""Restricted launcher for the pinned official Gemini Balance core.

Standalone child entrypoint, not the upstream main.py: no automatic probes,
configuration export, key-bearing SQL logs, dashboard or scheduler. Upstream
chat conversion, HTTP client, key rotation and retry code remain unmodified.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time

COMMIT = "5512f7ff218a85970ecb643e2d04e44e7de96efd"


def build_app(source: Path, runtime: Path, keys: list[str], access_token: str):
    # Upstream logs include raw provider errors and partially redacted keys.
    # Suppress them entirely; record only numeric, aggregate metadata below.
    logging.disable(logging.CRITICAL)
    os.environ.update(DATABASE_TYPE="sqlite", SQLITE_DATABASE="unused.sqlite3", LOG_LEVEL="CRITICAL",
                      CHECK_INTERVAL_HOURS="0", ERROR_LOG_RECORD_REQUEST_BODY="false",
                      FILES_CLEANUP_ENABLED="false", MAX_RETRIES="2", TIME_OUT="25",
                      API_KEYS="[]", ALLOWED_TOKENS="[]", AUTH_TOKEN="", VERTEX_API_KEYS="[]",
                      PAID_KEY="", PROXIES="[]", CUSTOM_HEADERS="{}")
    sys.path.insert(0, str(source))
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
    from app.config.config import settings
    from app.domain.openai_models import ChatRequest
    from app.handler.retry_handler import RetryHandler
    from app.service.key.key_manager import KeyManager
    from app.service.openai_compatiable import openai_compatiable_service as service_module

    settings.API_KEYS = keys
    settings.ALLOWED_TOKENS = [access_token]
    settings.AUTH_TOKEN = access_token
    settings.BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
    manager = KeyManager(keys, [])
    service = service_module.OpenAICompatiableService(settings.BASE_URL, manager)

    db = runtime / "stats.sqlite3"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS counters (status INTEGER PRIMARY KEY, count INTEGER NOT NULL, last_at INTEGER NOT NULL)")
    db.chmod(0o600)

    async def no_raw_error(**kwargs):
        return None

    async def safe_request_count(**kwargs):
        status = kwargs.get("status_code")
        status = status if type(status) is int and 100 <= status <= 599 else 0
        with sqlite3.connect(db) as connection:
            connection.execute("INSERT INTO counters VALUES (?,1,?) ON CONFLICT(status) DO UPDATE SET count=count+1,last_at=excluded.last_at", (status, int(time.time())))

    # Explicit integration seam: functions imported by the official chat service.
    # Never initialize/synchronize upstream Settings or request/error SQL tables.
    service_module.add_error_log = no_raw_error
    service_module.add_request_log = safe_request_count

    @RetryHandler(key_arg="api_key")
    async def upstream_call(*, request, api_key, key_manager):
        return await service.create_chat_completion(request, api_key)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authorize(request: Request, call_next):
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied, "Bearer " + access_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if int(request.headers.get("content-length", "0") or "0") > 1024 * 1024:
            return JSONResponse({"error": "request_too_large"}, status_code=413)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.get("/health")
    async def health():
        return {"status": "ready", "commit": COMMIT}

    @app.post("/openai/v1/chat/completions")
    async def chat(request: dict):
        try:
            parsed = ChatRequest.model_validate(request)
            if parsed.stream or parsed.response_format != {"type": "json_object"}:
                raise HTTPException(400, "nonstream_json_required")
            # Keep normal safety defaults; the direct OpenAI compatibility route
            # does not inject the upstream native route's safety overrides.
            api_key = await manager.get_next_working_key()
            if not await manager.is_key_valid(api_key):
                raise HTTPException(503, "gemini_keys_unavailable")
            result = await upstream_call(request=parsed, api_key=api_key, key_manager=manager)
            return result
        except HTTPException:
            raise
        except Exception as error:
            candidate = error.args[0] if error.args else None
            status = candidate if type(candidate) is int and 400 <= candidate <= 599 else 502
            return JSONResponse({"error": "gemini_upstream_failed"}, status_code=status)

    return app


def main():
    os.umask(0o077)
    source, runtime = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    # One line; keep stdin open afterward so parent exit shuts down this child.
    line = sys.stdin.readline(300000)
    payload = json.loads(line)
    port = payload.get("port", 6201)
    if type(port) is not int or port not in {6201, 6221, 6241, 6261}:
        raise ValueError("invalid_gateway_port")
    app = build_app(source, runtime, payload["keys"], payload["token"])
    del payload, line
    import uvicorn
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
        log_config=None, access_log=False, log_level="critical"))

    def parent_watchdog():
        sys.stdin.read()
        server.should_exit = True

    threading.Thread(target=parent_watchdog, daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
