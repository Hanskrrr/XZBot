"""Configured role names are conversation cues, never trusted identities."""
from __future__ import annotations

import re
import unicodedata


_COORDINATED_INVITATION = re.compile(
    r"(?:说话来|"
    r"(?:你们(?:俩|两位)?\s*(?:都|一起|分别)?|"
    r"(?:都|一起|分别)\s*(?=来|聊|说说|说几句|说句话|回答一下))\s*(?:来\s*)?"
    r"(?:说话|说说|说几句|说句话|聊聊|聊聊天|聊一聊|聊几句|回答(?:一下)?))"
    r"(?:[吧呀啊哦])?[。.!?]*\Z"
)


def _coordinated_groups(text, aliases):
    if "和" not in text and "与" not in text:
        return ()
    invitation = _COORDINATED_INVITATION.search(text.rstrip())
    if invitation is None:
        return ()
    names = text[:invitation.start()].rstrip(" \t,:、")
    matches = []
    for conjunction in re.finditer(r"[和与]", names):
        first = aliases.get(names[:conjunction.start()].strip())
        second = aliases.get(names[conjunction.end():].strip())
        if first and second:
            matches.append((frozenset(first), frozenset(second)))
    # Both names must resolve locally. An ambiguous split stays a candidate
    # for semantic assessment instead of choosing a recipient by prefix.
    return matches[0] if len(matches) == 1 else ()


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


def vocative_target_groups(text, mapping) -> tuple[frozenset[str], ...]:
    """Return candidate account IDs for each distinct leading spoken name.

    Unlike a general name mention, this requires an opening addressee followed
    by a separator, a second-person pronoun, or the end of the message. An
    explicit joint invitation can also address two locally known aliases. It
    does not establish permissions or prove the speaker's conversational intent.
    One name shared by two accounts remains one ambiguous group, not proof that
    the human deliberately addressed two different participants.
    """
    if not isinstance(text,str) or not isinstance(mapping,dict):
        return ()
    text = unicodedata.normalize("NFKC",text[:600]).casefold() + ("\0" if len(text)>600 else "")
    aliases = {}
    for account,names in list(mapping.items())[:16]:
        if not isinstance(account,str) or not isinstance(names,(list,tuple)):
            continue
        for name in names[:32]:
            if (not isinstance(name,str) or not 1 <= len(name.strip()) <= 60
                    or not any(c.isalnum() for c in name)
                    or any(unicodedata.category(c).startswith("C") for c in name)):
                continue
            normalized = unicodedata.normalize("NFKC",name.strip()).casefold()
            aliases.setdefault(normalized,set()).add(account)
    if groups := _coordinated_groups(text, aliases):
        return groups
    ordered = sorted(aliases,key=len,reverse=True)

    def separator(char):
        category = unicodedata.category(char)
        return char.isspace() or category.startswith("P") and category != "Pc"

    def boundary(end):
        if end == len(text) or text[end].isspace() or text[end] in ",:、你您妳":
            return True
        following = end
        while following < len(text) and separator(text[following]):
            following += 1
        if following == end:
            return False
        if following == len(text) or any(c in ",:、" for c in text[end:following]):
            return True
        # Other punctuation may separate consecutive addressees, but a name
        # embedded in a possessive or compound such as "Ann's" is not a call.
        for alias in ordered:
            if text.startswith(alias,following):
                after = following+len(alias)
                if after == len(text) or separator(text[after]) or text[after] in "你您妳":
                    return True
        return False

    offset, groups, seen = 0, [], set()
    while offset < len(text):
        while offset < len(text) and separator(text[offset]):
            offset += 1
        matched = None
        for alias in ordered:
            if not text.startswith(alias,offset):
                continue
            end = offset+len(alias)
            if boundary(end):
                matched = alias
                break
        if matched is None:
            break
        if matched not in seen:
            groups.append(frozenset(aliases[matched]))
            seen.add(matched)
        offset += len(matched)
        if offset < len(text) and text[offset] in "你您妳":
            break
    return tuple(groups)


def vocative_targets(text, mapping) -> set[str]:
    """Flatten the candidate IDs; use groups to distinguish plural/ambiguity."""
    return set().union(*vocative_target_groups(text,mapping))
