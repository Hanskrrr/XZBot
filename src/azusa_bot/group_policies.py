"""Account-owned, allowlisted per-group participation overrides (no secrets)."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from .settings import Settings, private_write, settings_lock

FIELDS = frozenset(k for k in Settings.model_fields if k.startswith(("group_chat_", "group_followup_", "group_heat_", "group_topic_", "group_idle_", "group_confidence_", "group_vote_"))) - {"group_idle_groups"}

def scopes(settings):
    platform = "onebot" if settings.transport=="onebot" else "qq"
    identity = settings.onebot_self_id if platform == "onebot" else settings.app_id
    groups = settings.onebot_allowed_groups if platform == "onebot" else settings.allowed_groups
    return [f"{platform}:{identity}:group:{g}" for g in groups] if identity else []

def read(root):
    path = Path(root)/"config/groups.json"
    if not path.exists(): return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict): raise ValueError("invalid_group_settings")
    return data

def revision(data):
    return hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()

def effective(root, settings, scope):
    if not scope or scope not in scopes(settings): return settings
    data = read(root).get(scope, {})
    if not isinstance(data, dict) or set(data)-FIELDS: raise ValueError("invalid_group_settings")
    values = settings.model_dump() | data
    group = scope.rsplit(":",1)[-1]
    # Legacy global idle selection remains intact until this group is saved.
    if "group_idle_enabled" in data:
        values["group_idle_groups"] = [group] if data["group_idle_enabled"] else []
    else:
        values["group_idle_enabled"] = settings.group_idle_enabled and group in settings.group_idle_groups
    return Settings.model_validate(values)

def curve(settings, silence_seconds):
    base = settings.group_chat_confidence_threshold
    floor = min(base, settings.group_confidence_floor)
    grace = settings.group_confidence_grace_minutes*60
    duration = settings.group_confidence_decay_minutes*60
    progress = min(1.,max(0.,(silence_seconds-grace)/duration))
    mode = settings.group_confidence_decay
    effective = base if mode == "fixed" else base-(base-floor)*(progress**2 if mode == "slow_start" else progress)
    return {"mode":mode,"base":base,"floor":floor,"grace_minutes":grace/60,
            "decay_minutes":duration/60,"silence_seconds":max(0.,silence_seconds),"effective":round(effective,6)}

class GroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: str = Field(min_length=1,max_length=512)
    revision: str
    settings: dict


def validate_values(base,changes):
    if not isinstance(changes,dict) or set(changes)-FIELDS:raise HTTPException(400,'unsupported_group_setting')
    try:value=Settings.model_validate(base.model_dump() | changes)
    except ValueError:raise HTTPException(422,'invalid_group_settings') from None
    if value.group_confidence_decay!='fixed' and value.group_confidence_floor>value.group_chat_confidence_threshold:
        raise HTTPException(422,'confidence_floor_exceeds_base')
    if value.group_idle_enabled and value.group_idle_start_hour==value.group_idle_end_hour:
        raise HTTPException(422,'idle_hours_empty')
    return {k:getattr(value,k) for k in sorted(FIELDS)}


def save_policy(current, scope, changes, expected_revision):
    """One CAS write path for direct edits and preset application.

    An identical save must not cancel replies or clear the group's context.
    Persisting explicit overrides can still pin an inherited but equal policy.
    """
    with settings_lock:
        base = current.settings()
        if scope not in scopes(base):
            raise HTTPException(400, "group_not_allowed")
        data = read(current.root)
        if revision(data.get(scope, {})) != expected_revision:
            raise HTTPException(409, "group_settings_changed_reload")
        before = effective(current.root, base, scope)
        values = validate_values(before, changes)
        changed = values != {key:getattr(before, key) for key in sorted(FIELDS)}
        if data.get(scope) != values:
            data[scope] = values
            private_write(current.root/"config/groups.json", json.dumps(data, ensure_ascii=False, indent=2)+"\n")
        if changed:
            current.clear_group_context(scope, reason="group_settings_changed")
        return changed

def create_router(engine):
    router=APIRouter(prefix="/admin/group-settings")
    def payload(scope=None):
        current=engine.current; base=current.settings(); allowed=scopes(base); data=read(current.root)
        if scope is not None and scope not in allowed: raise HTTPException(400,"group_not_allowed")
        chosen=scope or (allowed[0] if allowed else None)
        result={"scopes":[{"scope":s,"label":"群聊 "+s.rsplit(":",1)[-1],"overridden":s in data} for s in allowed],"scope":chosen}
        if chosen:
            settings=current.settings(chosen)
            result.update(settings={k:getattr(settings,k) for k in sorted(FIELDS)}, revision=revision(data.get(chosen,{})),
                inherited=chosen not in data, confidence=current.confidence_policy(chosen,settings))
        return result
    @router.get("")
    def get(scope: str | None = None): return payload(scope)
    @router.get('/status')
    def status(scope: str):
        from .group_monitor import snapshot
        current=engine.current
        if scope not in scopes(current.settings()):raise HTTPException(400,'group_not_allowed')
        return snapshot(current,scope)
    @router.put("")
    def put(item: GroupInput):
        current=engine.current
        changed = save_policy(current, item.scope, item.settings, item.revision)
        return payload(item.scope) | {"changed":changed}
    return router
