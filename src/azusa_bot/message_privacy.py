"""Per-message opt-out, shared by transports, context and persistence.

Check original text before adding mentions, attachment captions or truncating.
This is a local discard rule, not a prompt that asks a model to ignore content.
"""


def hidden_message(text: str) -> bool:
    """Double opening parentheses opt out this message only (either width)."""
    return isinstance(text, str) and text.lstrip().startswith(("（（", "(("))


def hidden_segments(segments) -> bool:
    """Native @, reply and media segments are not part of the typed prefix."""
    if not isinstance(segments, list):
        return False
    text = "".join(s["data"]["text"] for s in segments
                   if isinstance(s, dict) and s.get("type") == "text"
                   and isinstance(s.get("data"), dict) and isinstance(s["data"].get("text"), str))
    return hidden_message(text)
