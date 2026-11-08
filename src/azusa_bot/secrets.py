"""Read secrets into process memory only; never serialize or return their values."""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
import yaml


@dataclass(repr=False)
class Credentials:
    deepseek_key: str = field(default="", repr=False)
    fish_key: str = field(default="", repr=False)
    fish_reference: str = field(default="", repr=False)
    fish_model: str = ""
    qq_app_id: str = ""
    qq_secret: str = field(default="", repr=False)
    compatible_key: str = field(default="", repr=False)
    gemini_balance_token: str = field(default="", repr=False)

    def status(self) -> dict:
        return {"deepseek": bool(self.deepseek_key), "fish": bool(self.fish_key),
                "voice_reference": bool(self.fish_reference), "qq": bool(self.qq_app_id and self.qq_secret)}


def load_credentials(read_dsh: bool = False) -> Credentials:
    c = Credentials()
    if read_dsh:
        home = Path.home()
        p = home / ".dsh/.credentials.yaml"
        if p.exists():
            data = yaml.safe_load(p.read_text()) or {}
            value = data.get("refs", {}).get("DEEPSEEK_API_KEY", "")
            # A stored browser-session reference is not an API key.
            if isinstance(value, str) and value.startswith("sk-"):
                c.deepseek_key = value
        p = home / ".dsh/profiles/desktop/cordis.patch.yml"
        if p.exists():
            for item in yaml.safe_load(p.read_text()) or []:
                if item.get("id") == "im-qqbot":
                    cfg = item.get("config", {})
                    c.qq_app_id = str(cfg.get("appId", ""))
                    c.qq_secret = str(cfg.get("appSecret", ""))
        # This existing user's local provider contains the working reference/model.
        # Only read a known file; do not search arbitrary documents or logs for secrets.
        p = home / "Downloads/fish-audio-qq-adapter/cordis.patch.yml"
        if p.exists():
            for item in yaml.safe_load(p.read_text()) or []:
                if item.get("id") == "fish-audio-qq-provider":
                    cfg = item.get("config", {})
                    c.fish_key = str(cfg.get("apiKey", "")) or os.getenv(str(cfg.get("apiKeyEnv", "FISH_API_KEY")), "")
                    c.fish_reference = str(cfg.get("referenceId", ""))
                    c.fish_model = str(cfg.get("model", ""))
        p = home / ".dsh/profiles/desktop/speech-provider-adapter/providers.json"
        if p.exists():
            data = json.loads(p.read_text())
            for profile in data.get("profiles", []):
                if (profile.get("id") == data.get("activeId") and profile.get("protocol") == "fish"
                    and str(profile.get("baseURL", "")).rstrip("/") in {"https://api.fish.audio", "https://api.fish.audio/v1"}):
                    selection = profile.get("selection", {})
                    c.fish_key = str(profile.get("apiKey", ""))
                    c.fish_reference = str(selection.get("voice", ""))
                    c.fish_model = str(selection.get("model", ""))
    for attr, env in (("deepseek_key", "DEEPSEEK_API_KEY"), ("fish_key", "FISH_API_KEY"),
                      ("fish_reference", "FISH_REFERENCE_ID"), ("fish_model", "FISH_MODEL"),
                      ("qq_app_id", "QQ_APP_ID"), ("qq_secret", "QQ_APP_SECRET")):
        if os.getenv(env):
            setattr(c, attr, os.environ[env].strip())
    return c


def resolve_credentials(root: Path, settings, base: Credentials | None = None):
    """ENV > project Keychain > explicitly enabled DSH. Voice GUI fields win.

    Compatible-provider keys have a separate, endpoint-bound slot; existing
    DeepSeek credentials can never be forwarded to a configurable address.
    """
    from .credential_store import CredentialStore
    c = base if base is not None else load_credentials(settings.read_existing_dsh)
    store, sources = CredentialStore(root), {}
    for name, env, label in (("deepseek_key", "DEEPSEEK_API_KEY", "deepseek"),
                             ("fish_key", "FISH_API_KEY", "fish"),
                             ("compatible_key", "AZUSA_COMPATIBLE_API_KEY", "compatible")):
        env_value = os.environ.get(env, "").strip()
        if name == "compatible_key" and os.environ.get("AZUSA_COMPATIBLE_BASE_URL", "").rstrip("/") != settings.llm_base_url:
            env_value = ""
        if env_value:
            setattr(c, name, env_value)
            source = "environment"
        else:
            saved = store.get(name, settings.llm_base_url)
            if saved:
                setattr(c, name, saved)
                source = "private_file" if store.storage_kind()=="private_file" else "keychain"
            else:
                source = "dsh" if getattr(c, name) else "none"
        sources[label] = {"configured":bool(getattr(c, name)), "source":source}
    if settings.fish_reference_id:
        c.fish_reference = settings.fish_reference_id
    if settings.fish_model_override or not c.fish_model:
        c.fish_model = settings.fish_model
    if settings.llm_provider == "gemini_balance":
        from .settings import token
        c.gemini_balance_token = token(root / "runtime/gemini-gateway", "access")
    return c, sources
