"""Local GUI memory controls against the actual FastAPI application."""
import json

from fastapi.testclient import TestClient
import pytest

from azusa_bot.app import create_app
from azusa_bot.settings import Owner, Settings, write_settings


class Extractor:
    def __init__(self):
        self.calls = 0

    async def complete(self, messages, *, summary=False):
        self.calls += 1
        assert summary
        rows = json.loads(messages[-1]["content"])
        return {"memories": [{"text": "用户自述喜欢猫", "source_ids": [rows[0]["id"]]}]}


@pytest.fixture
def local(tmp_path):
    (tmp_path / "content").mkdir()
    (tmp_path / "content/persona.md").write_text("# 合成角色\n用于无网络测试的人设。")
    (tmp_path / "ui").mkdir()
    (tmp_path / "ui/index.html").write_text("<title>Test</title>")
    write_settings(tmp_path, Settings(transport="onebot", onebot_self_id="10001",
        onebot_allowed_groups=["30001"], onebot_allowed_dm_users=["20001"],
        onebot_owners=[Owner(kind="dm", scope_id="20001", user_id="20001")], auto_summary_every=4))
    app = create_app(tmp_path)
    extractor = Extractor()
    app.state.engine.provider_override = extractor
    admin = {"Authorization": "Bearer " + (tmp_path / "data/admin.token").read_text()}
    with TestClient(app) as client:
        yield client, app.state.engine, admin, extractor
    app.state.engine.memory.close()


def test_controls_require_local_admin_and_bool_is_strict(local):
    client, _, admin, _ = local
    for path in ("/status", "/targets"):
        assert client.get("/admin/memory" + path).status_code == 401
    data = {"scope": "onebot:10001:dm:20001", "user": "20001"}
    assert client.put("/admin/memory/consent", json={**data, "enabled": True}).status_code == 401
    assert client.post("/admin/memory/summarize", json=data).status_code == 401
    response = client.put("/admin/memory/consent", headers=admin, json={**data, "enabled": "true"})
    assert response.status_code == 422


def test_targets_only_include_configured_scopes_and_observed_members(local):
    client, engine, admin, _ = local
    engine.groups.add("onebot:10001:group:30001", "20002", "正文不可出现在目标清单", "e1", display_name="合成成员")
    engine.groups.add("onebot:10001:group:30002", "20003", "其他群", "e2", display_name="隐藏成员")
    result = client.get("/admin/memory/targets", headers=admin)
    data = result.json()
    assert data["profile_id"] == "default"
    assert {row["scope_id"] for row in data["scopes"]} == {"20001", "30001"}
    dm, group = data["scopes"]
    assert dm["members"][0]["owner"] is True
    assert group["members"][0]["display_name"] == "合成成员"
    assert group["members"][0]["enabled"] is False
    assert "正文不可" not in result.text and "隐藏成员" not in result.text


def test_manual_extraction_reports_actual_result_and_consent_disable_erases(local):
    client, engine, admin, provider = local
    member = {"scope": "onebot:10001:dm:20001", "user": "20001"}
    response = client.post("/admin/memory/summarize", headers=admin, json=member)
    assert response.json()["status"] == "disabled" and provider.calls == 0
    assert client.put("/admin/memory/consent", headers=admin, json={**member, "enabled": True}).status_code == 200
    engine.memory.add_history(member["scope"], member["user"], "user", "合成自述：喜欢猫", "only-one")
    before = client.get("/admin/memory/status", headers=admin).json()
    assert before["members"][0]["remaining_to_summary"] == 3
    assert before["members"][0]["environment"] == "qq"
    assert "喜欢猫" not in json.dumps(before, ensure_ascii=False)
    result = client.post("/admin/memory/summarize", headers=admin, json=member).json()
    assert result["status"] == "saved" and result["saved"] == 1 and provider.calls == 1
    assert client.post("/admin/memory/summarize", headers=admin, json=member).json()["status"] == "empty"
    engine.memory.add_memory(member["scope"], member["user"], "显式保存的偏好")
    client.put("/admin/memory/consent", headers=admin, json={**member, "enabled": False})
    after = client.get("/admin/memory/status", headers=admin).json()
    assert after["stats"]["history"] == 0 and after["stats"]["memories"] == 1
    assert after["members"][0]["inferred"] == 0 and not after["members"][0]["enabled"]


def test_nonempty_invalid_extraction_is_a_failure_not_no_facts(local):
    client, engine, admin, _ = local

    class InvalidExtractor:
        async def complete(self, messages, *, summary=False):
            return {"memories": [{"text": "不存在的来源不能当证据", "source_ids": [999999]}]}

    engine.provider_override = InvalidExtractor()
    member = {"scope": "onebot:10001:dm:20001", "user": "20001"}
    engine.memory.set_consent(member["scope"], member["user"], True)
    engine.memory.add_history(member["scope"], member["user"], "user", "合成用户消息", "source")
    result = client.post("/admin/memory/summarize", headers=admin, json=member).json()
    assert result["status"] == "summary_invalid" and result["saved"] == 0
    row = client.get("/admin/memory/status", headers=admin).json()["members"][0]
    assert row["pending"] == 1
    assert row["summary"]["status"] == "summary_invalid"
    assert row["summary"]["retry_after_seconds"] > 0
