"""Write-only platform credentials; no values are returned to the admin UI."""
import re
import threading
from .credential_store import CredentialStore, CredentialStoreError

FIELDS = ("bilibili_sessdata", "bilibili_bili_jct", "bilibili_buvid3", "steam_api_key", "weibo_sub", "weibo_cookie")
_LOCK = threading.RLock()


def _weibo_cookie(value):
    """Validate a browser request Cookie, not Set-Cookie or arbitrary headers."""
    cookies = {}
    for part in value.split(";"):
        key, separator, content = part.strip().partition("=")
        if (not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,80}", key)
                or key.lower() in {"domain", "path", "expires", "max-age", "secure", "httponly", "samesite"}
                or key in cookies or not re.fullmatch(r'[\x21\x23-\x2B\x2D-\x3A\x3C-\x5B\x5D-\x7E]*', content)):
            raise ValueError("subscription_credentials_value_invalid")
        cookies[key] = content
    if not cookies.get("SUB") or len(cookies) > 64:
        raise ValueError("subscription_credentials_value_invalid")
    return "; ".join(key + "=" + content for key, content in cookies.items())


def credential_status(root):
    store = CredentialStore(root)
    with _LOCK:
        return {"storage": store.storage_kind(), "fields": {
            name: {"configured": bool(store.get(name))} for name in FIELDS}}


def credential_values(root, names):
    if any(name not in FIELDS for name in names):
        raise ValueError("subscription_credentials_fields_invalid")
    with _LOCK:
        store = CredentialStore(root)
        return {name: store.get(name) for name in names}


def update_credentials(root, body):
    if not isinstance(body, dict) or set(body) - set(FIELDS) - {"clear"}:
        raise ValueError("subscription_credentials_fields_invalid")
    clear = body.get("clear", [])
    if not isinstance(clear, list) or any(not isinstance(name,str) or name not in FIELDS for name in clear):
        raise ValueError("subscription_credentials_fields_invalid")
    for name, value in body.items():
        if name == "clear":
            continue
        if (not isinstance(value, str) or len(value) > 8192
                or any(ord(c) < 32 or ord(c) > 126 for c in value)
                or name != "weibo_cookie" and ";" in value or name in clear and value):
            raise ValueError("subscription_credentials_value_invalid")
        if name == "weibo_cookie" and value.strip():
            _weibo_cookie(value.strip())
    with _LOCK:
        store = CredentialStore(root)
        changes={name:("" if name in clear else body[name].strip()) for name in FIELDS
                 if name in clear or body.get(name, "").strip()}
        if changes.get("weibo_cookie"):
            changes["weibo_cookie"] = _weibo_cookie(changes["weibo_cookie"])
        previous={name:store.get(name) for name in changes}
        touched=[]
        try:
            for name,value in changes.items():
                touched.append(name)
                store.set(name,value)
            return credential_status(root)
        except CredentialStoreError:
            failed=False
            for name in reversed(touched):
                try:store.set(name,previous[name])
                except CredentialStoreError:failed=True
            raise CredentialStoreError("subscription_credentials_rollback_failed" if failed else "subscription_credentials_write_failed") from None
