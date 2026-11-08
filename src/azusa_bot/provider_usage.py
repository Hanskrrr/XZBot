"""Request-local, numeric usage returned by the provider, never an estimate."""
from __future__ import annotations


USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens",
                "prompt_cache_hit_tokens", "prompt_cache_miss_tokens")


def safe_usage(value) -> dict[str, int]:
    """Drop unknown fields and invalid values without inventing missing counts."""
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in USAGE_FIELDS
            if type(value.get(key)) is int and 0 <= value[key] <= 1_000_000_000}


class CompletionResult(dict):
    """Parsed model JSON plus separate metadata from this HTTP response only.

    Metadata is not a JSON key, so model output cannot forge it or include it
    in subsequent prompts. Sharing a provider object cannot mix request usage.
    """
    __slots__ = ("usage",)

    def __init__(self, payload: dict, usage=None):
        super().__init__(payload)
        self.usage = safe_usage(usage)
