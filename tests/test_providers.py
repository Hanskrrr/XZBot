"""Exercise real cloud client boundaries through an in-memory HTTP transport."""

import io
import json
from pathlib import Path
import wave

import httpx
import pytest

from azusa_bot.providers import CloudProviders, ProviderError
from azusa_bot.secrets import Credentials


SECRET = "test-only-secret-never-a-real-key"
MESSAGES = [{"role": "user", "content": "你好"}]


@pytest.fixture
def cloud(tmp_path):
    credentials = Credentials(deepseek_key=SECRET, fish_key=SECRET,
                              fish_reference="test-reference", fish_model="test-model")
    return CloudProviders(credentials, "test-deepseek-model", "ignored-model", tmp_path / "media")


@pytest.fixture
def transport(monkeypatch):
    original = httpx.AsyncClient
    options = []
    requests = []

    def install(handler):
        def checked(request):
            requests.append(request)
            return handler(request)

        def factory(**kwargs):
            options.append(kwargs)
            return original(transport=httpx.MockTransport(checked), **kwargs)

        monkeypatch.setattr("azusa_bot.providers.httpx.AsyncClient", factory)
        return requests, options

    return install


def completion(payload, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"content": json.dumps(payload)}}]}


def valid_wav():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(b"\0\0" * 240)
    return stream.getvalue()


@pytest.mark.asyncio
async def test_deepseek_auth_model_and_json_success_do_not_expose_credentials(cloud, transport, caplog):
    def handler(request):
        assert str(request.url) == "https://api.deepseek.com/chat/completions"
        assert request.headers["Authorization"] == "Bearer " + SECRET
        body = json.loads(request.content)
        assert body["model"] == "test-deepseek-model"
        assert body["response_format"] == {"type": "json_object"}
        assert body["stream"] is False and body["max_tokens"] == 3200
        return httpx.Response(200, json=completion({"memories": []}))

    requests, options = transport(handler)
    result = await cloud.complete(MESSAGES, summary=True)
    assert result == {"memories": []}
    assert len(requests) == 1 and options[0]["follow_redirects"] is False
    assert SECRET not in repr(result) and SECRET not in caplog.text
    assert SECRET not in repr(cloud.credentials)


@pytest.mark.parametrize("content", [
    '\ufeff{"text":"你好"}',
    '\ufeff \n```JSON\r\n{"text":"你好"}\r\n```\n',
    '```\n{"text":"你好"}\n```',
])
async def test_deepseek_accepts_complete_json_with_a_bom_or_single_fence(cloud, transport, content):
    response = completion({})
    response["choices"][0]["message"]["content"] = content
    response["usage"] = {"completion_tokens": 12}
    requests, _ = transport(lambda request: httpx.Response(200, json=response))

    answer = await cloud.complete(MESSAGES)

    assert answer == {"text": "你好"}
    assert answer.usage == {"completion_tokens": 12}
    assert len(requests) == 1


@pytest.mark.parametrize(("content", "finish", "code"), [
    ('说明\n```json\n{"text":"你好"}\n```', "stop", "deepseek_output_not_json"),
    ('```json\n{"text":"你好"}\n```\n{}', "stop", "deepseek_output_not_json"),
    ('```json\n{"text":"你好"\n```', "stop", "deepseek_output_not_json"),
    ('```json\n{"text":"你好"}\n```', "length", "generation_length_limit"),
])
async def test_deepseek_rejects_ambiguous_or_truncated_fenced_output(cloud, transport, content, finish, code):
    response = completion({}, finish=finish)
    response["choices"][0]["message"]["content"] = content
    transport(lambda request: httpx.Response(200, json=response))

    with pytest.raises(ProviderError) as error:
        await cloud.complete(MESSAGES)
    assert str(error.value) == code


@pytest.mark.asyncio
async def test_redirects_never_forward_provider_authorization(cloud, transport, caplog):
    requests, options = transport(lambda request: httpx.Response(
        302, headers={"Location": "https://untrusted.example/steal"}, text=SECRET))
    for action, expected in ((cloud.complete(MESSAGES), "deepseek_http_302"),
                              (cloud.synthesize("你好"), "fish_http_302")):
        with pytest.raises(ProviderError) as error:
            await action
        assert str(error.value) == expected
        assert SECRET not in str(error.value)
    assert [r.url.host for r in requests] == ["api.deepseek.com", "api.fish.audio"]
    assert all(o["follow_redirects"] is False for o in options)
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_invalid_or_truncated_deepseek_responses_have_fixed_safe_errors(cloud, transport):
    invalid = [
        b"not-json-" + SECRET.encode(),
        [],
        {"choices": []},
        {"choices": [None]},
        {"choices": [{"finish_reason": "stop", "message": None}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "invalid-" + SECRET}}]},
        completion(["not an object"]),
    ]
    expected = ['response_not_json', 'response_shape_invalid', 'response_shape_invalid',
                'response_shape_invalid', 'response_shape_invalid', 'output_not_json', 'output_not_object']
    for data, suffix in zip(invalid, expected):
        transport(lambda request, body=data: httpx.Response(200, content=body) if isinstance(body, bytes)
                  else httpx.Response(200, json=body))
        with pytest.raises(ProviderError) as error:
            await cloud.complete(MESSAGES)
        assert str(error.value) == "deepseek_" + suffix
        assert SECRET not in str(error.value)
    transport(lambda request: httpx.Response(200, json=completion({"text": "truncated"}, finish="length")))
    with pytest.raises(ProviderError, match="^generation_length_limit$"):
        await cloud.complete(MESSAGES)


@pytest.mark.asyncio
async def test_http_error_and_network_exception_content_are_not_propagated(cloud, transport, caplog):
    transport(lambda request: httpx.Response(401, text="server echoed " + SECRET))
    with pytest.raises(ProviderError, match="^deepseek_http_401$"):
        await cloud.complete(MESSAGES)
    with pytest.raises(ProviderError, match="^fish_http_401$"):
        await cloud.synthesize("你好")

    def timeout(request):
        raise httpx.ReadTimeout("network diagnostics containing " + SECRET, request=request)

    transport(timeout)
    for action, expected in ((cloud.complete(MESSAGES), "deepseek_timeout"),
                              (cloud.synthesize("你好"), "fish_network_or_storage")):
        with pytest.raises(ProviderError) as error:
            await action
        assert str(error.value) == expected and SECRET not in str(error.value)
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_fish_wav_is_private_file_and_reference_is_only_in_request(cloud, transport, caplog):
    audio = valid_wav()

    def handler(request):
        assert str(request.url) == "https://api.fish.audio/v1/tts"
        assert request.headers["Authorization"] == "Bearer " + SECRET
        assert request.headers["model"] == "test-model"
        assert json.loads(request.content) == {"text": "你好", "reference_id": "test-reference", "format": "wav"}
        return httpx.Response(200, content=audio, headers={"Content-Type": "audio/wav"})

    transport(handler)
    result = await cloud.synthesize("你好")
    assert result.is_relative_to(cloud.media_dir)
    assert result.suffix == ".wav" and len(result.stem) == 32
    assert result.stat().st_mode & 0o777 == 0o600
    with wave.open(str(result)) as recorded:
        assert recorded.getnchannels() == 1 and recorded.getframerate() == 24000
        assert recorded.getnframes() == 240
    assert SECRET not in str(result) and SECRET not in caplog.text


class OversizeAudio(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield valid_wav()
        for _ in range(11):
            yield b"\0" * (1024 * 1024)


@pytest.mark.asyncio
async def test_invalid_and_oversize_audio_create_no_files(cloud, transport):
    for audio in (b"", b"not a wav " + SECRET.encode(), b"RIFF" + b"\0" * 39):
        transport(lambda request, data=audio: httpx.Response(200, content=data))
        with pytest.raises(ProviderError, match="^fish_invalid_wav$"):
            await cloud.synthesize("你好")
    transport(lambda request: httpx.Response(200, stream=OversizeAudio()))
    with pytest.raises(ProviderError, match="^fish_audio_too_large$"):
        await cloud.synthesize("你好")
    assert not cloud.media_dir.exists() or list(cloud.media_dir.iterdir()) == []


@pytest.mark.asyncio
async def test_missing_credentials_never_open_http_client(tmp_path, transport):
    requests, _ = transport(lambda request: pytest.fail("HTTP must not be called"))
    provider = CloudProviders(Credentials(), "model", "voice", tmp_path)
    with pytest.raises(ProviderError, match="^deepseek_not_configured$"):
        await provider.complete(MESSAGES)
    with pytest.raises(ProviderError, match="^fish_not_configured$"):
        await provider.synthesize("你好")
    assert requests == []
