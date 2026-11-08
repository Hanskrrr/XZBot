"""Per-profile, per-group memory collection and summary cadence.

Only policy values are reusable. Member choices and stored records never enter
presets, and applying a policy cannot enrol a member who explicitly opted out.
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from .group_policies import scopes, revision
from .settings import settings_lock

FIELDS=frozenset({'group_collection_enabled','auto_summary_every','auto_summary_max_wait_minutes'})


class Values(BaseModel):
    model_config=ConfigDict(extra='forbid')
    group_collection_enabled: StrictBool
    auto_summary_every: int = Field(strict=True,ge=4,le=100)
    auto_summary_max_wait_minutes: int = Field(strict=True,ge=5,le=1440)


def validate_values(base,changes):
    try:return Values.model_validate(changes).model_dump()
    except ValueError:raise HTTPException(422,'invalid_memory_settings') from None


def describe(current,scope):
    base=current.settings()
    if scope not in scopes(base):raise HTTPException(400,'group_not_allowed')
    with current.memory._lock:
        policy=current.memory.summary_policy(scope)
        values={key:policy.get(key,getattr(base,key)) for key in ('auto_summary_every','auto_summary_max_wait_minutes')}
        values['group_collection_enabled']=current.memory.group_collection_enabled(scope)
        return {'scope':scope,'settings':values,'revision':revision({'values':values,'override':bool(policy)}),
                'inherited':not policy,'profile_id':current.profile_id,
                'retention':{'history_days':current.memory.history_days,'max_history':current.memory.max_history}}


def save_policy(current,scope,changes,expected_revision):
    values=validate_values(None,changes)
    with settings_lock,current.memory._lock:
        before=describe(current,scope)
        if before['revision']!=expected_revision:raise HTTPException(409,'memory_settings_changed_reload')
        changed=before['settings']!=values
        if changed or before['inherited']:
            current.memory.configure_group_memory(scope,values['auto_summary_every'],values['auto_summary_max_wait_minutes'],values['group_collection_enabled'])
        if before['settings']['group_collection_enabled']!=values['group_collection_enabled']:
            current.clear_group_context(scope,reason='privacy_changed')
        return changed


class PolicyInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    scope: str = Field(min_length=1,max_length=512)
    revision: str = Field(max_length=64)
    settings: Values


def create_router(engine):
    router=APIRouter(prefix='/admin/memory-settings')

    @router.get('')
    def get(scope:str):return describe(engine.current,scope)

    @router.put('')
    def put(item:PolicyInput):
        current=engine.current
        changed=save_policy(current,item.scope,item.settings.model_dump(),item.revision)
        return describe(current,item.scope)|{'changed':changed}

    return router
