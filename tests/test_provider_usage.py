"""Record actual per-request usage without retaining raw provider envelopes."""
import asyncio
import json

import httpx
import pytest

from azusa_bot.core import Engine
from azusa_bot.provider_usage import CompletionResult, safe_usage
from azusa_bot.providers import CloudProviders, ProviderError
from azusa_bot.secrets import Credentials
from azusa_bot.settings import Settings, private_write, write_settings
from azusa_bot.trajectories import references


USAGE = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
         "prompt_cache_hit_tokens": 75, "prompt_cache_miss_tokens": 25}
MESSAGES = [{"role": "user", "content": "synthetic question"}]


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    provider = CloudProviders(Credentials(deepseek_key="synthetic-test-key"), "test-model", "test-voice", tmp_path)
    original = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr("azusa_bot.providers.httpx.AsyncClient",
                            lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))

    return provider, install


@pytest.fixture
def engine(tmp_path):
    private_write(tmp_path / "content/persona.md", "# 合成角色\n简短回答。")
    write_settings(tmp_path, Settings(provider_mode="cloud"))
    instance = Engine(tmp_path)
    yield instance
    instance.vision.clear(); instance.memory.close(); instance.groups.close()


def envelope(payload=None, *, usage=USAGE, finish="stop"):
    return {"id": "provider-id-not-for-storage", "system_fingerprint": "do-not-retain",
            "usage": usage, "choices": [{"finish_reason": finish,
                "message": {"content": json.dumps(payload or {"text": "合成回答", "mode": "text"})}}]}


async def call(engine, provider, *, kind="reply"):
    return await engine._complete_traced(provider, MESSAGES, engine.settings(), kind=kind,
        trigger="automatic" if kind == "summary" else "direct", scope="dm:synthetic", user="synthetic",
        refs=references())


@pytest.mark.parametrize("value, expected", [
    (None, {}),
    ([], {}),
    ({"prompt_tokens": True, "completion_tokens": -1, "total_tokens": "100"}, {}),
    ({"prompt_tokens": 1.5, "total_tokens": 1_000_000_001}, {}),
    ({"prompt_tokens": 0, "completion_tokens": 20, "secret": "do-not-retain"},
     {"prompt_tokens": 0, "completion_tokens": 20}),
])
def test_usage_is_bounded_numeric_allowlist_without_coercion(value, expected):
    assert safe_usage(value) == expected


@pytest.mark.asyncio
async def test_provider_usage_is_not_forgeable_by_model_json_and_not_serialized_as_output(cloud):
    provider, install = cloud
    payload = {"text": "合成回答", "mode": "text", "usage": {"total_tokens": 999999}}
    install(lambda _: httpx.Response(200, json=envelope(payload, usage=dict(USAGE, secret="do-not-retain"))))
    answer = await provider.complete(MESSAGES)
    assert isinstance(answer, CompletionResult)
    assert answer.usage == USAGE
    assert dict(answer) == payload
    assert "prompt_cache_hit_tokens" not in json.dumps(answer)
    assert "do-not-retain" not in repr(answer.usage)


@pytest.mark.asyncio
async def test_shared_provider_concurrency_keeps_response_usage_per_request(cloud):
    provider, install = cloud
    first_entered = asyncio.Event()
    release_first = asyncio.Event()

    async def response(request):
        content = json.loads(request.content)["messages"][0]["content"]
        if content == "slow":
            first_entered.set()
            await release_first.wait()
            count = 10
        else:
            release_first.set()
            count = 20
        return httpx.Response(200, json=envelope(usage={"total_tokens": count}))

    install(response)
    slow = asyncio.create_task(provider.complete([{"role": "user", "content": "slow"}]))
    await asyncio.wait_for(first_entered.wait(), 1)
    fast = await provider.complete([{"role": "user", "content": "fast"}])
    assert fast.usage == {"total_tokens": 20}
    assert (await slow).usage == {"total_tokens": 10}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["reply", "summary"])
async def test_usage_survives_finalization_and_is_returned_in_list_and_detail(engine, cloud, kind):
    provider, install = cloud
    install(lambda _: httpx.Response(200, json=envelope()))
    answer = await call(engine, provider, kind=kind)
    assert type(answer) is dict and "usage" not in answer
    trace = engine.trajectories.list()["items"][0]
    if kind == "summary":
        engine.trajectories.finish(trace["id"], "no_facts", result={"processed": 12, "saved": 0,
            "usage": {"total_tokens": 999999}})  # Model/extractor result cannot forge counts.
    trace = engine.trajectories.list()["items"][0]
    assert trace["usage"] == USAGE
    assert engine.trajectories.get(trace["id"], engine.content_root)["usage"] == USAGE
    stored = engine.memory._db.execute("SELECT refs,result FROM trajectories").fetchone()
    assert "provider-id-not-for-storage" not in str(tuple(stored))
    assert "do-not-retain" not in str(tuple(stored))
    assert "合成回答" not in str(tuple(stored))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["length", "invalid_json", "empty_reply"])
async def test_returned_usage_is_preserved_even_when_generated_answer_is_rejected(engine, cloud, failure):
    provider, install = cloud
    response = envelope(finish="length" if failure == "length" else "stop")
    if failure == "invalid_json":
        response["choices"][0]["message"]["content"] = "not JSON"
    if failure == "empty_reply":
        response["choices"][0]["message"]["content"] = '{}'
    install(lambda _: httpx.Response(200, json=response))
    with pytest.raises(ProviderError):
        await call(engine, provider)
    trace = engine.trajectories.list()["items"][0]
    assert trace["status"] == "failed" and trace["usage"] == USAGE


@pytest.mark.asyncio
async def test_absent_usage_remains_unknown_and_fake_dict_cannot_inject_usage(engine, cloud):
    provider, install = cloud
    install(lambda _: httpx.Response(200, json=envelope(usage=None)))
    await call(engine, provider)
    assert engine.trajectories.list()["items"][0]["usage"] is None

    class ForgedProvider:
        async def complete(self, messages, **kwargs):
            return {"text": "另一个回答", "usage": USAGE, "_usage": USAGE}

    await call(engine, ForgedProvider())
    assert engine.trajectories.list()["items"][0]["usage"] is None


@pytest.mark.asyncio
async def test_usage_never_recreates_deleted_trace(engine, cloud):
    provider, install = cloud

    def response(_):
        trace = engine.trajectories.list()["items"][0]
        assert engine.trajectories.delete(trace["id"])
        return httpx.Response(200, json=envelope())

    install(response)
    await call(engine, provider)
    assert engine.trajectories.list()["items"] == []
