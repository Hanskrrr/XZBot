from __future__ import annotations

import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid
import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles

from . import __version__
from .core import Engine, Incoming
from .profiles import ProfileEngineManager, ProfileError, VOICE_FIELDS, VOICE_STORED
from .memory_admin import create_router as memory_router
from .trajectory_admin import create_router as trajectory_router
from .assets_admin import create_router as assets_router
from .secrets import load_credentials, resolve_credentials
from .credential_store import CredentialStoreError
from . import provider_config, lore
from .napcat_admin import NapCatAdmin, NapCatError
from .bilibili import BilibiliSubscriptions, BilibiliError
from .subscription_commands import handle_subscription_command
from .desktop import DesktopService
from .gemini_gateway import GeminiGateway, create_router as gemini_router
from .settings import Settings, private_write, read_settings, root_path, token, write_settings, settings_lock
from .bot_control import BotPowerController, BotPowerError, activity_allowed
from .runtime_layout import runtime_layout
from .accounts import AccountManager, AccountRegistry, AccountError, create_router as accounts_router, import_manager_profile, assert_account_available


class MemoryInput(BaseModel):
    scope: str = Field(min_length=1, max_length=512)
    user: str = Field(min_length=1, max_length=160)
    text: str = Field(default="", max_length=8000)
    kind: str = "personal"
    subject: str | None = None


def create_app(root: Path | None = None) -> FastAPI:
    root = root or root_path()
    layout = runtime_layout(root)
    os.umask(0o077)
    engine = ProfileEngineManager(root)
    gateway = GeminiGateway(root)
    subscriptions = BilibiliSubscriptions(root)
    from .role_activity import selected_sources
    subscriptions.activity_sources = lambda: set(selected_sources(engine.profiles))
    subscriptions.activity_usage = lambda: selected_sources(engine.profiles,enabled_only=False)
    from .role_activity import sync_all
    subscriptions.activity_sync = lambda keys,backfill=False: sync_all(engine,subscriptions,only_sources=keys,backfill=backfill)
    accounts = AccountManager(root)
    from .updates.service import UpdateService
    updates = UpdateService(root)
    from .qq_connection import QQConnection
    qq_connection = QQConnection(root)
    def invalidate_activity():
        engine.invalidate_activity()
        subscriptions.cancel_pending()
    power = BotPowerController(root, invalidate_activity)
    from .bot_runtime import NativeBotRuntime
    runtime = NativeBotRuntime(root)
    power.runtime_stop = runtime.stop
    power.runtime_start = runtime.start
    async def subscription_command(event, owner):
        return await handle_subscription_command(subscriptions, event, owner)
    engine.subscription_handler = subscription_command
    async def poll_subscriptions():
        while True:
            try:
                if activity_allowed(root):
                    await subscriptions.poll_once()
            except Exception:
                engine.diagnostics.record("core","internal_failure")
            await asyncio.sleep(15)

    async def poll_memory():
        # One bounded extraction per minute, only from an explicitly consenting
        # member in the currently selected transport's live whitelist.
        while True:
            await asyncio.sleep(60)
            if not activity_allowed(root):
                continue
            with engine.pin() as current:
                try:
                    await current.summarize_due()
                except Exception:
                    current.diagnostics.record("summary","summary_invalid")

    @asynccontextmanager
    async def lifespan(app):
        await gateway.restore()
        task = asyncio.create_task(poll_subscriptions())
        memory_task = asyncio.create_task(poll_memory())
        connection_task = asyncio.create_task(qq_connection.run())
        update_task = asyncio.create_task(updates.run())
        try:
            yield
        finally:
            update_task.cancel()
            with suppress(asyncio.CancelledError):
                await update_task
            connection_task.cancel()
            with suppress(asyncio.CancelledError):
                await connection_task
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            memory_task.cancel()
            with suppress(asyncio.CancelledError):
                await memory_task
            subscriptions.close()
            await gateway.close()
            await accounts.close()
    admin_token, bridge_token = token(root, "admin"), token(root, "bridge")
    app = FastAPI(title="XZBot", docs_url=None, redoc_url=None, openapi_url=None,lifespan=lifespan)
    app.state.engine = engine
    app.state.gateway = gateway
    app.state.subscriptions = subscriptions
    app.state.accounts = accounts
    app.state.qq_connection = qq_connection
    app.state.bot_power = power
    app.state.updates = updates
    app.mount("/static", StaticFiles(directory=root / "ui", check_dir=False), name="static")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    @app.middleware("http")
    async def limits(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in {layout.core_url, f"http://localhost:{layout.core_port}", "http://testserver"}:
            return JSONResponse({"detail":"origin_forbidden"}, status_code=403)
        max_body = 8 * 1024 * 1024 if request.url.path in {"/admin/knowledge/import"} or (request.method == "PATCH" and request.url.path.startswith("/admin/knowledge/")) else 250000
        if request.method == "POST" and request.url.path == "/admin/assets":
            max_body = 5 * 1024 * 1024
        try:
            if int(request.headers.get("content-length", "0")) > max_body:
                return JSONResponse({"detail":"body_too_large"}, status_code=413)
        except ValueError:
            return JSONResponse({"detail":"invalid_length"}, status_code=400)
        if request.method in {"POST","PUT","PATCH"}:
            body = await request.body()
            if len(body) > max_body:
                return JSONResponse({"detail":"body_too_large"}, status_code=413)
        with engine.pin():
            requested_profile=request.headers.get("x-xzbot-profile")
            if (request.method in {"POST","PUT","PATCH","DELETE"} and request.url.path.startswith("/admin/")
                    and not request.url.path.startswith("/admin/bot-power/")
                    and requested_profile is not None and requested_profile!=engine.profile_id):
                return JSONResponse({"detail":"profile_changed"},status_code=409)
            response = await call_next(request)
            response.headers["X-XZBot-Profile"] = engine.profile_id
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default response echoes submitted values; do not expose credentials.
        return JSONResponse({"detail":"invalid_request_fields"}, status_code=422)

    @app.exception_handler(PermissionError)
    async def forbidden(request, exc):
        return JSONResponse({"detail":"operation_forbidden"}, status_code=403)

    @app.exception_handler(ValueError)
    async def bad_input(request, exc):
        safe_knowledge_errors = {"knowledge_always_include_unverified", "knowledge_always_include_limit",
                                 "knowledge_always_include_invalid"}
        code = str(exc) if str(exc) in safe_knowledge_errors else "invalid_operation"
        return JSONResponse({"detail":code}, status_code=400)

    @app.exception_handler(ProfileError)
    async def profile_error(request,exc):
        code=str(exc)
        return JSONResponse({"detail":code},status_code=404 if code=="profile_not_found" else
                            409 if code in {"profile_busy","profile_cannot_delete_default_or_active"} else 400)

    @app.exception_handler(CredentialStoreError)
    async def keychain_error(request, exc):
        return JSONResponse({"detail":"keychain_operation_failed"}, status_code=503)

    @app.exception_handler(AccountError)
    async def account_error(request, exc):
        code = str(exc)
        return JSONResponse({"detail": code}, status_code=409 if code in {"account_busy", "qq_account_already_managed"}
            else 403 if code == "account_manager_only" else 400)

    @app.exception_handler(BilibiliError)
    async def bilibili_error(request, exc):
        details = exc.public_details()
        retry = details.get("retry_after_seconds", 0)
        return JSONResponse(details, status_code=503 if retry else 502,
                            headers={"Retry-After": str(retry)} if retry else None)

    def auth(expected):
        def check(request: Request):
            value = request.headers.get("Authorization", "")
            if not hmac.compare_digest(value.encode("utf-8"), ("Bearer " + expected).encode("utf-8")):
                raise HTTPException(401, "authentication_required")
        return check
    admin = auth(admin_token)
    bridge = auth(bridge_token)
    from .subscription_library import create_router as subscription_library_router
    app.include_router(subscription_library_router(subscriptions,engine.profiles), dependencies=[Depends(admin)])
    app.include_router(gemini_router(gateway), dependencies=[Depends(admin)])
    app.include_router(memory_router(engine), dependencies=[Depends(admin)])
    from .memory_policies import create_router as memory_policy_router
    from .memory_presets import create_router as memory_preset_router
    app.include_router(memory_policy_router(engine),dependencies=[Depends(admin)])
    app.include_router(memory_preset_router(engine),dependencies=[Depends(admin)])
    from .group_policies import create_router as group_policies_router
    app.include_router(group_policies_router(engine), dependencies=[Depends(admin)])
    from .group_presets import create_router as group_presets_router
    app.include_router(group_presets_router(engine),dependencies=[Depends(admin)])
    from .prompts import create_router as prompts_router
    app.include_router(prompts_router(engine), dependencies=[Depends(admin)])
    from .conversation_config import create_router as conversation_router
    app.include_router(conversation_router(engine), dependencies=[Depends(admin)])
    app.include_router(trajectory_router(engine), dependencies=[Depends(admin)])
    app.include_router(assets_router(engine), dependencies=[Depends(admin)])
    app.include_router(accounts_router(accounts), dependencies=[Depends(admin)])
    from .updates.router import create_router as updates_router
    app.include_router(updates_router(updates), dependencies=[Depends(admin)])

    @app.get("/admin/bot-power", dependencies=[Depends(admin)])
    def bot_power_status():
        return power.status()

    @app.post("/admin/bot-power/{operation}", dependencies=[Depends(admin)])
    async def bot_power_change(operation: str, request: Request):
        if operation not in {"start", "stop"}:
            raise HTTPException(404, "power_operation_unknown")
        if await json_object(request) != {}:
            raise HTTPException(400, "power_fields_invalid")
        try:
            return await getattr(power, operation)()
        except BotPowerError as exc:
            raise HTTPException(503, str(exc)) from None

    async def json_object(request: Request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "invalid_json_object") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "invalid_json_object")
        return body

    @app.get("/")
    def index():
        return FileResponse(root / "ui/index.html")

    @app.get("/health")
    def health():
        return {"ok":True, "version":__version__}

    @app.get("/admin/status", dependencies=[Depends(admin)])
    def status():
        settings = engine.settings()
        credentials = resolve_credentials(root,settings,load_credentials(settings.read_existing_dsh))[0]
        if engine.profile_id != "default":
            credentials.fish_reference=settings.fish_reference_id
            credentials.fish_model=settings.fish_model
        credential_status = credentials.status()
        if settings.llm_provider == "openai_compatible":
            credential_status["compatible"] = bool(credentials.compatible_key)
        return {"version":__version__, "project_path":str(root.resolve()), "settings":settings.model_dump(),
                "account_context":accounts.context(),
                "active_profile_id":engine.profile_id, "active_profile_name":engine.profiles.get(engine.profile_id)["name"],
                "credentials":credential_status,
                "stats":engine.memory.stats(), "group_stats":engine.groups.get_stats(), "qq_connected":False,
                "transport":settings.transport,
                "transport_enabled":settings.onebot_enabled if settings.transport == "onebot" else settings.qq_enabled,
                "official_qq_credentials_required":settings.transport == "official",
                "qq_note":"核心不持有QQ连接；实际连接状态请在NapCat与AstrBot中确认。" if settings.transport == "onebot" else "核心不持有QQ连接；实际连接状态请在AstrBot中确认。",
                "runtime":"cloud APIs + SQLite; no local inference models"}

    @app.get("/admin/cloud-budget", dependencies=[Depends(admin)])
    def cloud_budget_get():
        return engine.ledger.cloud_budget(read_settings(root).daily_cloud_requests)

    @app.put("/admin/cloud-budget", dependencies=[Depends(admin)])
    async def cloud_budget_put(request: Request):
        body = await json_object(request)
        value = body.get("daily_cloud_requests")
        if set(body) != {"daily_cloud_requests"} or type(value) is not int or not 1 <= value <= 10000:
            raise HTTPException(400, "invalid_cloud_budget")
        with settings_lock:
            latest = read_settings(root)
            write_settings(root, Settings.model_validate(latest.model_dump() | {"daily_cloud_requests": value}))
        return cloud_budget_get()

    @app.get("/admin/settings", dependencies=[Depends(admin)])
    def settings_get():
        return engine.settings().model_dump()

    @app.get("/admin/profiles", dependencies=[Depends(admin)])
    def profiles_get():
        return engine.profiles.list()

    @app.post("/admin/profiles", dependencies=[Depends(admin)])
    async def profiles_create(request: Request):
        return engine.profiles.create(await json_object(request))

    @app.get('/admin/profile-templates', dependencies=[Depends(admin)])
    def profile_templates():
        from .starter_templates import catalog
        return {'templates':catalog()}

    @app.post('/admin/profile-templates/{key}', dependencies=[Depends(admin)])
    async def profile_template_import(key: str):
        from .starter_templates import import_profile
        from starlette.concurrency import run_in_threadpool
        if key not in {'azusa','hazel'}:raise HTTPException(404,'starter_profile_not_found')
        return await run_in_threadpool(import_profile,engine.profiles,key)

    @app.post("/admin/profiles/import-from-manager", dependencies=[Depends(admin)])
    async def profiles_import(request: Request):
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(import_manager_profile, root, engine.profiles, await json_object(request))

    @app.patch("/admin/profiles/{profile_id}", dependencies=[Depends(admin)])
    async def profiles_update(profile_id: str, request: Request):
        result=engine.profiles.update(profile_id,await json_object(request))
        if profile_id==engine.active_profile_id:
            engine.invalidate_groups(reason="profile_settings_changed")
        return result

    @app.post("/admin/profiles/{profile_id}/activate", dependencies=[Depends(admin)])
    async def profiles_activate(profile_id: str, request: Request):
        if await json_object(request):
            raise HTTPException(400,"profile_activation_requires_empty_object")
        return engine.activate(profile_id)

    @app.delete("/admin/profiles/{profile_id}", dependencies=[Depends(admin)])
    def profiles_delete(profile_id: str):
        return engine.delete_profile(profile_id)

    @app.get("/admin/diagnostics", dependencies=[Depends(admin)])
    def diagnostics():
        result=engine.diagnostics.status()
        result["retry_after_seconds"]=max(0,round(engine._cloud_retry_after-time.monotonic()))
        return result

    @app.get("/admin/desktop/status", dependencies=[Depends(admin)])
    def desktop_status():
        return DesktopService(root).status()

    @app.put("/admin/desktop/autostart", dependencies=[Depends(admin)])
    async def desktop_autostart(request: Request):
        return DesktopService(root).set_autostart(await json_object(request))

    @app.get("/admin/subscriptions", dependencies=[Depends(admin)])
    def subscriptions_get():
        status=subscriptions.status()
        return {"subscriptions":subscriptions.list(),"status":status,"deliveries":status["deliveries"]}

    @app.post("/admin/subscriptions", dependencies=[Depends(admin)])
    async def subscription_add(request: Request):
        return await subscriptions.add(await json_object(request))

    @app.post("/admin/subscriptions/batch", dependencies=[Depends(admin)])
    async def subscription_add_many(request: Request):
        return await subscriptions.add_many(await json_object(request))

    @app.post("/admin/subscriptions/preview", dependencies=[Depends(admin)])
    async def subscription_preview(request: Request):
        result = await subscriptions.preview(await json_object(request))
        image_file = result.pop("image_file", "")
        result["media_url"] = ""
        if image_file:
            path = Path(image_file)
            media_root = (root / "data/media").resolve()
            if (path.is_absolute() and path.is_file() and not path.is_symlink()
                and path.resolve().is_relative_to(media_root) and re.fullmatch(r"[a-f0-9]{32}\.png",path.name)):
                result["media_url"] = "/admin/media/" + path.name
        return result

    @app.get("/admin/subscription-credentials", dependencies=[Depends(admin)])
    def subscription_credentials_get():
        from .subscription_credentials import credential_status
        return credential_status(root)

    @app.put("/admin/subscription-credentials", dependencies=[Depends(admin)])
    async def subscription_credentials_put(request: Request):
        from .subscription_credentials import update_credentials
        from starlette.concurrency import run_in_threadpool
        return await run_in_threadpool(update_credentials, root, await json_object(request))

    @app.patch("/admin/subscriptions/{number}", dependencies=[Depends(admin)])
    async def subscription_update(number: str, request: Request):
        return await subscriptions.update(number,await json_object(request))

    @app.delete("/admin/subscriptions/{number}", dependencies=[Depends(admin)])
    def subscription_delete(number: str):
        return subscriptions.delete(number)

    @app.get("/admin/providers", dependencies=[Depends(admin)])
    def providers_get():
        return provider_config.describe(root,profile_store=engine.profiles,profile_id=engine.profile_id)

    @app.put("/admin/providers", dependencies=[Depends(admin)])
    async def providers_put(request: Request):
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(provider_config.save,root,await json_object(request),
                                        profile_store=engine.profiles,profile_id=engine.profile_id)
        engine.reset_provider_backoff()
        engine.invalidate_groups(reason="provider_settings_changed")
        return result

    @app.get('/admin/role-activity',dependencies=[Depends(admin)])
    def role_activity_get():
        from . import role_activity
        observed=role_activity.observations(subscriptions)
        from .subscription_library import saved_sources
        return role_activity.describe(engine.content_root,observed) | {'library':saved_sources(root)['sources']}

    @app.put('/admin/role-activity',dependencies=[Depends(admin)])
    async def role_activity_put(request: Request):
        from . import role_activity
        observed=role_activity.observations(subscriptions)
        role_activity.save(engine.content_root,await json_object(request),observed)
        engine.invalidate_groups(reason="role_activity_changed")
        observed=role_activity.observations(subscriptions)
        await subscriptions.sync_activity(role_activity.config(engine.content_root)['sources'])
        from .subscription_library import saved_sources
        return role_activity.describe(engine.content_root,observed) | {'library':saved_sources(root)['sources']}

    @app.get("/admin/knowledge", dependencies=[Depends(admin)])
    def knowledge_list(limit: int = 100, offset: int = 0, collection: str | None = None, sort: str = 'added_desc'):
        return lore.list_documents(engine.content_root,limit=limit,offset=offset,collection=collection,sort=sort)

    @app.post("/admin/knowledge/import", dependencies=[Depends(admin)])
    async def knowledge_import(request: Request):
        from starlette.concurrency import run_in_threadpool
        body = await json_object(request)
        if set(body) != {"documents"}:
            raise HTTPException(400,"documents_required")
        result = await run_in_threadpool(lore.import_documents,engine.content_root,body["documents"])
        engine.invalidate_groups(reason="knowledge_changed")
        return result

    @app.get("/admin/knowledge/search", dependencies=[Depends(admin)])
    def knowledge_search(q: str, collection: str | None = None):
        if not 1 <= len(q) <= 2000:
            raise HTTPException(400,"query_length_invalid")
        return {"results":lore.search(engine.content_root,q,limit=5,collection=collection)}

    @app.post("/admin/knowledge/rebuild", dependencies=[Depends(admin)])
    def knowledge_rebuild():
        return lore.rebuild_index(engine.content_root)

    @app.post("/admin/knowledge/source/preview", dependencies=[Depends(admin)])
    async def knowledge_source_preview(request: Request):
        from .knowledge_sources import fetch_moegirl
        body=await json_object(request)
        if set(body)!={"url"} or not isinstance(body["url"],str):
            raise HTTPException(400,"source_url_required")
        try:
            return await fetch_moegirl(body["url"])
        except ValueError as exc:
            raise HTTPException(400,str(exc)) from None

    @app.post('/admin/knowledge/move',dependencies=[Depends(admin)])
    async def knowledge_move(request: Request):
        body=await json_object(request)
        if set(body)!={'ids','collection'}:raise HTTPException(400,'knowledge_move_invalid')
        return lore.move_documents(engine.content_root,body['ids'],body['collection'])

    @app.get("/admin/knowledge/{document_id}", dependencies=[Depends(admin)])
    def knowledge_get(document_id: str):
        return lore.get_document(engine.content_root,document_id)

    @app.patch("/admin/knowledge/{document_id}", dependencies=[Depends(admin)])
    async def knowledge_update(document_id: str, request: Request):
        from starlette.concurrency import run_in_threadpool
        result = await run_in_threadpool(lore.update_document,engine.content_root,document_id,await json_object(request))
        engine.trajectories.erase_previews(knowledge_id=document_id)
        engine.invalidate_groups(reason="knowledge_changed")
        return result

    @app.delete("/admin/knowledge/{document_id}", dependencies=[Depends(admin)])
    def knowledge_delete(document_id: str):
        result = lore.delete_document(engine.content_root,document_id)
        engine.trajectories.erase_previews(knowledge_id=document_id)
        engine.invalidate_groups(reason="knowledge_changed")
        return result

    @app.get('/admin/qq-status', dependencies=[Depends(admin)])
    async def qq_status():
        return await qq_connection.check()

    @app.post('/bridge/connection-status', dependencies=[Depends(bridge)])
    async def qq_status_report(request: Request):
        body = await json_object(request)
        if set(body) != {'self_id','connected','online'}:
            raise HTTPException(400,'qq_status_invalid')
        qq_connection.report(body['self_id'],body['connected'],body['online'])
        return {'recorded':True}

    @app.get("/admin/napcat/status", dependencies=[Depends(admin)])
    async def napcat_status():
        try:
            return await qq_connection.admin.status()
        except NapCatError as exc:
            return {"available":False,"logged_in":False,"reason":str(exc)}

    @app.get("/admin/napcat/qr", dependencies=[Depends(admin)])
    async def napcat_qr():
        try:
            return Response(await qq_connection.admin.qr_svg(),media_type="image/svg+xml")
        except NapCatError as exc:
            raise HTTPException(503,str(exc)) from None

    @app.post("/admin/napcat/qr/refresh", dependencies=[Depends(admin)])
    async def napcat_qr_refresh():
        try:
            return Response(await qq_connection.admin.refresh_qr_svg(),media_type="image/svg+xml")
        except NapCatError as exc:
            status = 409 if str(exc) in {"napcat_already_logged_in", "napcat_restarting", "napcat_login_in_progress"} else 503
            raise HTTPException(status,str(exc)) from None

    @app.post("/admin/napcat/bind", dependencies=[Depends(admin)])
    async def napcat_bind():
        try:
            return await qq_connection.admin.bind_logged_account()
        except NapCatError as exc:
            raise HTTPException(400,str(exc)) from None

    @app.put("/admin/settings", dependencies=[Depends(admin)])
    def settings_put(settings: Settings):
        if settings.qq_enabled and settings.onebot_enabled:
            raise HTTPException(400, "only_one_transport_can_be_enabled")
        if (settings.qq_enabled and settings.transport != "official") or (settings.onebot_enabled and settings.transport != "onebot"):
            raise HTTPException(400, "enabled_transport_must_match_selection")
        if settings.qq_enabled and (not settings.app_id or not settings.owners or not (settings.allowed_groups or settings.allowed_dm_users)):
            raise HTTPException(400, "configure_app_owner_whitelist_first")
        if settings.qq_enabled and not any(
            (owner.kind == "group" and owner.scope_id in settings.allowed_groups)
            or (owner.kind == "dm" and owner.scope_id == owner.user_id and owner.user_id in settings.allowed_dm_users)
            for owner in settings.owners
        ):
            raise HTTPException(400, "owner_must_be_in_allowed_scope")
        if settings.onebot_enabled and (
            not settings.onebot_self_id
            or not (settings.onebot_allowed_groups or settings.onebot_allowed_dm_users)
        ):
            raise HTTPException(400, "configure_onebot_self_whitelist_first")
        # A local GUI administrator needs no second QQ account. Chat-based
        # delegation is optional, but each supplied binding must be usable.
        if settings.onebot_owners and not all(
            owner.user_id != settings.onebot_self_id and (
                (owner.kind == "group" and owner.scope_id in settings.onebot_allowed_groups)
                or (owner.kind == "dm" and owner.user_id in settings.onebot_allowed_dm_users)
            ) for owner in settings.onebot_owners
        ):
            raise HTTPException(400, "onebot_owner_must_be_other_user_in_allowed_scope")
        if settings.voice_enabled and not settings.voice_authorized:
            raise HTTPException(400, "voice_authorization_required")
        with AccountRegistry(root).transaction(), settings_lock:
            if settings.transport == "onebot" and settings.onebot_self_id:
                assert_account_available(root, settings.onebot_self_id)
            previous_voice=engine.profiles.voice(engine.profile_id)
            voice_patch={k:getattr(settings,k) for k in VOICE_FIELDS if getattr(settings,k)!=previous_voice[k]}
            voice_changed=False
            try:
                if voice_patch:
                    engine.profiles.update(engine.profile_id,{"voice":voice_patch})
                    voice_changed=True
                if engine.profile_id != "default":
                    global_settings=read_settings(root)
                    global_body=settings.model_dump()
                    global_body.update({k:getattr(global_settings,k) for k in VOICE_STORED})
                    write_settings(root,Settings.model_validate(global_body))
                else:
                    write_settings(root, settings)
            except Exception:
                if voice_changed:
                    engine.profiles.restore_voice(engine.profile_id,previous_voice)
                raise
        # Any settings change invalidates in-flight passive decisions and clears
        # old scopes after whitelist/identity/participation changes.
        engine.invalidate_groups()
        return {"saved":True}

    @app.get("/admin/groups", dependencies=[Depends(admin)])
    def groups(scope: str):
        current = engine.current
        settings = current.settings(scope)
        limits = current._group_budget(settings)
        budget = current.groups.budget_status(scope, **limits)
        return {"scope": scope, "members": current.groups.familiar_members(scope),
                "participation_enabled": settings.group_chat_enabled,
                "budget": {**budget, "hour_limit": limits["hour_limit"], "day_limit": limits["day_limit"]},
                "heat": current.groups.heat(scope, settings),
                "stats": current.groups.get_stats()}

    @app.post("/admin/groups/clear", dependencies=[Depends(admin)])
    async def groups_clear(request: Request):
        body=await json_object(request)
        scope=body.get("scope")
        if not isinstance(scope,str) or not scope or len(scope)>512:
            raise HTTPException(400,"invalid_group_scope")
        engine.clear_group_context(scope)
        return {"cleared":True}

    @app.get("/admin/persona", dependencies=[Depends(admin)])
    def persona_get():
        return {"text":(engine.content_root / "content/persona.md").read_text()}

    @app.put("/admin/persona", dependencies=[Depends(admin)])
    async def persona_put(request: Request):
        body = await json_object(request)
        text = body.get("text")
        if not isinstance(text, str) or not 10 <= len(text) <= 60000:
            raise HTTPException(400, "persona_length_10_to_60000")
        private_write(engine.content_root / "content/persona.md", text)
        engine.invalidate_groups(reason="persona_changed")
        return {"saved":True}

    @app.post("/admin/simulate", dependencies=[Depends(admin)])
    async def simulate(request: Request):
        body = await json_object(request)
        settings = read_settings(root)
        body.setdefault("transport", settings.transport)
        body.setdefault("app_id", (settings.onebot_self_id or "1") if body["transport"] == "onebot" else "demo")
        body.setdefault("event_id", uuid.uuid4().hex)
        event = Incoming.model_validate(body)
        return await engine.handle(event, demo=True)

    @app.get("/admin/memories", dependencies=[Depends(admin)])
    def memories(scope: str, user: str, subject: str | None = None):
        return {"memories":engine.memory.list_memories(scope,user,is_owner=True,subject=subject)}

    @app.post("/admin/memories", dependencies=[Depends(admin)])
    def memory_add(item: MemoryInput):
        number = engine.memory.add_memory(item.scope,item.user,item.text,kind=item.kind,subject=item.subject,is_owner=True)
        return {"id":number}

    @app.patch("/admin/memories/{number}", dependencies=[Depends(admin)])
    def memory_update(number: int, item: MemoryInput):
        engine.memory.update_memory(number,item.scope,item.user,item.text,is_owner=True)
        engine.clear_group_context(item.scope, reason="memory_changed")
        return {"saved":True}

    @app.delete("/admin/memories/{number}", dependencies=[Depends(admin)])
    def memory_delete(number: int, scope: str, user: str):
        engine.memory.delete_memory(number,scope,user,is_owner=True)
        engine.clear_group_context(scope, reason="memory_changed")
        return {"deleted":True}

    @app.get("/admin/history", dependencies=[Depends(admin)])
    def history(scope: str, user: str):
        return {"history":engine.memory.list_history(scope,user,is_owner=True)}

    @app.patch("/admin/history/{number}", dependencies=[Depends(admin)])
    def history_update(number: int, item: MemoryInput):
        engine.memory.update_history(number,item.scope,item.user,item.text,is_owner=True)
        engine.clear_group_context(item.scope, reason="memory_changed")
        return {"saved":True}

    @app.delete("/admin/history/{number}", dependencies=[Depends(admin)])
    def history_delete(number: int, scope: str, user: str):
        engine.memory.delete_history(number,scope,user,is_owner=True)
        engine.clear_group_context(scope, reason="memory_changed")
        return {"deleted":True}

    @app.post("/admin/forget", dependencies=[Depends(admin)])
    async def forget(item: MemoryInput):
        await engine.forget_person_when_idle(item.scope,item.user)
        return {"deleted":True, "backup_note":"旧备份仍需单独删除；恢复备份可能恢复已删除内容。"}

    @app.post("/admin/backup", dependencies=[Depends(admin)])
    def backup():
        path = backup_project(root,engine)
        return {"file":str(path)}

    @app.get("/admin/deliveries", dependencies=[Depends(admin)])
    def deliveries():
        return {"deliveries":engine.ledger.deliveries()}

    @app.get("/admin/media/{name}", dependencies=[Depends(admin)])
    def media(name: str):
        if not re.fullmatch(r"[a-f0-9]{32}\.(wav|png|jpg|jpeg|gif)",name):
            raise HTTPException(404)
        path = root / "data/media" / name
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404)
        return FileResponse(path)

    @app.post("/bridge/message", dependencies=[Depends(bridge)])
    async def bridge_message(event: Incoming):
        return await engine.handle(event)

    @app.post("/bridge/reply/authorize", dependencies=[Depends(bridge)])
    async def reply_authorize(request: Request):
        body=await json_object(request)
        reply_id=body.get("reply_id")
        if set(body)!={"reply_id"} or not isinstance(reply_id,str) or not re.fullmatch(r"[a-f0-9]{32}",reply_id):
            raise HTTPException(400,"invalid_reply_id")
        claim_token = engine.claim_reply(reply_id, token_required=request.headers.get("x-xzbot-claim-receipts") == "1")
        result = {"allowed": claim_token is not None}
        if claim_token:
            result["claim_token"] = claim_token
        return result

    @app.post("/bridge/reply/segment-authorize", dependencies=[Depends(bridge)])
    async def segment_authorize(request: Request):
        from . import segmented_delivery
        body = await json_object(request)
        if set(body) != {"reply_id", "segment_index"} or not re.fullmatch(r"[a-f0-9]{32}", str(body.get("reply_id", ""))):
            raise HTTPException(400, "invalid_segment_request")
        return {"allowed":segmented_delivery.authorize(engine, body["reply_id"], body["segment_index"])}

    @app.post("/bridge/reply/segment-ack", dependencies=[Depends(bridge)])
    async def segment_ack(request: Request):
        from . import segmented_delivery
        body = await json_object(request)
        if set(body)-{"reply_id", "segment_index", "status", "message_id"} or not re.fullmatch(r"[a-f0-9]{32}", str(body.get("reply_id", ""))):
            raise HTTPException(400, "invalid_segment_request")
        try:
            return segmented_delivery.acknowledge(engine, body["reply_id"], body.get("segment_index"), body.get("status"), body.get("message_id"))
        except ValueError:
            raise HTTPException(400, "invalid_segment_ack") from None

    @app.post("/bridge/reply/segment-finish", dependencies=[Depends(bridge)])
    async def segment_finish(request: Request):
        from . import segmented_delivery
        body = await json_object(request)
        if set(body) != {"reply_id"} or not re.fullmatch(r"[a-f0-9]{32}", str(body.get("reply_id", ""))):
            raise HTTPException(400, "invalid_segment_request")
        segmented_delivery.settle(engine.current, body["reply_id"], abort=True)
        return {"ok":True}

    @app.post("/bridge/notifications/claim", dependencies=[Depends(bridge)])
    async def notification_claim(request: Request):
        body=await json_object(request)
        return {"notifications":subscriptions.fetch_claim(body.get("self_id"),limit=1)}

    @app.get('/admin/runtime/choice', dependencies=[Depends(admin)])
    def qq_runtime_choice():
        from .qq_runtime import describe
        return describe(root)

    @app.put('/admin/runtime/choice', dependencies=[Depends(admin)])
    async def qq_runtime_select(request: Request):
        from .qq_runtime import read, save, validate, describe, local_listener_open
        body=await json_object(request)
        try:validate(body)
        except ValueError as error:raise HTTPException(400,str(error)) from None
        if body==read(root):return describe(root)
        if body['environment']!='external' and not describe(root)['prepared'].get(body['environment']+'_'+body['adapter']):
            raise HTTPException(409,'qq_runtime_not_prepared')
        try:await power.stop()
        except BotPowerError as error:raise HTTPException(503,str(error)) from None
        async with power.lock:
            if power.status()['enabled'] or power.status()['busy']:
                raise HTTPException(409,'qq_runtime_stop_required')
            # Also close a login-only runtime opened while the bot was paused.
            try:await runtime.stop()
            except BotPowerError as error:raise HTTPException(503,str(error)) from None
            for port in (layout.napcat_port,layout.core_port+6):
                if local_listener_open(port):
                    raise HTTPException(409,'qq_runtime_previous_still_running')
            try:save(root,body)
            except ValueError as error:raise HTTPException(400,str(error)) from None
            async with qq_connection.lock:qq_connection.runtime_changed()
        return describe(root)

    @app.post('/admin/runtime/open-login', dependencies=[Depends(admin)])
    async def qq_runtime_open_login(request: Request):
        from .external_runtime import enabled
        if await json_object(request)!={}:raise HTTPException(400,'qq_runtime_invalid')
        if enabled(root):raise HTTPException(409,'qq_runtime_external_launch_required')
        async with power.lock:
            if power.status()['busy']:raise HTTPException(409,'qq_runtime_stop_required')
            # Mark the login-only runtime as needing attention so Close always stops it.
            if not power.status()['enabled']:power._write('error','power_qq_login_required')
            try:await runtime._run('start')
            except BotPowerError as error:raise HTTPException(503,str(error)) from None
        return {'ok':True,'reply_enabled':power.status()['enabled']}

    @app.get('/admin/runtime/config', dependencies=[Depends(admin)])
    def qq_runtime_config():
        from .external_runtime import connection_archive
        from .qq_runtime import read
        name=read(root)['adapter']
        return Response(connection_archive(root),media_type='application/zip',headers={
            'Content-Disposition':f'attachment; filename="xzbot-{name}-private-config.zip"','Cache-Control':'no-store'})

    @app.post('/admin/runtime/snowluma-access', dependencies=[Depends(admin)])
    async def qq_runtime_local_access(request: Request):
        from .qq_runtime import read
        from .host import private_path
        body=await json_object(request)
        if set(body)!={'kind'} or body['kind'] not in {'webui','desktop'}:
            raise HTTPException(400,'qq_runtime_invalid')
        if read(root)!= {'schema':1,'environment':'lima','adapter':'snowluma'}:
            raise HTTPException(409,'snowluma_local_access_unavailable')
        path=root/'data/snowluma-access.json'
        try:
            if not private_path(path) or path.stat().st_size>2048:raise ValueError()
            value=json.loads(path.read_text())[body['kind']]
            if not isinstance(value,str) or not 8<=len(value)<=128:raise ValueError()
        except (OSError,ValueError,KeyError):raise HTTPException(409,'snowluma_local_access_unavailable') from None
        return JSONResponse({'value':value},headers={'Cache-Control':'no-store'})

    @app.get("/admin/runtime/external", dependencies=[Depends(admin)])
    def external_runtime_status():
        from .external_runtime import enabled
        from .qq_runtime import read
        return {"external":enabled(root),"onebot_port":layout.onebot_port,"napcat_port":layout.napcat_port,**read(root)}

    @app.get("/admin/runtime/external/config", dependencies=[Depends(admin)])
    def external_runtime_config():
        from .external_runtime import enabled, connection_archive
        if not enabled(root):raise HTTPException(409,"runtime_not_external")
        return Response(connection_archive(root),media_type='application/zip',headers={
            'Content-Disposition':'attachment; filename="xzbot-napcat-private-config.zip"','Cache-Control':'no-store'})

    @app.get("/admin/groups/idle/status", dependencies=[Depends(admin)])
    def idle_status():
        return engine.idle.status()

    @app.post("/admin/groups/idle/trigger", dependencies=[Depends(admin)])
    async def trigger_idle(request: Request):
        body = await json_object(request)
        if (set(body) != {"scope", "request_id"} or not isinstance(body.get("scope"), str)
                or not re.fullmatch(r"[a-f0-9]{32}", str(body.get("request_id", "")))):
            raise HTTPException(400, "invalid_idle_request")
        # Unconfirmed prior sends are a warning, not proof of logout. A fresh
        # explicit admin action may try once; this never replays the old draft.
        if qq_connection.snapshot()["state"] not in {"online","degraded"}:
            raise HTTPException(409, "manual_idle_offline")
        try:
            return engine.idle.queue_manual(body["scope"], body["request_id"])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/admin/groups/idle/requests/{number}", dependencies=[Depends(admin)])
    def manual_idle_status(number: str):
        if not re.fullmatch(r"[a-f0-9]{32}", number):
            raise HTTPException(400, "invalid_idle_request")
        row = engine.idle.manual_status(number)
        if row is None:
            raise HTTPException(404, "manual_idle_not_found")
        return row

    @app.post("/admin/groups/voice/send", dependencies=[Depends(admin)])
    async def send_group_voice(request: Request):
        body = await json_object(request)
        if (set(body) != {"scope", "request_id", "text"}
                or not isinstance(body["scope"], str) or len(body["scope"]) > 512
                or not isinstance(body["request_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", body["request_id"])
                or not isinstance(body["text"], str) or not body["text"].strip()):
            raise HTTPException(400, "invalid_voice_request")
        if qq_connection.snapshot()["state"] not in {"online", "degraded"}:
            raise HTTPException(409, "manual_voice_offline")
        try:
            return engine.idle.queue_manual(body["scope"], body["request_id"], voice_text=body["text"])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    @app.get("/admin/groups/voice/requests/{number}", dependencies=[Depends(admin)])
    async def group_voice_status(number: str):
        if not re.fullmatch(r"[a-f0-9]{32}", number):
            raise HTTPException(400, "invalid_voice_request")
        row = engine.idle.manual_status(number, voice=True)
        if row is None:
            raise HTTPException(404, "manual_voice_not_found")
        return row

    @app.post("/bridge/idle/manual-claim", dependencies=[Depends(bridge)])
    async def manual_idle_claim(request: Request):
        body = await json_object(request)
        if set(body) != {"self_id"} or not isinstance(body.get("self_id"), str):
            raise HTTPException(400, "invalid_idle_request")
        with engine.pin() as current:
            return {"messages": await current.idle.claim_manual(body["self_id"])}

    @app.post("/bridge/idle/claim", dependencies=[Depends(bridge)])
    async def idle_claim(request: Request):
        body=await json_object(request)
        if set(body)!={"self_id"} or not isinstance(body.get("self_id"),str):
            raise HTTPException(400,"invalid_idle_request")
        with engine.pin() as current:
            return {"messages":await current.idle.claim(body["self_id"])}

    @app.post("/bridge/idle/authorize", dependencies=[Depends(bridge)])
    async def idle_authorize(request: Request):
        body=await json_object(request)
        if set(body)!={"id","claim_token","self_id"}:
            raise HTTPException(400,"invalid_idle_request")
        return {"allowed":engine.idle.authorize(body.get("id"),body.get("claim_token"),body.get("self_id"))}

    @app.post("/bridge/idle/ack", dependencies=[Depends(bridge)])
    async def idle_ack(request: Request):
        body=await json_object(request)
        if set(body)-{"id","claim_token","self_id","status","message_id","error_code"}:
            raise HTTPException(400,"invalid_idle_request")
        return engine.idle.ack(body.get("id"),body.get("claim_token"),body.get("self_id"),body.get("status"),body.get("message_id"),body.get("error_code"))

    @app.post("/bridge/notifications/authorize", dependencies=[Depends(bridge)])
    async def notification_authorize(request: Request):
        body=await json_object(request)
        return {"allowed":subscriptions.authorize_claim(body.get("id"),body.get("claim_token"),body.get("self_id"))}

    @app.post("/bridge/notifications/ack", dependencies=[Depends(bridge)])
    async def notification_ack(request: Request):
        body=await json_object(request)
        return subscriptions.ack(body.get("id"),body.get("claim_token"),body.get("status"))

    @app.post("/bridge/ack", dependencies=[Depends(bridge)])
    async def ack(request: Request):
        body = await json_object(request)
        if body.get("status") not in {"sent", "uncertain", "cancelled"} or not re.fullmatch(r"[a-f0-9]{32}", str(body.get("reply_id", ""))):
            raise HTTPException(400, "invalid_ack")
        message_id=body.get("message_id")
        if message_id is not None and (not isinstance(message_id,str) or not re.fullmatch(r"-?[0-9]{1,20}",message_id)):
            raise HTTPException(400,"invalid_message_id")
        claim_token = body.get("claim_token", "")
        if claim_token != "" and (not isinstance(claim_token, str) or not re.fullmatch(r"[a-f0-9]{32}", claim_token)):
            raise HTTPException(400, "invalid_claim_token")
        from .segmented_delivery import metadata
        if metadata(engine.ledger, body["reply_id"]):
            raise HTTPException(409, "segment_receipts_required")
        from .delivery_errors import safe_delivery_code
        error_code = safe_delivery_code(body["error_code"]) if body.get("error_code") else ""
        engine.acknowledge(body["reply_id"], body["status"], message_id, claim_token=claim_token, error_code=error_code)
        return {"ok":True}

    return app


def backup_project(root: Path, engine: Engine) -> Path:
    from contextlib import closing
    import shutil
    path = root / "data/backups" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
    path.mkdir(parents=True,mode=0o700)
    def snapshot(source_root, destination):
        destination.mkdir(parents=True,exist_ok=True,mode=0o700)
        for name in ("memory.sqlite3","group_state.sqlite3"):
            source_path=source_root / "data" / name
            if source_path.is_file() and not source_path.is_symlink():
                with closing(sqlite3.connect(source_path.as_uri()+"?mode=ro",uri=True)) as source, closing(sqlite3.connect(destination / name)) as target:
                    source.backup(target)
        with lore.backup_guard(source_root):
            if (source_root / "content").exists():
                shutil.copytree(source_root / "content",destination / "content")
    if isinstance(engine,ProfileEngineManager):
        with engine.profiles.lock:
            profiles=engine.profiles.list()
            snapshot(root,path)
            for profile in profiles["profiles"]:
                if profile["id"] != "default":
                    snapshot(engine.profiles.directory(profile["id"]),path / "profiles" / profile["id"])
            shutil.copyfile(engine.profiles.path,path / "profiles.json")
    else:
        engine.memory.backup(path / "memory.sqlite3")
        engine.groups.backup(path / "group_state.sqlite3")
        with lore.backup_guard(root):
            shutil.copytree(root / "content",path / "content")
    if (root / "data/bilibili.sqlite3").exists():
        BilibiliSubscriptions(root).backup(path / "bilibili.sqlite3")
    with engine.ledger.db() as source, closing(sqlite3.connect(path / "state.sqlite3")) as dest:
        source.backup(dest)
    if (root / "config/groups.json").exists():
        shutil.copyfile(root / "config/groups.json",path / "group-settings.json")
    from .group_presets import path as group_presets_path
    if group_presets_path(root).exists():
        shutil.copyfile(group_presets_path(root),path/'group-presets.json')
    from .memory_presets import path as memory_presets_path
    if memory_presets_path(root).exists():
        shutil.copyfile(memory_presets_path(root),path/'memory-presets.json')
    if (root / "config/local.json").exists():
        shutil.copyfile(root / "config/local.json",path / "settings.json")
    identity = root / "data/keychain-service"
    if identity.is_file() and not identity.is_symlink():
        shutil.copyfile(identity, path / "keychain-service")
    control = root / "data/bot-control.json"
    if control.is_file() and not control.is_symlink():
        shutil.copyfile(control, path / "bot-control.json")
    # Save routing metadata, but never recursively copy another account's data
    # or a QQ login. Each account keeps its own consistent backup above.
    registry = AccountRegistry(root)
    with registry.transaction():
        if registry.layout.is_managed:
            shutil.copyfile(root / "config/instance.json", path / "instance.json")
        elif registry.path.exists():
            rows = registry.read()
            private_write(path / "accounts.json", json.dumps({"schema": 1, "accounts": rows}, ensure_ascii=False))
    private_write(path / "manifest.json",json.dumps({"schema":2,"version":__version__,"created_at":time.time(),"contains_credentials":False,
                                                 "profile_layout":"default_at_backup_root_others_in_profiles",
                                                 "account_scope":"this_account_only"}))
    for p in path.rglob("*"):
        p.chmod(0o700 if p.is_dir() else 0o600)
    return path
