from __future__ import annotations

import json
import io
import re
import wave
from pathlib import Path
import uuid

import httpx

from .secrets import Credentials
from .provider_usage import CompletionResult, safe_usage
from .runtime_layout import runtime_layout


class ProviderError(RuntimeError):
    """Only fixed, safe codes may cross provider boundary."""

    def __init__(self, code, *, usage=None):
        super().__init__(code)
        self.usage = safe_usage(usage)


def _context_rejected(response: httpx.Response) -> bool:
    """Recognize explicit input overflow without retaining upstream error text.

    A 400/413/422 alone is NOT a context error. Only structured standard codes
    or narrow, known provider messages qualify. Gate on status first so a
    rate-limit/overload response can never trigger a compression retry.
    """
    if response.status_code not in {400, 413, 422} or len(response.content) > 65536:
        return False
    try:
        body = response.json()
    except (ValueError, TypeError):
        return False
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return False
    if any(error.get(field) == "context_length_exceeded" for field in ("code", "type")):
        return True
    message = error.get("message")
    if not isinstance(message, str) or len(message) > 8192:
        return False
    # Gemini may expose only numeric code 400 / INVALID_ARGUMENT. Do not treat
    # all INVALID_ARGUMENTs or max_tokens validation errors as input overflow.
    patterns = (
        r"this model['’]s maximum context length is [0-9,]+ tokens[.\s;:].*(?:however|requested|your messages)",
        r"(?:the )?input token count \([0-9,]+\) exceeds the maximum number of tokens allowed \([0-9,]+\)",
        r"(?:the )?input token count exceeds the maximum number of tokens allowed for this model",
        r"unable to submit request because the input token count is [0-9,]+ but (?:the )?model only supports up to [0-9,]+",
    )
    return any(re.match(pattern, message.strip(), flags=re.IGNORECASE | re.DOTALL) for pattern in patterns)


class CloudProviders:
    def __init__(self, credentials: Credentials, model: str, fish_model: str, media_dir: Path,
                 *, protocol: str = "deepseek", base_url: str = "https://api.deepseek.com", runtime_root: Path | None = None):
        self.credentials = credentials
        self.model = model
        self.fish_model = credentials.fish_model or fish_model
        self.media_dir = media_dir
        self.protocol = protocol
        self.base_url = ("https://api.deepseek.com" if protocol == "deepseek" else
                         (runtime_layout(runtime_root).gemini_url if runtime_root is not None else
                          "http://127.0.0.1:6201/openai/v1") if protocol == "gemini_balance" else base_url.rstrip("/"))

    async def complete(self, messages: list[dict], *, summary: bool = False) -> dict:
        prefix = "gemini_balance" if self.protocol == "gemini_balance" else "deepseek"
        key = (self.credentials.deepseek_key if self.protocol == "deepseek" else
               self.credentials.gemini_balance_token if self.protocol == "gemini_balance" else self.credentials.compatible_key)
        if not key:
            raise ProviderError(prefix + "_not_configured")
        if not self.model:
            raise ProviderError("model_not_configured")
        body = {"model": self.model, "messages": messages, "stream": False,
                "max_tokens": 700 if summary else 1200, "response_format": {"type": "json_object"}}
        if self.protocol == "deepseek":
            body["thinking"] = {"type": "disabled"}
        usage = {}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(70, connect=10), follow_redirects=False,
                                        trust_env=self.protocol != "gemini_balance") as client:
                r = await client.post(self.base_url + "/chat/completions",
                    headers={"Authorization": "Bearer " + key}, json=body)
                if r.status_code != 200:
                    if _context_rejected(r):
                        raise ProviderError("context_length_exceeded")
                    raise ProviderError(prefix + "_http_" + str(r.status_code))
                try:
                    data = r.json()
                except ValueError:
                    raise ProviderError(prefix + "_response_not_json") from None
                usage = safe_usage(data.get("usage")) if isinstance(data, dict) else {}
                if (not isinstance(data, dict) or not isinstance(data.get("choices"), list)
                    or not data["choices"] or not isinstance(data["choices"][0], dict)):
                    raise ProviderError(prefix + "_response_shape_invalid")
                choice = data["choices"][0]
                if choice.get("finish_reason") == "length":
                    # This can mean output budget OR total context exhaustion.
                    # Never parse or send a possibly truncated JSON document.
                    raise ProviderError("generation_length_limit")
                if choice.get("finish_reason") != "stop":
                    raise ProviderError(prefix + "_incomplete")
                message = choice.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("content"), str):
                    raise ProviderError(prefix + "_response_shape_invalid")
                if not message["content"].strip():
                    raise ProviderError(prefix + "_empty_content")
                try:
                    result = json.loads(message["content"])
                except ValueError:
                    raise ProviderError(prefix + "_output_not_json") from None
                if not isinstance(result, dict):
                    raise ProviderError(prefix + "_output_not_object")
                return CompletionResult(result, usage)
        except ProviderError as exc:
            exc.usage = usage
            raise
        except httpx.TimeoutException:
            raise ProviderError(prefix + "_timeout", usage=usage) from None
        except httpx.HTTPError:
            raise ProviderError(prefix + "_network_error", usage=usage) from None

    async def synthesize(self, text: str) -> Path:
        c = self.credentials
        if not c.fish_key or not c.fish_reference:
            raise ProviderError("fish_not_configured")
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(70, connect=10), follow_redirects=False) as client:
                async with client.stream("POST", "https://api.fish.audio/v1/tts",
                    headers={"Authorization": "Bearer " + c.fish_key, "model": self.fish_model},
                    json={"text": text, "reference_id": c.fish_reference, "format": "wav"}) as r:
                    if r.status_code != 200:
                        raise ProviderError("fish_http_" + str(r.status_code))
                    data = bytearray()
                    async for chunk in r.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > 10 * 1024 * 1024:
                            raise ProviderError("fish_audio_too_large")
                    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
                        raise ProviderError("fish_invalid_wav")
                    # Streaming WAVs may use 0xffffffff sizes. Rebuild a bounded
                    # PCM header so players report the real duration, not hours.
                    try:
                        with wave.open(io.BytesIO(data), "rb") as wav:
                            params = wav.getparams()
                            if params.nchannels not in {1, 2} or params.sampwidth != 2 or not 8000 <= params.framerate <= 48000:
                                raise ValueError()
                            pcm = wav.readframes(10 * 1024 * 1024)
                        if not pcm or len(pcm) % (params.nchannels * params.sampwidth):
                            raise ValueError()
                        normalized = io.BytesIO()
                        with wave.open(normalized, "wb") as wav:
                            wav.setparams(params)
                            wav.writeframes(pcm)
                        data = normalized.getvalue()
                    except (wave.Error, EOFError, ValueError, OverflowError):
                        raise ProviderError("fish_invalid_wav") from None
            self.media_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.media_dir / (uuid.uuid4().hex + ".wav")
            with path.open("xb") as f:
                f.write(data)
            path.chmod(0o600)
            return path.resolve()
        except ProviderError:
            raise
        except (httpx.HTTPError, OSError):
            raise ProviderError("fish_network_or_storage") from None


class MockProviders:
    async def complete(self, messages: list[dict], *, summary: bool = False) -> dict:
        if summary:
            return {"memories": []}
        query = messages[-1]["content"]
        if isinstance(query,list):
            query = " ".join(block.get("text","") for block in query if block.get("type")=="text")
        try:
            anchor = json.loads(query.split("\n",1)[1]).get("current_message")
            if isinstance(anchor,dict) and isinstance(anchor.get("text"),str):query=anchor["text"]
        except (ValueError,IndexError,AttributeError):
            pass
        result = {"text": "这是离线演练。已按当前群和成员检索记忆；连接云端后会由角色自然回答。你说：" + query[:100],
                "mode": "text"}
        if '"votes"' in messages[0]["content"]:
            result.update(votes={key:.9 for key in ("relevance","understanding","contribution","timing")},continuation=True)
        return result

    async def synthesize(self, text: str) -> Path:
        raise ProviderError("mock_no_audio")
