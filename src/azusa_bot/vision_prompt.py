"""Bind short-lived visual evidence to the exact retained conversation events.

This module never downloads a URL or stores image bytes in a transcript.
Budgeting still operates on the original structured text; visual inputs are
attached only after trimming, with a separate conservative token reservation.
"""
from __future__ import annotations

from .prompts import DEFAULTS as PROMPT_DEFAULTS, prompt

from copy import deepcopy
import json


VISION_POLICY = PROMPT_DEFAULTS["vision"]


RECENT_IMAGE_SECONDS = 90


def _envelopes(messages, kind, refs=None):
    prefixes=(refs or {}).get("prompt_prefixes",{})
    keys=("group_header","followup_header") if kind=="followup" else ("direct_header" if kind=="reply" else "group_header",)
    # Fixed positions and expected scene-specific prefixes; raw history is never parsed.
    for index,key in enumerate(keys,1):
        if index>=len(messages):continue
        content=messages[index].get("content")
        prefix=prefixes.get(key,PROMPT_DEFAULTS[key])
        if isinstance(content,str) and content.startswith(prefix):
            yield json.loads(content[len(prefix):])


def visual_sources(messages, refs, kind, current_event=None, history_events=None):
    """Select visual sources from retained application data, never raw history
    claims. Old group pictures require an explicit reply to a retained message.
    A duplicate acknowledged-dialogue row must not refresh an image's age.
    """
    if kind in {"summary","group_topic","group_plan"}:
        return {}
    data_items = list(_envelopes(messages,kind,refs))
    anchor = next((data["current_message"] for data in data_items if "current_message" in data),{})
    group = anchor.get("kind") == "group"
    events, ages = [], {}
    for data in data_items:
        for key in ("recent_group_messages", "confirmed_direct_dialogue", "confirmed_dialogue"):
            for row in data.get(key, []):
                if row.get("role") == "user" and isinstance(row.get("message_id"), str):
                    event = row["message_id"]
                    events.append(event)
                    age = row.get("age_seconds")
                    if type(age) in (int,float) and 0 <= age <= 7200:
                        ages[event] = max(ages.get(event,0),int(age))
    for ref in [] if group else refs.get("history", []):
        event = (history_events or {}).get(ref["id"])
        if event:
            events.append(event)
    if current_event:
        events.append(current_event)
    # Keep the last occurrence, so the current input is never crowded out.
    ordered = list(dict.fromkeys(reversed(events)))[::-1]
    quoted_event = anchor.get("reply_to_id")
    if quoted_event in ordered and quoted_event != current_event:
        ordered.remove(quoted_event)
        ordered.insert(-1 if current_event in ordered else len(ordered),quoted_event)
    sources = {}
    for event in ordered:
        if event == current_event:
            sources[event] = {"relation":"current","age_seconds":0}
            continue
        age = ages.get(event)
        quoted = event == anchor.get("reply_to_id")
        if group and (age is None or (age > RECENT_IMAGE_SECONDS and not quoted)):
            continue
        sources[event] = {"relation":"reply" if quoted else "recent"}
        if age is not None:
            sources[event]["age_seconds"] = age
    return sources


def retained_events(messages, refs, kind, current_event=None, history_events=None):
    """Return oldest-to-newest eligible event IDs; current input wins priority."""
    return list(visual_sources(messages,refs,kind,current_event,history_events))


def attach_images(messages, images, metadata, *, current_event=None, labels=None):
    """Attach to a user message with explicit source labels, without mutation."""
    labels = PROMPT_DEFAULTS if labels is None else labels
    if not metadata:
        return messages
    result = deepcopy(messages)
    last = result[-1]
    if last["role"] != "user" or not isinstance(last["content"], str):
        raise ValueError("vision_prompt_shape_invalid")
    blocks = [{"type": "text", "text": last["content"]}]
    earlier = {}
    available = {row["image_id"]: row for row in images}
    for row in metadata:
        # URL, base64 and download errors never enter textual prompt metadata.
        safe = {key: row[key] for key in ("image_id", "event_id", "user_id", "status", "error_code",
                "width", "height", "animated_first_frame", "relation", "age_seconds") if key in row}
        item = available.get(row["image_id"])
        if not item:
            safe["status"] = "unavailable"
        target = blocks
        if current_event is not None and row.get("relation") != "current" and row["event_id"] != str(current_event):
            target = earlier.setdefault(row["event_id"],[{"type":"text", "text":
                labels["earlier_image"]}])
        target.append({"type": "text", "text": labels["image_label"] + json.dumps(safe, ensure_ascii=False)})
        if item:
            target.append({"type": "image_url", "image_url": {"url": item["data_url"], "detail": "high"}})
    # Earlier images get distinct user turns BEFORE the anchored current input.
    # A text-only current message stays text-only, even when earlier images exist.
    if len(blocks)>1:
        last["content"] = blocks
    result[-1:-1] = [{"role":"user","content":parts} for parts in earlier.values()]
    return result
