from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator


# Canonical decimal IDs prevent aliases such as "00123" sharing an identity.
QQ_ID_PATTERN = r"^[1-9][0-9]{0,19}$"
settings_lock = threading.RLock()
QQNumber = Annotated[str, Field(min_length=1, max_length=20, pattern=QQ_ID_PATTERN)]


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["group", "dm"]
    scope_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")
    user_id: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_-]+$")


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: Literal["official", "onebot"] = "official"
    app_id: str = ""
    allowed_groups: list[str] = []
    allowed_dm_users: list[str] = []
    owners: list[Owner] = []
    qq_enabled: bool = False
    onebot_enabled: bool = False
    onebot_self_id: str = Field(default="", max_length=20, pattern=r"^(?:[1-9][0-9]{0,19})?$")
    onebot_allowed_groups: list[QQNumber] = []
    onebot_allowed_dm_users: list[QQNumber] = []
    onebot_owners: list[Owner] = []
    read_existing_dsh: bool = False
    provider_mode: Literal["mock", "cloud"] = "mock"
    llm_provider: Literal["deepseek", "openai_compatible", "gemini_balance"] = "deepseek"
    deepseek_model: str = Field(default="deepseek-flash", min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$")
    llm_base_url: str = "https://api.openai.com/v1"
    compatible_model: str = Field(default="", max_length=160, pattern=r"^[A-Za-z0-9._:/-]*$")
    gemini_model: str = Field(default="gemini-2.5-flash", min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$")
    fish_model: str = Field(default="s2.1-pro-free", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    fish_model_override: bool = False
    fish_reference_id: str = Field(default="", max_length=160, pattern=r"^[A-Za-z0-9_-]*$")
    voice_enabled: bool = False
    voice_authorized: bool = False
    auto_summary_every: int = Field(default=12, ge=4, le=100)
    auto_summary_max_wait_minutes: int = Field(default=30, ge=5, le=1440)
    max_input_chars: int = Field(default=4000, ge=100, le=10000)
    max_voice_chars: int = Field(default=180, ge=10, le=500)
    min_interval_seconds: float = Field(default=2, ge=0, le=60)
    daily_cloud_requests: int = Field(default=200, ge=1, le=10000)
    context_window_tokens: int = Field(default=32768, ge=4096, le=1000000, strict=True)
    vision_enabled: bool = Field(default=True, strict=True)
    group_chat_enabled: bool = False
    group_chat_confidence_threshold: float = Field(default=0.8, ge=0, le=1, strict=True)
    group_vote_enabled: bool = False
    group_vote_weight_relevance: int = Field(default=25, ge=0, le=100, strict=True)
    group_vote_weight_understanding: int = Field(default=30, ge=0, le=100, strict=True)
    group_vote_weight_contribution: int = Field(default=25, ge=0, le=100, strict=True)
    group_vote_weight_timing: int = Field(default=20, ge=0, le=100, strict=True)
    web_search_enabled: bool = False
    web_search_show_sources: bool = False
    group_confidence_decay: Literal["fixed", "slow_start", "linear"] = "fixed"
    group_confidence_floor: float = Field(default=0.65, ge=0, le=1, strict=True)
    group_confidence_grace_minutes: int = Field(default=10, ge=0, le=1440)
    group_confidence_decay_minutes: int = Field(default=120, ge=1, le=10080)
    group_topic_enabled: bool = False
    group_topic_minutes: int = Field(default=3, ge=1, le=10)
    group_topic_messages: int = Field(default=20, ge=3, le=50)
    group_topic_confidence_threshold: float = Field(default=0.8, ge=0, le=1, strict=True)
    group_topic_interval_seconds: int = Field(default=60, ge=20, le=600)
    group_topic_daily_limit: int = Field(default=30, ge=1, le=500)
    group_heat_enabled: bool = False
    group_heat_require_topic: bool = True
    group_heat_window_seconds: int = Field(default=90, ge=30, le=300)
    group_heat_min_messages: int = Field(default=8, ge=4, le=50)
    group_heat_min_members: int = Field(default=3, ge=2, le=20)
    group_heat_interval_seconds: int = Field(default=20, ge=10, le=120)
    group_heat_max_replies: int = Field(default=3, ge=1, le=6)
    web_search_daily_limit: int = Field(default=30, ge=1, le=500)
    group_chat_debounce_seconds: float = Field(default=3, ge=0, le=15)
    group_chat_cooldown_seconds: int = Field(default=90, ge=15, le=3600)
    group_chat_hourly_limit: int = Field(default=8, ge=1, le=60)
    group_chat_daily_limit: int = Field(default=40, ge=1, le=300)
    group_chat_min_messages: int = Field(default=3, ge=1, le=20)
    group_chat_decision_interval_seconds: int = Field(default=30, ge=5, le=600)
    group_chat_context_messages: int = Field(default=30, ge=5, le=100)
    group_chat_context_minutes: int = Field(default=5, ge=1, le=120)
    group_chat_max_reply_chars: int = Field(default=220, ge=40, le=600)
    group_followup_enabled: bool = True
    group_followup_seconds: int = Field(default=180, ge=30, le=900)
    group_followup_max_turns: int = Field(default=4, ge=1, le=12)
    group_followup_min_interval_seconds: int = Field(default=5, ge=2, le=30)
    group_idle_enabled: bool = False
    group_idle_groups: list[QQNumber] = Field(default_factory=list, max_length=100)
    group_idle_minutes: int = Field(default=30, ge=10, le=1440)
    group_idle_daily_limit: int = Field(default=3, ge=1, le=20)
    group_idle_start_hour: int = Field(default=9, ge=0, le=23)
    group_idle_end_hour: int = Field(default=23, ge=0, le=24)

    @field_validator("llm_base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        if any(ord(c) < 32 for c in value):
            raise ValueError("invalid_url_character")
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (len(value) > 300 or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(r"https://[A-Za-z0-9.\-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*", value)):
            raise ValueError("HTTPS API base URL required; no credentials or query parameters")
        return value

    @field_validator("onebot_owners")
    @classmethod
    def validate_onebot_owners(cls, owners: list[Owner]) -> list[Owner]:
        for owner in owners:
            if not re.fullmatch(QQ_ID_PATTERN, owner.scope_id) or not re.fullmatch(QQ_ID_PATTERN, owner.user_id):
                raise ValueError("OneBot owner bindings require numeric QQ/group IDs")
            if owner.kind == "dm" and owner.scope_id != owner.user_id:
                raise ValueError("OneBot private owner scope must equal its QQ user ID")
        return owners

    @model_validator(mode="after")
    def validate_vote_weights(self):
        if not sum(getattr(self,"group_vote_weight_"+k) for k in ("relevance","understanding","contribution","timing")):
            raise ValueError("vote_weights_empty")
        return self

    def is_owner(self, kind: str, scope_id: str, user_id: str, transport: str = "official") -> bool:
        bindings = self.onebot_owners if transport == "onebot" else self.owners if transport == "official" else []
        return any(x.kind == kind and x.scope_id == scope_id and x.user_id == user_id for x in bindings)


def root_path() -> Path:
    return Path(os.environ.get("AZUSA_ROOT", Path(__file__).resolve().parents[2])).resolve()


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    from .host import protect
    tmp = Path(filename)
    protect(tmp)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def read_settings(root: Path) -> Settings:
    path = root / "config/local.json"
    return Settings.model_validate_json(path.read_text()) if path.exists() else Settings()


def write_settings(root: Path, settings: Settings) -> None:
    with settings_lock:
        private_write(root / "config/local.json", json.dumps(settings.model_dump(), ensure_ascii=False, indent=2) + "\n")


def token(root: Path, name: str) -> str:
    path = root / "data" / (name + ".token")
    if not path.exists():
        private_write(path, secrets.token_urlsafe(40))
    path.chmod(0o600)
    return path.read_text().strip()
