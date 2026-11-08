"""Admin-only provider settings and write-only credentials, separate from chat."""
from __future__ import annotations
from pathlib import Path
import threading
from .credential_store import CredentialStore, CredentialStoreError
from .secrets import resolve_credentials
from .settings import Settings, read_settings, write_settings, settings_lock

FIELDS = frozenset(('provider_mode','llm_provider','deepseek_model','llm_base_url','compatible_model',
                    'gemini_model','context_window_tokens','vision_enabled','web_search_enabled','web_search_daily_limit','web_search_show_sources',
                    'fish_model','fish_reference_id','voice_enabled','voice_authorized','read_existing_dsh'))
SECRET_FIELDS = frozenset(('deepseek_key','compatible_key','fish_key'))
_lock = threading.RLock()


def describe(root: Path, *, profile_store=None, profile_id=None) -> dict:
    with _lock:
        settings = read_settings(root)
        if profile_store:
            settings = Settings.model_validate(settings.model_dump() | profile_store.voice(profile_id))
        credentials, sources = resolve_credentials(root, settings)
        if profile_store and profile_id != 'default':
            credentials.fish_reference = settings.fish_reference_id
            credentials.fish_model = settings.fish_model
        editable = {k:getattr(settings,k) for k in sorted(FIELDS)}
        # Before the first GUI save, show the actual legacy model so merely
        # saving an API key cannot silently replace a working TTS model.
        editable['fish_model'] = credentials.fish_model
        return {'settings':editable,
                'credentials':sources,
                'effective':{'fish_reference_id':credentials.fish_reference,'fish_model':credentials.fish_model},
                'storage': '受文件权限保护的迁移凭据（未加密）' if CredentialStore(root).storage_kind()=='private_file' else 'macOS Keychain'}


def save(root: Path, body: dict, *, profile_store=None, profile_id=None) -> dict:
    if set(body) - {'settings','secrets','clear_secrets'}:
        raise ValueError('unsupported_fields')
    changes, secrets, clears = body.get('settings',{}), body.get('secrets',{}), body.get('clear_secrets',[])
    if (not isinstance(changes,dict) or set(changes)-FIELDS or not isinstance(secrets,dict)
        or set(secrets)-SECRET_FIELDS or not isinstance(clears,list)
        or any(not isinstance(x,str) or x not in SECRET_FIELDS for x in clears)
        or set(clears) & set(secrets)):
        raise ValueError('invalid_provider_fields')
    for value in secrets.values():
        if not isinstance(value,str) or not 1 <= len(value) <= 4096 or any(c.isspace() or ord(c)<32 for c in value):
            raise ValueError('invalid_credential')
    with _lock:
        old = read_settings(root)
        if profile_store:
            old = Settings.model_validate(old.model_dump() | profile_store.voice(profile_id))
        updated = Settings.model_validate(old.model_dump() | changes)
        if 'fish_model' in changes:
            updated.fish_model_override = True
        if updated.voice_enabled and not updated.voice_authorized:
            raise ValueError('voice_authorization_required')
        if profile_store and profile_id != 'default' and updated.voice_enabled and not updated.fish_reference_id:
            raise ValueError('profile_voice_requires_reference_and_authorization')
        if updated.provider_mode == 'cloud' and updated.llm_provider == 'openai_compatible' and not updated.compatible_model:
            raise ValueError('compatible_model_required')
        store = CredentialStore(root)
        replacements = secrets | {name:'' for name in clears}
        # Read everything before mutating anything. Roll back if Keychain or
        # settings persistence fails; caller never receives a secret value.
        previous = {name:store.get(name,updated.llm_base_url) for name in replacements}
        changed = []
        previous_voice = profile_store.voice(profile_id) if profile_store else None
        voice_changed = False
        try:
            for name, value in replacements.items():
                store.set(name,value,updated.llm_base_url)
                changed.append(name)
            # Keychain may wait for a system prompt. Do not restore an old QQ
            # switch/whitelist snapshot after that wait. Only merge requested
            # provider fields into the latest settings under the write lock.
            with settings_lock:
                latest = read_settings(root)
                patch = {name:getattr(updated,name) for name in changes}
                if 'fish_model' in changes:
                    patch['fish_model_override'] = True
                if profile_store:
                    from .profiles import VOICE_FIELDS, VOICE_STORED
                    voice_patch = {k:v for k,v in patch.items() if k in VOICE_FIELDS}
                    if voice_patch:
                        profile_store.update(profile_id,{'voice':voice_patch})
                        voice_changed = True
                    if profile_id != 'default':
                        patch = {k:v for k,v in patch.items() if k not in VOICE_STORED}
                write_settings(root,Settings.model_validate(latest.model_dump() | patch))
        except Exception:
            rollback_ok = True
            if voice_changed:
                try:
                    profile_store.restore_voice(profile_id,previous_voice)
                except Exception:
                    rollback_ok = False
            for name in reversed(changed):
                try:
                    store.set(name,previous[name],updated.llm_base_url)
                except Exception:
                    rollback_ok = False
            raise CredentialStoreError('provider_save_failed' if rollback_ok else 'provider_save_rollback_incomplete') from None
        return describe(root,profile_store=profile_store,profile_id=profile_id)
