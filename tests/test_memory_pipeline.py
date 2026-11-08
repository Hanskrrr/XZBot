"""Extraction queue invariants, using synthetic statements and no cloud calls."""
from __future__ import annotations

import sqlite3

import pytest

from azusa_bot.memory import MemoryStore


SCOPE = "onebot:10001:group:30001"
USER = "20001"


@pytest.fixture
def memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.sqlite3")
    yield store
    store.close()


def observed(memory, count=4, user=USER, scope=SCOPE):
    memory.set_consent(scope, user, True)
    return [memory.add_history(scope, user, "user", f"合成偏好 {n}：喜欢猫", f"test-{n}") for n in range(count)]


def test_status_explains_empty_and_opt_in_without_exposing_content(memory):
    empty = memory.collection_status(4)
    assert empty["stats"]["consenting_members"] == 0
    assert empty["members"] == []
    observed(memory, 2)
    row = memory.collection_status(4)["members"][0]
    assert row["enabled"] and row["pending"] == 2
    assert row["remaining_to_summary"] == 2
    assert "合成偏好" not in str(row)
    assert row["summary"]["status"] == "not_started"
    assert memory.begin_summary(SCOPE, USER, threshold=4)["status"] == "below_threshold"


def test_claim_dedup_and_empty_result_advances_cursor(memory):
    observed(memory)
    first = memory.begin_summary(SCOPE, USER, threshold=4)
    assert first["status"] == "ready"
    assert memory.begin_summary(SCOPE, USER, force=True)["status"] == "retry_wait"
    result = memory.finish_summary(SCOPE, USER, first["batch"], [])
    assert result == {"status": "no_facts", "saved": 0, "processed": 4}
    assert memory.begin_summary(SCOPE, USER, force=True)["status"] == "empty"
    assert memory.finish_summary(SCOPE, USER, first["batch"], [])["status"] == "superseded"
    row = memory.collection_status()["members"][0]
    assert row["pending"] == 0 and row["summary"]["last_success_at"]


def test_valid_fact_saved_with_original_evidence_new_messages_remain_pending(memory):
    refs = observed(memory)
    batch = memory.begin_summary(SCOPE, USER, threshold=4)["batch"]
    memory.add_history(SCOPE, USER, "user", "新的合成偏好：喜欢茶", "later")
    result = memory.finish_summary(SCOPE, USER, batch, [{"text": "用户自述喜欢猫", "source_ids": [refs[0]]}])
    assert result["saved"] == 1
    fact = memory.list_memories(SCOPE, USER)[0]
    assert fact["source"] == "inferred" and fact["source_ids"] == [refs[0]]
    assert memory.collection_status()["members"][0]["pending"] == 1


def test_cross_member_scope_assistant_and_boolean_evidence_rejected(memory):
    refs = observed(memory)
    foreign = observed(memory, 1, user="20002")[0]
    other_group = observed(memory, 1, scope="onebot:10001:group:30002")[0]
    assistant = memory.add_history(SCOPE, USER, "assistant", "模型输出不能证明个人事实", "assistant")
    batch = memory.begin_summary(SCOPE, USER, threshold=4)["batch"]
    with pytest.raises(ValueError, match="summary_invalid"):
        memory.finish_summary(SCOPE, USER, batch, [
            {"text": "跨成员", "source_ids": [foreign]},
            {"text": "跨群", "source_ids": [other_group]},
            {"text": "模型或布尔引用", "source_ids": [assistant, True, refs[0]]},
        ])
    assert memory.list_memories(SCOPE, USER) == []
    assert next(row for row in memory.collection_status()["members"] if row["scope"] == SCOPE and row["user"] == USER)["pending"] == 4


@pytest.mark.parametrize("operation", ["disable", "forget", "correct", "delete"])
def test_privacy_or_evidence_change_cancels_inflight_summary(memory, operation):
    refs = observed(memory)
    batch = memory.begin_summary(SCOPE, USER, threshold=4)["batch"]
    if operation == "disable":
        memory.set_consent(SCOPE, USER, False)
    elif operation == "forget":
        memory.forget_me(SCOPE, USER)
    elif operation == "correct":
        memory.update_history(refs[0], SCOPE, USER, "更正：之前那是朋友的喜好")
    else:
        memory.delete_history(refs[0], SCOPE, USER)
    result = memory.finish_summary(SCOPE, USER, batch, [{"text": "旧偏好", "source_ids": [refs[0]]}])
    assert result["status"] == "superseded"
    assert memory.list_memories(SCOPE, USER) == []


def test_corrected_old_history_can_be_processed_again(memory):
    refs = observed(memory)
    batch = memory.begin_summary(SCOPE, USER, threshold=4)["batch"]
    memory.finish_summary(SCOPE, USER, batch, [{"text": "旧偏好", "source_ids": [refs[0]]}])
    memory.update_history(refs[0], SCOPE, USER, "合成更正：现在喜欢茶")
    pending = memory.begin_summary(SCOPE, USER, force=True)
    assert pending["status"] == "ready"
    assert pending["batch"]["history"][0]["text"] == "合成更正：现在喜欢茶"
    assert memory.list_memories(SCOPE, USER) == []


def test_failed_claim_stays_pending_and_does_not_leak_error_text(memory):
    observed(memory)
    batch = memory.begin_summary(SCOPE, USER, threshold=4)["batch"]
    result = memory.fail_summary(SCOPE, USER, batch, "SECRET server response /private/data", retry_seconds=60)
    assert result["status"] == "provider_failure"
    status = memory.collection_status(4)
    row = status["members"][0]
    assert row["pending"] == 4 and row["summary"]["retry_after_seconds"] > 0
    assert "SECRET" not in str(status)
    assert memory.begin_summary(SCOPE, USER, force=True)["status"] == "retry_wait"


def test_stale_claim_recovers_after_process_restart(memory):
    observed(memory)
    batch = memory.begin_summary(SCOPE, USER, threshold=4)["batch"]
    with sqlite3.connect(memory.path) as connection:
        connection.execute("UPDATE summary_progress SET retry_at=1")
    replacement = memory.begin_summary(SCOPE, USER, threshold=4)
    assert replacement["status"] == "ready"
    assert replacement["batch"]["claim_id"] != batch["claim_id"]
    assert memory.finish_summary(SCOPE, USER, batch, [])["status"] == "superseded"


def test_legacy_migration_is_idempotent_and_cannot_overwrite_rewind(memory):
    refs = observed(memory)
    memory.import_summary_checkpoint(SCOPE, USER, refs[-1])
    assert memory.begin_summary(SCOPE, USER, force=True)["status"] == "empty"
    memory.update_history(refs[0], SCOPE, USER, "更正后等待处理")
    memory.import_summary_checkpoint(SCOPE, USER, refs[-1])
    assert memory.begin_summary(SCOPE, USER, force=True)["status"] == "ready"


def test_oldest_pending_window_is_processed_first(memory):
    refs = observed(memory, 140)
    batch = memory.begin_summary(SCOPE, USER, threshold=4)["batch"]
    assert len(batch["history"]) == 100
    assert batch["history"][0]["id"] == refs[0]
    memory.finish_summary(SCOPE, USER, batch, [])
    later = memory.begin_summary(SCOPE, USER, threshold=4)["batch"]
    assert [row["id"] for row in later["history"]] == refs[100:]


def test_separate_profile_files_never_share_checkpoint_or_memories(memory, tmp_path):
    observed(memory)
    other = MemoryStore(tmp_path / "other-profile.sqlite3")
    try:
        assert other.collection_status()["members"] == []
        assert other.begin_summary(SCOPE, USER, force=True)["status"] == "disabled"
        observed(other, 1)
        batch = memory.begin_summary(SCOPE, USER, threshold=4)["batch"]
        assert other.finish_summary(SCOPE, USER, batch, [])["status"] == "superseded"
    finally:
        other.close()


def test_background_queue_is_fair_and_respects_consent_threshold_and_backoff(memory):
    observed(memory, 4)
    observed(memory, 4, user="20002")
    observed(memory, 3, user="20003")
    observed(memory, 4, user="20004")
    memory.set_consent(SCOPE, "20004", False)
    assert [r["user"] for r in memory.due_summaries(4)] == [USER, "20002"]
    batch = memory.begin_summary(SCOPE, USER, threshold=4)["batch"]
    assert [r["user"] for r in memory.due_summaries(4)] == ["20002"]
    memory.fail_summary(SCOPE, USER, batch, "provider_unavailable")
    assert [r["user"] for r in memory.due_summaries(4)] == ["20002"]


def test_scope_filter_precedes_queue_limit_so_demo_backlog_cannot_starve_real_users(memory):
    for number in range(25):
        observed(memory, 4, user=f"synthetic-{number}", scope="demo:group:excluded")
    observed(memory, 4)
    assert all(row["scope"] != SCOPE for row in memory.due_summaries(4, limit=20))
    assert memory.due_summaries(4, limit=20, allowed_scopes=[SCOPE]) == [{"scope": SCOPE, "user": USER, "pending": 4}]
    assert memory.due_summaries(4, allowed_scopes=[]) == []
