"""Local request budgeting without rewriting facts or deleting stored history.

UTF-8 bytes plus protocol headroom are a deliberately conservative estimate for
the supported text-chat requests, not an exact model tokenizer or a guarantee
about an arbitrary gateway. Only the known prompt structures built by core.py
may be shortened. System instructions, full persona, explicitly pinned whole
background documents and the current question are never sliced or dropped;
a request that still cannot fit must not be sent.
"""
from __future__ import annotations

from copy import deepcopy
import json
from .prompts import DEFAULTS


DIRECT_PREFIX = DEFAULTS['direct_header']
GROUP_PREFIX = DEFAULTS['group_header']
FOLLOWUP_PREFIX = DEFAULTS['followup_header']
REMOVED_KEYS = ("assets", "history_lookup", "history", "group_context", "dialogue", "memories", "knowledge")


def estimate_tokens(messages: list[dict]) -> int:
    """Conservative byte estimate; it intentionally does not claim exact tokens."""
    if not isinstance(messages, list):
        raise ValueError("context_messages_invalid")
    total = 256
    for message in messages:
        if (not isinstance(message, dict) or not isinstance(message.get("role"), str)
                or not isinstance(message.get("content"), str)):
            raise ValueError("context_messages_invalid")
        if set(message) == {"role", "content"} and message["role"] in {"system", "user", "assistant"}:
            total += len(message["content"].encode("utf-8")) + 32
        else:
            # Future/unsupported envelopes must not hide large metadata from
            # the estimate. They will not qualify for structural trimming.
            total += len(json.dumps(message, ensure_ascii=False).encode("utf-8")) + 32
    return total


def _json_message(messages, index, prefix):
    message = messages[index]
    if message["role"] != "user" or not message["content"].startswith(prefix):
        raise ValueError("context_shape_unknown")
    value = json.loads(message["content"][len(prefix):])
    if not isinstance(value, dict):
        raise ValueError("context_shape_unknown")
    return value


def _rows(value):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("context_shape_unknown")
    return value


def _aligned(rows, refs, keys):
    _rows(rows); _rows(refs)
    if len(rows) != len(refs) or any(
            any(key not in row or key not in ref or row[key] != ref[key] for key in keys)
            for row, ref in zip(rows, refs)):
        raise ValueError("context_refs_mismatch")


def _turn_prefix_length(rows):
    """Length of an old whole turn, or zero when only the protected turn remains.

    Consecutive user statements form one turn. Its following assistant output
    stays attached. A leading assistant left over from an earlier history limit
    is expendable, so trimming never creates a new orphaned assistant message.
    """
    if not rows:
        return 0
    if any(row.get("role") not in {"user", "assistant"} for row in rows):
        raise ValueError("context_shape_unknown")
    if rows[0]["role"] == "assistant":
        index = 0
        while index < len(rows) and rows[index]["role"] == "assistant":
            index += 1
        return index
    answered = False
    for index, row in enumerate(rows):
        if row["role"] == "assistant":
            answered = True
        elif answered:
            return index
    return 0


class _Context:
    def __init__(self, messages, refs, kind):
        self.messages, self.refs, self.kind = messages, refs, kind
        prefixes=refs.get("prompt_prefixes",{})
        self.direct_prefix=prefixes.get("direct_header",DIRECT_PREFIX)
        self.group_prefix=prefixes.get("group_header",GROUP_PREFIX)
        self.followup_prefix=prefixes.get("followup_header",FOLLOWUP_PREFIX)
        self.data = self.followup = None
        self.history = []
        self.summary = None
        if (not messages or messages[0]["role"] != "system"
                or any(set(message) != {"role", "content"} for message in messages)):
            raise ValueError("context_shape_unknown")
        for key in ("assets", "history", "memories", "knowledge"):
            _rows(refs.get(key))
        if kind == "summary":
            if len(messages) != 2 or messages[1]["role"] != "user":
                raise ValueError("context_shape_unknown")
            self.summary = _rows(json.loads(messages[1]["content"]))
            if not self.summary or any(row.get("role") != "user" for row in self.summary):
                raise ValueError("context_shape_unknown")
            _aligned(self.summary, refs["history"], ("id",))
            return
        if kind == "reply":
            if len(messages) < 3 or messages[-1]["role"] != "user":
                raise ValueError("context_shape_unknown")
            self.data = _json_message(messages, 1, self.direct_prefix)
            if not isinstance(self.data.get("trusted_identity"), dict):
                raise ValueError("context_shape_unknown")
            self.history = messages[2:-1]
            if (len(self.history) != len(refs["history"])
                    or any(row["role"] not in {"user", "assistant"} for row in self.history)):
                raise ValueError("context_refs_mismatch")
            self.memory_key = "retrieved_memory"
        elif kind in {"group_decision", "followup", "group_idle", "group_topic"}:
            if len(messages) != (3 if kind == "followup" else 2):
                raise ValueError("context_shape_unknown")
            self.data = _json_message(messages, 1, self.group_prefix)
            if "latest_speaker_id" not in self.data:
                raise ValueError("context_shape_unknown")
            self.memory_key = "latest_speaker_own_memories"
            if kind == "followup":
                self.followup = _json_message(messages, 2, self.followup_prefix)
                if (not isinstance(self.followup.get("latest_text"), str)
                        or "current_speaker" not in self.followup):
                    raise ValueError("context_shape_unknown")
                if self.followup.get("member_own_memories") != self.data.get(self.memory_key):
                    raise ValueError("context_refs_mismatch")
        else:
            raise ValueError("context_shape_unknown")
        _aligned(self.data.get("available_expressions"), refs["assets"], ("id", "revision"))
        _aligned(self.data.get(self.memory_key), refs["memories"], ("id",))
        _aligned(self.data.get("lore"), refs["knowledge"], ("doc_id", "chunk_id"))
        _aligned(self.data.get('requested_history',[]),refs.get('history_lookup',{}).get('references',[]),('id',))
        group = _rows(self.data.get("recent_group_messages", []))
        if refs.get("group_context_count") != len(group):
            raise ValueError("context_refs_mismatch")
        # With no explicit latest_text in the group prompt, its last timeline
        # row is the only representation of the current message: retain it.
        if kind in {"group_decision", "followup"} and not group:
            raise ValueError("context_shape_unknown")
        dialogue = self.dialogue()
        _rows(dialogue)
        _turn_prefix_length(dialogue)
        if refs.get("dialogue_count") != len(dialogue):
            raise ValueError("context_refs_mismatch")
        if (self.followup is not None and "confirmed_direct_dialogue" in self.data
                and self.data["confirmed_direct_dialogue"] != dialogue):
            raise ValueError("context_refs_mismatch")

    def dialogue(self):
        if self.followup is not None:
            return self.followup.get("confirmed_dialogue", [])
        return self.data.get("confirmed_direct_dialogue", [])

    def render(self):
        if self.summary is not None:
            self.messages[1]["content"] = json.dumps(self.summary, ensure_ascii=False)
            return
        topic=self.data.get("group_topic")
        if topic:
            retained={r.get("message_id") for r in self.data.get("recent_group_messages",[]) if r.get("role")=="user"}
            if not all(r["message_id"] in retained for r in topic.get("sources",[])):
                self.data.pop("group_topic",None)
                self.refs["group_topic"]=dict(self.refs.get("group_topic",{}),status="context_trimmed",injected=False)
        lookup=self.data.get('knowledge_lookup')
        if lookup:
            retained={r['doc_id'] for r in self.data['lore']}
            lookup['retained_document_ids']=[n for n in lookup['document_ids'] if n in retained]
            if lookup['status']=='found' and not lookup['retained_document_ids']:lookup['status']='context_trimmed'
            self.refs['knowledge_lookup']=deepcopy(lookup)
        prefix = self.direct_prefix if self.kind == "reply" else self.group_prefix
        self.messages[1]["content"] = prefix + json.dumps(self.data, ensure_ascii=False)
        if self.kind == "reply":
            self.messages[2:-1] = self.history
        if self.followup is not None:
            self.messages[2]["content"] = self.followup_prefix + json.dumps(self.followup, ensure_ascii=False)

    def fixed_over_budget(self, budget):
        """A lower bound that can prove failure without serializing the JSON.

        Raw pinned text and the system message survive every legal reduction.
        JSON escaping and envelope overhead can only add bytes. Stop counting
        as soon as this lower bound exceeds the available input budget.
        """
        size = len(self.messages[0]["content"].encode("utf-8"))
        for row in self.data["lore"]:
            if size > budget:
                return True
            if row.get("always_include") is True and isinstance(row.get("text"), str):
                size += len(row["text"].encode("utf-8"))
        return size > budget

    def trim_summary(self, budget):
        """Choose the oldest fitting prefix without re-encoding every suffix.

        json.dumps(list) is exactly '[' + ', '.join(dumps(row)) + ']'
        with our fixed options. Accumulate its byte cost once, then render once.
        A too-large first row is still retained so the caller blocks it without
        accidentally acknowledging or silently skipping that source.
        """
        if len(self.summary) <= 1:
            return 0
        empty = [self.messages[0], {"role": "user", "content": "[]"}]
        used, kept = estimate_tokens(empty), 0
        for row in self.summary:
            cost = len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + (2 if kept else 0)
            if used + cost > budget:
                break
            used += cost
            kept += 1
        # Called only when the original request is over budget. At least one
        # whole source must go; unusual whitespace is not treated as evidence
        # of a successful history reduction.
        kept = min(len(self.summary) - 1, max(1, kept))
        removed = len(self.summary) - kept
        del self.summary[kept:]
        del self.refs["history"][kept:]
        self.render()
        return removed

    def remove(self, kind):
        """Remove one whole expendable unit and update exact audit references."""
        if self.summary is not None:
            if kind == "history" and len(self.summary) > 1:
                self.summary.pop()
                self.refs["history"].pop()
                return 1
            return 0
        if kind == "assets" and self.data["available_expressions"]:
            self.data["available_expressions"].pop()
            self.refs["assets"].pop()
            return 1
        if kind == 'history_lookup' and self.data.get('requested_history'):
            self.data['requested_history'].pop(0)
            lookup=self.refs['history_lookup'];lookup['references'].pop(0)
            if not lookup['references']:
                lookup['status']='context_trimmed';self.data['history_lookup']['status']='context_trimmed'
            return 1
        if kind == "history" and self.history:
            count = _turn_prefix_length(self.history)
            if count:
                del self.history[:count]
                del self.refs["history"][:count]
                return count
        if kind == "group_context":
            group = self.data.get("recent_group_messages", [])
            if len(group) > 1:
                group.pop(0)
                self.refs["group_context_count"] = len(group)
                return 1
        if kind == "dialogue":
            dialogue = self.dialogue()
            count = _turn_prefix_length(dialogue)
            if count:
                del dialogue[:count]
                if self.followup is not None and "confirmed_direct_dialogue" in self.data:
                    self.data["confirmed_direct_dialogue"] = deepcopy(dialogue)
                self.refs["dialogue_count"] = len(dialogue)
                return count
        if kind == "memories" and self.data[self.memory_key]:
            self.data[self.memory_key].pop()
            if self.followup is not None:
                self.followup["member_own_memories"] = deepcopy(self.data[self.memory_key])
            self.refs["memories"].pop()
            return 1
        if kind == "knowledge" and self.data["lore"]:
            # Administrator-pinned whole documents are fixed input, regardless
            # of their factual category. Rebudgeting/retries cannot silently
            # turn a full-document choice back into retrieval or remove it.
            optional = [index for index, row in enumerate(self.data["lore"])
                        if row.get("always_include") is not True]
            if not optional:
                return 0
            # Among optional hits, preserve canon until other categories are
            # exhausted; drop the lowest-ranked (last) hit within either group.
            candidates = [index for index in optional
                          if self.data["lore"][index].get("category") != "canon"]
            index = candidates[-1] if candidates else optional[-1]
            self.data["lore"].pop(index)
            self.refs["knowledge"].pop(index)
            return 1
        return 0


def fit_context(messages, refs, *, kind, window_tokens=32768, output_tokens=1200,
                safety_tokens=1024, image_tokens=0, example_tokens=0):
    """Fit a copy of a known request, returning exact retained refs and counts.

    Callers must check ``blocked`` before calling a provider. For summary calls,
    only the retained oldest history prefix may be acknowledged as processed.
    Neither this function nor its result advances any database checkpoint.

    Images are attached by the caller after text fitting. ``image_tokens`` is a
    conservative bounded reservation, not the size of a base64 encoding. Text
    estimates and ``input_budget`` keep their original text-only meaning; total
    estimates separately include this reservation. The caller may reserve more
    candidate images than survive text trimming, but must never attach more.
    """
    if any(type(value) is not int or value < 0 for value in
           (window_tokens, output_tokens, safety_tokens, image_tokens, example_tokens)) or image_tokens > 1000000 or not isinstance(refs, dict):
        raise ValueError("context_budget_invalid")
    fitted, fitted_refs = deepcopy(messages), deepcopy(refs)
    before = estimate_tokens(fitted)
    total_budget = max(0, window_tokens - output_tokens - safety_tokens)
    budget = max(0, total_budget - image_tokens - example_tokens)
    removed = {key: 0 for key in REMOVED_KEYS}
    after = before
    if before > budget:
        try:
            context = _Context(fitted, fitted_refs, kind)
        except (ValueError, KeyError, TypeError, IndexError):
            context = None
        if context is not None:
            if context.summary is not None:
                removed["history"] = context.trim_summary(budget)
                after = estimate_tokens(fitted)
            else:
                # Large fixed documents may already make success impossible.
                # Keep the same fully reduced request/refs, but avoid encoding
                # those documents again for every optional item removed.
                fixed_over_budget = context.fixed_over_budget(budget)
                for category in REMOVED_KEYS:
                    while after > budget:
                        count = context.remove(category)
                        if not count:
                            break
                        removed[category] += count
                        if not fixed_over_budget:
                            context.render()
                            after = estimate_tokens(fitted)
                if fixed_over_budget and any(removed.values()):
                    context.render()
                    after = estimate_tokens(fitted)
    blocked = after > budget or image_tokens + example_tokens > total_budget
    metrics = {"window_tokens": window_tokens, "input_budget": budget,
               "output_tokens": output_tokens, "safety_tokens": safety_tokens,
               "image_tokens": image_tokens, "total_input_budget": total_budget,
               "example_tokens": example_tokens,
               "estimated_total_before": before + image_tokens + example_tokens,
               "estimated_total_after": after + image_tokens + example_tokens,
               "estimated_before": before, "estimated_after": after,
               "estimator": "utf8_upper_estimate", "removed": removed,
               "trimmed": any(removed.values()), "blocked": blocked}
    return {"messages": fitted, "refs": fitted_refs, "metrics": metrics, "blocked": blocked}
