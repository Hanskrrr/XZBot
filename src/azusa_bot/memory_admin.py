"""Authenticated local administration of the active profile's memory pipeline."""
from __future__ import annotations
import time

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool


class MemberInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(min_length=1, max_length=512)
    user: str = Field(min_length=1, max_length=160)


class ConsentInput(MemberInput):
    enabled: StrictBool


class SummaryInput(MemberInput):
    revisit: StrictBool = False


class BackgroundInput(MemberInput):
    text: str = Field(min_length=1,max_length=700)
    version: int = Field(default=0,ge=0,strict=True)
    profile_id: str | None = Field(default=None,pattern=r'^(default|[a-f0-9]{32})$')


class GroupCollectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(min_length=1, max_length=512)
    enabled: StrictBool


def create_router(engine) -> APIRouter:
    """Caller must mount with its local-admin authentication dependency."""
    router = APIRouter(prefix="/admin/memory")

    def member_view(current,scope,user):
        from .member_notes import view
        from .bot_control import read_control
        result=view(current.memory,scope,user)
        settings=current.settings(scope)
        policy=current.memory.summary_policy(scope)
        threshold=policy.get('auto_summary_every',settings.auto_summary_every)
        wait=policy.get('auto_summary_max_wait_minutes',settings.auto_summary_max_wait_minutes)
        prefix,identity,groups,friends=(('onebot',settings.onebot_self_id,settings.onebot_allowed_groups,settings.onebot_allowed_dm_users)
            if settings.transport=='onebot' else ('qq',settings.app_id,settings.allowed_groups,settings.allowed_dm_users))
        scope_allowed=(scope in {f'{prefix}:{identity}:group:{group}' for group in groups}
                       or scope==f'{prefix}:{identity}:dm:{user}' and user in friends)
        with current.memory._lock:
            state=current.memory._db.execute('SELECT * FROM summary_progress WHERE scope=? AND user_id=?',(scope,user)).fetchone()
            state=dict(state) if state else {}
            pending,oldest=current.memory._db.execute("SELECT COUNT(*),MIN(created_at) FROM history WHERE scope=? AND user_id=? AND role='user' AND id>?",
                (scope,user,state.get('through_id',0))).fetchone()
        result['schedule']={'poll_seconds':60,'threshold':threshold,'max_wait_minutes':wait,
            'enabled':current.memory.consent(scope,user) and not current.groups.opted_out(scope,user),
            'bot_running':read_control(current.root)['enabled'] and (settings.onebot_enabled if settings.transport=='onebot' else settings.qq_enabled),
            'scope_allowed':scope_allowed,'pending':pending,
            'ready':pending>=threshold or bool(oldest and time.time()-oldest>=wait*60),
            'last_attempt_at':state.get('last_attempt_at'),'last_success_at':state.get('last_success_at'),
            'last_status':state.get('last_status','not_started'),'retry_at':state.get('retry_at',0)}
        return dict(result,profile_id=current.profile_id)

    @router.get('/member-context')
    def member_context(scope: str = Query(min_length=1,max_length=512), user: str = Query(min_length=1,max_length=160)):
        current=engine.current
        return member_view(current,scope,user)

    @router.put('/member-background')
    def member_background(item: BackgroundInput):
        from .member_notes import edit_background,view
        current=engine.current
        if item.profile_id is not None and item.profile_id!=current.profile_id:
            raise HTTPException(409,'member_profile_changed')
        try:
            edit_background(current.memory,item.scope,item.user,item.text,version=item.version)
        except ValueError as exc:
            raise HTTPException(409 if str(exc)=='member_background_changed' else 400,str(exc)) from None
        current.clear_group_context(item.scope,reason='memory_changed')
        return member_view(current,item.scope,item.user)

    @router.get("/status")
    def status(scope: str | None = Query(default=None,min_length=1,max_length=512)):
        current = engine.current
        settings=current.settings()
        policy=current.memory.summary_policy(scope) if scope else {}
        result = current.memory.collection_status(policy.get('auto_summary_every',settings.auto_summary_every),policy.get('auto_summary_max_wait_minutes',settings.auto_summary_max_wait_minutes),scope_filter=scope)
        if scope:result['scope']=scope
        result["profile_id"] = current.profile_id
        result["background_interval_seconds"] = 60
        return result

    @router.get("/browse")
    def browse(scope: str = Query(min_length=1,max_length=512), kind: str = "history",
               user: str | None = None, before: int | None = Query(default=None,ge=1),
               limit: int = Query(default=100,ge=1,le=200)):
        if kind not in {"history","memories"}: raise HTTPException(400,"invalid_record_kind")
        return engine.current.memory.browse_scope(scope,kind=kind,user=user,before=before,limit=limit)

    @router.get("/targets")
    def targets():
        current = engine.current
        settings = current.settings()
        onebot = settings.transport == "onebot"
        platform = "onebot" if onebot else "qq"
        identity = settings.onebot_self_id if onebot else settings.app_id
        groups = settings.onebot_allowed_groups if onebot else settings.allowed_groups
        users = settings.onebot_allowed_dm_users if onebot else settings.allowed_dm_users
        scopes = []
        if identity:
            for kind, values in (("dm", users), ("group", groups)):
                for target in values:
                    scope = f"{platform}:{identity}:{kind}:{target}"
                    observed = current.groups.familiar_members(scope, limit=128) if kind == "group" else []
                    candidates = [{"user_id": target, "display_name": "", "observed": False}] if kind == "dm" else [dict(row, observed=True) for row in observed]
                    if kind == "group":
                        seen = {row["user_id"] for row in candidates}
                        for user in current.memory.known_members(scope):
                            if user not in seen:
                                candidates.append({"user_id": user, "display_name": "", "observed": False})
                                seen.add(user)
                    members = [{"user": row["user_id"], "display_name": row.get("display_name", ""),
                                "owner": settings.is_owner(kind, target, row["user_id"], settings.transport),
                                "enabled": current.memory.consent(scope, row["user_id"]),
                                "collection_source": current.memory.collection_source(scope, row["user_id"]),
                                "observed": row["observed"]} for row in candidates]
                    scopes.append({"scope": scope, "kind": kind, "scope_id": target,
                                   "label": ("私聊 " if kind == "dm" else "群聊 ") + target,
                                   "group_collection_enabled": kind == "group" and current.memory.group_collection_enabled(scope),
                                   "stats":current.memory.scope_stats(scope), "members": members})
        return {"profile_id": current.profile_id, "transport": settings.transport,
                "transport_enabled": settings.onebot_enabled if onebot else settings.qq_enabled,
                "scopes": scopes}

    @router.put("/group")
    def group_collection(item: GroupCollectionInput):
        current = engine.current
        settings = current.settings()
        onebot = settings.transport == "onebot"
        platform = "onebot" if onebot else "qq"
        identity = settings.onebot_self_id if onebot else settings.app_id
        targets = settings.onebot_allowed_groups if onebot else settings.allowed_groups
        allowed = {f"{platform}:{identity}:group:{target}" for target in targets} if identity else set()
        if item.scope not in allowed:
            raise HTTPException(400, "group_collection_requires_allowed_group")
        current.memory.set_group_collection(item.scope, item.enabled)
        current.clear_group_context(item.scope)
        return {"saved": True, "scope": item.scope, "enabled": item.enabled,
                "profile_id": current.profile_id,
                "note": "群默认采集已开启；新老成员从后续发言起纳入，单独关闭的成员继续排除。" if item.enabled else
                        "群默认采集已暂停；已有历史和记忆保留，单独开启的成员继续记录。"}

    @router.put("/consent")
    def consent(item: ConsentInput):
        current = engine.current
        current.memory.set_consent(item.scope, item.user, item.enabled)
        if not item.enabled:
            current.trajectories.erase_image_sources(item.scope,item.user)
            current.clear_group_context(item.scope)
        return {"saved": True, "enabled": item.enabled, "profile_id": current.profile_id}

    @router.post("/summarize")
    async def summarize(item: SummaryInput):
        # Pin before the first await. The engine rechecks profile activation and
        # the store rechecks consent/evidence before committing the result.
        current = engine.current
        settings = current.settings()
        async with current._capacity:
            result = await current.summarize(item.scope, item.user, None, settings, force=True,revisit=item.revisit)
        return {**result, "profile_id": current.profile_id}

    return router
