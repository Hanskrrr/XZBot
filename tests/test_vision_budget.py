"""Image reservations and provenance use synthetic metadata, never image APIs."""
from copy import deepcopy
import json

import pytest

from azusa_bot.context_budget import DIRECT_PREFIX, estimate_tokens, fit_context
from azusa_bot.memory import MemoryStore
from azusa_bot.trajectories import TrajectoryStore, references, safe_image_references


def request(lore=()):
    data = {"trusted_identity": {"kind": "group", "user_id": "20001"},
            "available_expressions": [], "retrieved_memory": [], "lore": list(lore)}
    messages = [{"role": "system", "content": "完整角色与权限规则"},
                {"role": "user", "content": DIRECT_PREFIX + json.dumps(data, ensure_ascii=False)},
                {"role": "user", "content": "（用户发送了一张图片）"}]
    return messages, references(knowledge=list(lore), persona=messages[0]["content"])


def fixed_document(number):
    text = f"资料{number}的全文开始。" + "固定人设背景🙂" * 60 + f"资料{number}的全文结束。"
    return {"doc_id": f"full-{number}", "chunk_id": f"full-{number}:hash", "text": text,
            "title": f"资料{number}", "category": "reference", "sources": [],
            "start_char": 0, "end_char": len(text), "always_include": True}


def visual(number=1, **extra):
    return {"image_id": f"{number:032x}", "user_id": "20001", "event_id": "-123",
            "status": "ready", "error_code": "", "width": 800, "height": 600,
            "animated_first_frame": False, **extra}


@pytest.mark.parametrize("image_count", [1, 2, 4])
def test_images_reserve_input_space_without_counting_base64_as_text(image_count):
    messages, refs = request()
    text_tokens = estimate_tokens(messages)
    reserved = image_count * 2048
    window = text_tokens + reserved + 1200 + 1024
    result = fit_context(messages, refs, kind="reply", window_tokens=window, image_tokens=reserved)
    assert not result["blocked"]
    assert result["messages"] == messages
    assert result["metrics"]["estimated_after"] == text_tokens
    assert result["metrics"]["input_budget"] == text_tokens
    assert result["metrics"]["image_tokens"] == reserved
    assert result["metrics"]["estimated_total_after"] == result["metrics"]["total_input_budget"]
    too_small = fit_context(messages, refs, kind="reply", window_tokens=window - 1, image_tokens=reserved)
    assert too_small["blocked"]
    assert too_small["messages"] == messages


def test_image_reservation_never_evicts_full_backgrounds_to_fake_success():
    fixed = [fixed_document(1), fixed_document(2)]
    messages, refs = request(fixed)
    original = deepcopy((messages, refs))
    text_tokens = estimate_tokens(messages)
    result = fit_context(messages, refs, kind="reply", window_tokens=text_tokens + 1200 + 1024,
                         image_tokens=4096)
    assert result["blocked"]
    assert result["messages"] == messages and result["refs"] == refs
    assert result["metrics"]["removed"]["knowledge"] == 0
    assert (messages, refs) == original


def test_zero_image_reservation_preserves_all_original_budget_semantics():
    messages, refs = request()
    result = fit_context(messages, refs, kind="reply", window_tokens=8192)
    metrics = result["metrics"]
    assert metrics["image_tokens"] == 0
    assert metrics["input_budget"] == metrics["total_input_budget"] == 8192 - 1200 - 1024
    assert metrics["estimated_total_before"] == metrics["estimated_before"]
    assert metrics["estimated_total_after"] == metrics["estimated_after"]
    assert not result["blocked"]


@pytest.mark.parametrize("bad", [True, False, -1, 1.5, "2048", None, 1000001])
def test_image_reservation_requires_bounded_nonnegative_integer(bad):
    with pytest.raises(ValueError, match="context_budget_invalid"):
        fit_context(*request(), kind="reply", image_tokens=bad)


def test_images_alone_exceeding_window_block_even_without_text_space():
    result = fit_context(*request(), kind="reply", window_tokens=4096, image_tokens=8192)
    assert result["blocked"]
    assert result["metrics"]["input_budget"] == 0


def test_image_metadata_whitelist_drops_bytes_urls_and_freeform_errors():
    unsafe = visual(url="https://gchat.qpic.cn/image?credential=private",
                    data_url="data:image/png;base64,private-bytes", caption="private caption",
                    error_code="vision_do_not_persist_arbitrary_provider_text")
    result = safe_image_references([unsafe])[0]
    assert result["image_id"] == unsafe["image_id"]
    assert result["error_code"] == "vision_unavailable"
    assert not {"url", "data_url", "caption"} & result.keys()
    assert "private" not in json.dumps(result)
    assert safe_image_references([visual(status=[], error_code={})])[0]["status"] == "unavailable"
    assert safe_image_references([visual(image_id=12345678901234567890123456789012)]) == []
    assert safe_image_references(None) == []
    assert safe_image_references([{"image_id": "https://invalid.invalid/private"}]) == []


def test_untrusted_metadata_values_cannot_disguise_content_as_identity_or_dimensions():
    result = safe_image_references([visual(event_id="https://private.invalid/?secret=hidden",
                                           user_id="data:image/png;base64,secret", status="private text",
                                           width=True, height=9000, animated_first_frame="true")])[0]
    assert result["event_id"].startswith("sha256:") and result["user_id"].startswith("sha256:")
    assert result["status"] == "unavailable"
    assert "width" not in result and "height" not in result
    assert result["animated_first_frame"] is False
    assert "private" not in json.dumps(result) and "secret" not in json.dumps(result)


def test_trajectory_persists_only_visual_metadata_even_if_caller_supplies_cache_rows(tmp_path):
    store = MemoryStore(":memory:")
    try:
        trace = TrajectoryStore(store)
        meta = visual(data_url="data:image/png;base64,not-for-database", url="https://private.invalid/?key=hidden")
        refs = references(images=[meta])
        number = trace.begin("reply", "direct", "onebot:10001:group:30001", "20001", "deepseek", "deepseek-flash", refs)
        refs["images"] = [meta]  # A later update is sanitized again, not trusted.
        assert trace.set_context(number, refs)
        trace.called(number)
        trace.finish(number)
        detail = trace.get(number, tmp_path)
        assert detail["references"]["images"] == safe_image_references([meta])
        assert detail["counts"]["images"] == 1
        raw = store._db.execute("SELECT refs FROM trajectories WHERE id=?", (number,)).fetchone()[0]
        assert "base64" not in raw and "not-for-database" not in raw and "private.invalid" not in raw
        assert trace.delete(number)
        assert trace.get(number, tmp_path) is None
    finally:
        store.close()


def test_legacy_trajectory_without_image_metadata_remains_readable(tmp_path):
    store = MemoryStore(":memory:")
    try:
        trace = TrajectoryStore(store)
        refs = references()
        number = trace.begin("reply", "direct", "test", "member", "deepseek", "test", refs)
        refs.pop("images")
        with store._db:
            store._db.execute("UPDATE trajectories SET refs=? WHERE id=?", (json.dumps(refs), number))
        detail = trace.get(number, tmp_path)
        assert detail["references"]["images"] == [] and detail["counts"]["images"] == 0
    finally:
        store.close()


def test_forgetting_image_author_erases_cross_member_refs_only_in_same_group(tmp_path):
    store = MemoryStore(":memory:")
    try:
        trace = TrajectoryStore(store)
        refs = references(images=[visual(1, user_id="20001"), visual(2, user_id="20002")])
        here = trace.begin("followup", "followup", "group-1", "20002", "deepseek", "test", refs)
        elsewhere = trace.begin("reply", "direct", "group-2", "20002", "deepseek", "test", refs)
        assert trace.erase_image_sources("group-1", "20001") == 1
        assert [row["user_id"] for row in trace.get(here, tmp_path)["references"]["images"]] == ["20002"]
        assert len(trace.get(elsewhere, tmp_path)["references"]["images"]) == 2
        assert trace.erase_image_sources("group-1", "20001") == 0
        assert trace.exists(here) and trace.exists(elsewhere)
    finally:
        store.close()
