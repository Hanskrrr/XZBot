"""Input overflow is distinct from HTTP failures and partial JSON responses."""
import json

import httpx
import pytest

from azusa_bot.diagnostics import explanation, safe_code
from azusa_bot.providers import CloudProviders, ProviderError
from azusa_bot.secrets import Credentials

SECRET = "synthetic-private-error-payload"
MESSAGES = [{"role": "user", "content": "合成问题"}]


@pytest.fixture
def invoke(tmp_path, monkeypatch):
    original = httpx.AsyncClient

    async def call(response, *, protocol="deepseek"):
        requests = []

        def handler(request):
            requests.append(request)
            return response

        monkeypatch.setattr("azusa_bot.providers.httpx.AsyncClient",
                            lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
        provider = CloudProviders(Credentials(deepseek_key="synthetic-key", compatible_key="synthetic-key",
                    gemini_balance_token="synthetic-key"), "synthetic-model", "unused", tmp_path / "media",
                    protocol=protocol, base_url="https://synthetic.example/v1")
        with pytest.raises(ProviderError) as found:
            await provider.complete(MESSAGES)
        assert len(requests) == 1  # Retrying belongs to the budgeted caller.
        assert SECRET not in str(found.value)
        return str(found.value)

    return call


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["deepseek", "openai_compatible", "gemini_balance"])
@pytest.mark.parametrize("field", ["code", "type"])
async def test_explicit_standard_code_cross_provider(invoke, protocol, field, caplog):
    response = httpx.Response(400, json={"error": {field: "context_length_exceeded", "message": SECRET}})
    assert await invoke(response, protocol=protocol) == "context_length_exceeded"
    assert SECRET not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 413, 422])
@pytest.mark.parametrize("message", [
    "This model's maximum context length is 8192 tokens. However, you requested 9000 tokens.",
    "The input token count (9,000) exceeds the maximum number of tokens allowed (8,192).",
    "Input token count exceeds the maximum number of tokens allowed for this model. Please adjust your prompt.",
    "Unable to submit request because the input token count is 9000 but model only supports up to 8192",
])
async def test_narrow_known_upstream_message_without_standard_code(invoke, status, message):
    response = httpx.Response(status, json={"error": {"code": status, "status": "INVALID_ARGUMENT",
                                                     "message": message + " " + SECRET}})
    assert await invoke(response) == "context_length_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 402, 429, 500, 503])
async def test_limit_or_service_error_never_becomes_compression_retry(invoke, status):
    response = httpx.Response(status, json={"error": {"code": "context_length_exceeded",
        "message": "The input token count (9000) exceeds the maximum number of tokens allowed (8192)."}})
    assert await invoke(response) == f"deepseek_http_{status}"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    {"code": "invalid_request_error", "message": "Unrecognized request argument: thinking"},
    {"code": 400, "status": "INVALID_ARGUMENT", "message": "Invalid model name"},
    {"code": 400, "message": "max_tokens must be less than the maximum number of output tokens allowed"},
    {"message": "token limit reached"},
    {"message": "This parameter controls the maximum context length, but its format is invalid"},
    {"message": "context_length_exceeded"},
    {"message": ["context_length_exceeded"]},
    {"code": ["context_length_exceeded"], "type": {}},
    {"message": "The input token count (9000) exceeds the maximum number of tokens allowed (8192)." + "x" * 8192},
])
async def test_generic_invalid_parameters_and_malformed_errors_are_not_overflow(invoke, error):
    assert await invoke(httpx.Response(400, json={"error": error})) == "deepseek_http_400"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    b"context_length_exceeded", b"<html>request too large</html>",
    json.dumps({"error": "context_length_exceeded"}).encode(),
    json.dumps([{"error": {"code": "context_length_exceeded"}}]).encode(),
    json.dumps({"error": {"code": "context_length_exceeded", "message": "x" * 65536}}).encode(),
])
async def test_bounded_error_body_never_echoes_or_guesses(invoke, body):
    assert await invoke(httpx.Response(413, content=body)) == "deepseek_http_413"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["deepseek", "openai_compatible", "gemini_balance"])
async def test_length_stop_never_parses_partial_json_or_retries_input(invoke, protocol):
    response = httpx.Response(200, json={"choices": [{"finish_reason": "length",
        "message": {"content": '{"text":"' + SECRET}}]})
    assert await invoke(response, protocol=protocol) == "generation_length_limit"


@pytest.mark.asyncio
async def test_other_incomplete_reason_preserves_previous_behavior(invoke):
    response = httpx.Response(200, json={"choices": [{"finish_reason": "content_filter",
                                                     "message": {"content": SECRET}}]})
    assert await invoke(response) == "deepseek_incomplete"


def test_diagnostic_codes_are_fixed_and_distinct():
    assert safe_code("context_length_exceeded") == "context_length_exceeded"
    assert safe_code("generation_length_limit") == "generation_length_limit"
    assert safe_code("context_length_exceeded: " + SECRET) == "provider_unknown_error"
    assert "过长上下文" in explanation("context_length_exceeded")
    assert "不完整" in explanation("generation_length_limit")
    assert "限流" in explanation("deepseek_http_429")
    assert "繁忙" in explanation("deepseek_http_503")
    for code in ("context_budget_exceeded", "privacy_context_changed", "context_retry_budget"):
        assert safe_code(code) == code
    assert "没有擅自截断核心人设" in explanation("context_budget_exceeded")
    assert "取消" in explanation("privacy_context_changed")
    assert "本次未再请求模型" in explanation("context_retry_budget")
