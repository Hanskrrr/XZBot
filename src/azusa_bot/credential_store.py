"""Project-scoped native macOS Keychain, using the maintained keyring backend.

An explicitly installed migration marker can select a private POSIX file.
No automatic plaintext fallback after a Keychain error; no CLI subprocess
containing a secret. Injectable in-memory backend is used by automated tests.
"""
from __future__ import annotations
import hashlib
import platform
import re
from pathlib import Path


class CredentialStoreError(RuntimeError):
    pass


class CredentialStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.service = "app.azusa.private-bot." + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:24]
        # A project move must preserve every endpoint-bound Keychain account.
        # This marker contains only the service identifier, never credentials.
        marker = root / "data/keychain-service"
        if marker.exists():
            if marker.is_symlink() or marker.stat().st_size > 100:
                raise CredentialStoreError("invalid_keychain_service")
            service = marker.read_text().strip()
            if not re.fullmatch(r"app\.azusa\.private-bot\.[a-f0-9]{24}", service):
                raise CredentialStoreError("invalid_keychain_service")
            self.service = service

    def storage_kind(self):
        marker=self.root/'data/credential-backend.json'
        return 'private_file' if marker.exists() or marker.is_symlink() else 'windows_credential_manager' if platform.system()=='Windows' else 'macos_keychain'

    def backend(self):
        if self.storage_kind()=='private_file':
            try:
                from .portable_credentials import PrivateFileBackend,read_private,MARKER
                if read_private(self.root/'data/credential-backend.json',1024)!=MARKER:
                    raise ValueError()
                return PrivateFileBackend(self.root)
            except Exception:
                raise CredentialStoreError('private_credentials_unavailable') from None
        if platform.system() == "Windows":
            from keyring.backends.Windows import WinVaultKeyring
            return WinVaultKeyring()
        if platform.system() != "Darwin":
            raise CredentialStoreError("keychain_macos_required")
        try:
            from keyring.backends.macOS import Keyring
            return Keyring()
        except Exception:
            raise CredentialStoreError("keychain_unavailable") from None

    @staticmethod
    def account(name: str, base_url: str = "") -> str:
        if name not in {"deepseek_key", "fish_key", "compatible_key", "gemini_keys",
                        "steam_api_key", "bilibili_sessdata", "bilibili_bili_jct", "bilibili_buvid3", "weibo_sub", "weibo_cookie"}:
            raise ValueError("unsupported_credential")
        if name == "compatible_key":
            if not base_url:
                raise ValueError("endpoint_required")
            return name + ":" + hashlib.sha256(base_url.encode()).hexdigest()
        return name

    def get(self, name: str, base_url: str = "") -> str:
        try:
            return self.backend().get_password(self.service, self.account(name, base_url)) or ""
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("keychain_read_failed") from None

    def set(self, name: str, value: str, base_url: str = "") -> None:
        try:
            backend, account = self.backend(), self.account(name, base_url)
            if value:
                backend.set_password(self.service, account, value)
            elif backend.get_password(self.service, account) is not None:
                backend.delete_password(self.service, account)
        except CredentialStoreError:
            raise
        except Exception:
            raise CredentialStoreError("keychain_write_failed") from None
