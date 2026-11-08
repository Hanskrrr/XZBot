"""Configured role names are conversation cues, never trusted identities."""
from __future__ import annotations

import re
import unicodedata


def address_names(value) -> list[str]:
    """Validate and normalize administrator-provided, bounded role aliases."""
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("profile_address_names_invalid")
    result, seen, canonical_seen = [], set(), set()
    for item in value:
        if (not isinstance(item, str) or any(unicodedata.category(c).startswith("C") for c in item)
                or not 2 <= len(item.strip()) <= 30 or not any(c.isalnum() for c in item)):
            raise ValueError("profile_address_names_invalid")
        item = item.strip()
        # Keep the existing validation of literally/canonically repeated names.
        # Older profiles could contain both full-width and ordinary spelling;
        # collapse those new compatibility duplicates without disabling every
        # alias in an otherwise valid saved profile. Display spelling stays as-is.
        canonical = unicodedata.normalize("NFC", item).casefold()
        if canonical in canonical_seen:
            raise ValueError("profile_address_names_invalid")
        canonical_seen.add(canonical)
        key = unicodedata.normalize("NFKC", item).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def is_addressed(text, names) -> bool:
    """Find a bounded alias cue; the model still decides who is being addressed.

    Chinese names may occur within normal unspaced sentences. Latin/alphanumeric
    names require boundaries against other Latin letters/digits/underscores so
    names such as ``Ann`` do not match ``Anna`` or ``banner``.
    """
    if not isinstance(text, str) or not text:
        return False
    try:
        names = address_names(names)
    except ValueError:
        return False
    text = unicodedata.normalize("NFKC", text).casefold()
    for name in names:
        needle = unicodedata.normalize("NFKC", name).casefold()
        if any("\u3400" <= c <= "\u9fff" or "\U00020000" <= c <= "\U0003134f" for c in needle):
            if needle in text:
                return True
        elif re.search(r"(?<![a-z0-9_])" + re.escape(needle) + r"(?![a-z0-9_])", text):
            return True
    return False
