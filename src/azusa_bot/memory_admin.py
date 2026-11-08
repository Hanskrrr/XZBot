"""Authenticated local administration of the active profile's memory pipeline."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool


class MemberInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(min_length=1, max_length=512)
    user: str = Field(min_length=1, max_length=160)


class ConsentInput(MemberInput):
    enabled: StrictBool


class GroupCollectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(min_length=1, max_length=512)
    enabled: StrictBool


def create_router(engine) -> APIRouter:
    """Caller must mount with its local-admin authentication dependency."""
    router = APIRouter(prefix="/admin/memory")

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
    async def summarize(item: MemberInput):
        # Pin before the first await. The engine rechecks profile activation and
        # the store rechecks consent/evidence before committing the result.
        current = engine.current
        settings = current.settings()
        async with current._capacity:
            result = await current.summarize(item.scope, item.user, None, settings, force=True)
        return {**result, "profile_id": current.profile_id}

    return router
