"""Profile-local, AstrBot-compatible example pairs and reply execution controls.

No second persona/history database: Markdown and this file remain authoritative.
See docs/astrbot-dialogue-integration.md for upstream references and boundaries.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import threading

from fastapi import APIRouter, HTTPException, Request
from .settings import private_write

LOCK = threading.RLock()
DEFAULT = {
    "examples_enabled": True,
    "begin_dialogs": [],
    "segmented_reply": {"enabled": False, "max_segments": 3, "interval_seconds": 1.2},
    "execution": {"max_tool_rounds": 2, "max_model_calls": 4},
}


def validate(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT):
        raise ValueError("conversation_fields_invalid")
    dialogs = value["begin_dialogs"]
    if (type(value["examples_enabled"]) is not bool or not isinstance(dialogs, list)
            or len(dialogs) > 24 or len(dialogs) % 2
            or any(not isinstance(s, str) or not s.strip() or len(s) > 1500 or "\x00" in s for s in dialogs)
            or sum(len(s) for s in dialogs) > 12000):
        raise ValueError("begin_dialogs_invalid")
    segment, execution = value["segmented_reply"], value["execution"]
    if (not isinstance(segment, dict) or set(segment) != set(DEFAULT["segmented_reply"])
            or type(segment["enabled"]) is not bool
            or type(segment["max_segments"]) is not int or not 2 <= segment["max_segments"] <= 3
            or type(segment["interval_seconds"]) not in (int, float)
            or not 0 <= segment["interval_seconds"] <= 5):
        raise ValueError("segmented_reply_invalid")
    if (not isinstance(execution, dict) or set(execution) != set(DEFAULT["execution"])
            or type(execution["max_tool_rounds"]) is not int or not 0 <= execution["max_tool_rounds"] <= 3
            or type(execution["max_model_calls"]) is not int or not 1 <= execution["max_model_calls"] <= 6
            or execution["max_model_calls"] <= execution["max_tool_rounds"]):
        raise ValueError("execution_budget_invalid")
    return deepcopy(value)


def path(root):
    target = Path(root) / "content/conversation.json"
    if target.is_symlink() or target.parent.is_symlink():
        raise ValueError("conversation_path_invalid")
    return target


def read(root):
    with LOCK:
        target = path(root)
        if not target.exists():
            return deepcopy(DEFAULT)
        if target.stat().st_size > 100000:
            raise ValueError("conversation_file_too_large")
        return validate(json.loads(target.read_text()))


def revision(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]


def example_revision(config):
    return revision({key: config[key] for key in ("examples_enabled", "begin_dialogs")})


def describe(root):
    config = read(root)
    return {"config": config, "revision": revision(config)}


def save(root, value, expected):
    with LOCK:
        if expected != revision(read(root)):
            raise RuntimeError("conversation_revision_changed")
        private_write(path(root), json.dumps(validate(value), ensure_ascii=False, indent=2) + "\n")


def examples(config):
    # Same alternating roles as AstrBot begin_dialogs, but never its DB history.
    if not config["examples_enabled"]:
        return []
    # The UI edits spoken lines; transport examples must obey the same JSON
    # contract as real replies. Plain assistant examples encourage prose that
    # the strict provider parser cannot consume, especially in direct chats.
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": text if i % 2 == 0 else
             json.dumps({"text": text, "mode": "text"}, ensure_ascii=False)}
            for i, text in enumerate(config["begin_dialogs"])]


def split_text(text, config):
    """Bounded post-generation splitting; preserve every non-whitespace character.

    Code and URLs remain a single output. No arbitrary user regex or text slicing.
    Combining overflow into the last segment avoids losing the answer's ending.
    """
    if not config["enabled"] or re.search(r"https?://|```|`", text):
        return [text]
    units = re.findall(r'.*?[。！？!?]+[”’」』）)]*\s*|.*?\n+|.+$', text, re.S)
    parts = []
    for unit in units:
        unit = unit.strip()
        if not unit:
            continue
        if parts and len(parts[-1]) < 8:
            parts[-1] += unit
        else:
            parts.append(unit)
    limit = config["max_segments"]
    if len(parts) > limit:
        parts[limit - 1:] = ["".join(parts[limit - 1:])]
    if not parts or re.sub(r"\s", "", "".join(parts)) != re.sub(r"\s", "", text):
        return [text]
    return parts


def create_router(engine):
    router = APIRouter(prefix="/admin/conversation")

    @router.get("")
    def get():
        return describe(engine.current.content_root)

    @router.put("")
    async def put(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"config", "revision"}:
                raise ValueError("conversation_fields_invalid")
            current = engine.current
            save(current.content_root, body["config"], body["revision"])
            current.invalidate_groups(reason="conversation_settings_changed")
            return describe(current.content_root)
        except RuntimeError:
            raise HTTPException(409, "conversation_revision_changed") from None
        except (ValueError, TypeError):
            raise HTTPException(400, "conversation_config_invalid") from None

    @router.post("/preview")
    async def preview(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"config", "text"} or not isinstance(body["text"], str) or len(body["text"]) > 3000:
                raise ValueError()
            config = validate(body["config"])
            return {"segments": split_text(body["text"], config["segmented_reply"])}
        except (ValueError, TypeError):
            raise HTTPException(400, "conversation_preview_invalid") from None

    return router
