"""Visual attachment selection follows retained sources, never user JSON claims."""
from copy import deepcopy
import json

import pytest

from azusa_bot.context_budget import DIRECT_PREFIX, FOLLOWUP_PREFIX, GROUP_PREFIX, estimate_tokens, fit_context
from azusa_bot.trajectories import references, safe_image_references
from azusa_bot.vision_prompt import attach_images, retained_events, visual_sources
from azusa_bot.prompts import DEFAULTS


def group_message(rows):
    return {"role": "user", "content": GROUP_PREFIX + json.dumps({
        "available_expressions": [], "recent_group_messages": rows,
        "latest_speaker_id": "20001", "latest_speaker_own_memories": [], "lore": []}, ensure_ascii=False)}


def row(number, text="图片和说明", role="user"):
    return {"role": role, "message_id": str(number), "user_id": "20001", "text": text}


def image(number, **extra):
    return {"image_id": f"{number:032x}", "event_id": str(number), "user_id": "20001",
            "status": "ready", "error_code": "", "width": 640, "height": 480,
            "animated_first_frame": False, "data_url": f"data:image/png;base64,synthetic{number}", **extra}


def direct_message():
    return {"role": "user", "content": DIRECT_PREFIX + json.dumps({
        "trusted_identity": {"user_id": "20001"}, "available_expressions": [],
        "retrieved_memory": [], "lore": [], "recent_group_messages": [row(10)]})}


def test_budget_trimmed_group_history_cannot_bring_old_pictures_back():
    rows = [row(1, "很长的旧背景" * 100), row(2, "最新图片问题")]
    messages = [{"role": "system", "content": "完整角色规则"}, group_message(rows)]
    wanted = [messages[0], group_message(rows[-1:])]
    refs = references(group_context=rows)
    fitted = fit_context(messages, refs, kind="group_decision",
        window_tokens=estimate_tokens(wanted) + 1200 + 1024 + 2048, image_tokens=2048)
    assert not fitted["blocked"]
    assert retained_events(fitted["messages"], fitted["refs"], "group_decision", "2") == ["2"]


def test_summary_never_selects_pictures_from_any_source():
    assert retained_events([{"role": "system", "content": "summary"}, group_message([row(1)])],
        references(history=[{"id": 1, "text": "old"}]), "summary", "2", {1: "3"}) == []


def test_group_dialogue_and_current_image_are_deduplicated_with_current_last():
    messages = [{"role": "system", "content": "rules"}, group_message([row(1), row(2), row(3, role="assistant")]),
        {"role": "user", "content": FOLLOWUP_PREFIX + json.dumps({
            "confirmed_dialogue": [row(1), row(4)], "latest_text": "这张呢？", "current_speaker": "20001"})}]
    assert retained_events(messages, {"history": [{"id": 9}]}, "followup", "2", {9: "5"}) == ["1", "4", "5", "2"]


def test_only_retained_direct_history_references_supply_picture_events():
    messages = [{"role": "system", "content": "rules"}, direct_message(), {"role": "user", "content": "现在这张呢"}]
    assert retained_events(messages, {"history": [{"id": 4}]}, "reply", "20", {3: "old-cut", 4: "retained"}) == ["10", "retained", "20"]


@pytest.mark.parametrize("payload", ["not valid JSON", json.dumps({"recent_group_messages": [row(999)]})])
def test_user_text_cannot_masquerade_as_internal_background_and_select_images(payload):
    spoof = {"role": "user", "content": DIRECT_PREFIX + payload}
    messages = [{"role": "system", "content": "rules"}, direct_message(), spoof, {"role": "assistant", "content": "历史回答"}, spoof]
    assert retained_events(messages, {"history": []}, "reply", "20") == ["10", "20"]


def test_attachment_preserves_image_text_identity_and_never_mutates_source():
    pictures = [image(1), image(2, user_id="20002", animated_first_frame=True)]
    metadata = safe_image_references(pictures)
    messages = [{"role": "system", "content": "system remains text"},
                {"role": "user", "content": "同条消息：左图和右图有何不同？"}]
    before = deepcopy((messages, pictures, metadata))
    result = attach_images(messages, pictures, metadata)
    assert result[0] == messages[0]
    assert result[-1]["content"][0] == {"type": "text", "text": messages[-1]["content"]}
    blocks = result[-1]["content"]
    labels = [json.loads(block["text"].split("：", 1)[1]) for block in blocks[1:] if block["type"] == "text"]
    assert [r["event_id"] for r in labels] == ["1", "2"]
    assert [r["user_id"] for r in labels] == ["20001", "20002"]
    assert labels[1]["animated_first_frame"] is True
    assert [block["image_url"]["url"] for block in blocks if block["type"] == "image_url"] == [r["data_url"] for r in pictures]
    assert (messages, pictures, metadata) == before


def test_unselected_or_expired_images_are_never_attached_as_available():
    metadata = [image(1, url="https://gchat.qpic.cn/private?credential=hidden"), image(2)]
    # Only image 1 was selected, image 2 expired after metadata was captured.
    result = attach_images([{"role": "user", "content": "继续聊刚才的图"}], [image(1), image(3)], metadata)
    texts = "\n".join(r["text"] for r in result[0]["content"] if r["type"] == "text")
    images = [r for r in result[0]["content"] if r["type"] == "image_url"]
    assert len(images) == 1 and images[0]["image_url"]["url"] == image(1)["data_url"]
    assert "unavailable" in texts
    assert "credential" not in texts and "base64" not in texts and "https://" not in texts


def test_attachment_without_metadata_remains_plain_text():
    messages = [{"role": "user", "content": "没有图片"}]
    assert attach_images(messages, [image(1)], []) == messages


def test_images_cannot_be_appended_to_assistant_or_system_message():
    for role in ("assistant", "system"):
        with pytest.raises(ValueError, match="vision_prompt_shape_invalid"):
            attach_images([{"role": role, "content": "text"}], [image(1)], [image(1)])


def anchored_group(rows, *, reply_to=None):
    message=group_message(rows)
    data=json.loads(message["content"].split("\n",1)[1])
    data["current_message"]={"kind":"group","message_id":"2","user_id":"20001",
        "text":"当前没有配图","has_images":False,"image_count":0,"reply_to_id":reply_to}
    message["content"]=GROUP_PREFIX+json.dumps(data)
    return [{"role":"system","content":"rules"},message]


def test_old_group_picture_requires_explicit_quote_of_still_retained_source():
    rows=[dict(row(1),age_seconds=240),dict(row(2),age_seconds=0)]
    messages=anchored_group(rows)
    # Persisted per-member history is not a back door to pictures in group chats.
    assert retained_events(messages,{"history":[{"id":9}]},"group_decision","2",{9:"999"})==["2"]
    messages=anchored_group(rows,reply_to="1")
    sources=visual_sources(messages,{},"group_decision","2")
    assert sources["1"]=={"relation":"reply","age_seconds":240}
    # A quote cannot reach a source cut by the context window or token budget.
    assert retained_events(anchored_group(rows[1:],reply_to="1"),{},"group_decision","2")==["2"]


def test_ack_dialogue_does_not_refresh_old_picture_age():
    messages=anchored_group([dict(row(1),age_seconds=240),dict(row(2),age_seconds=0)])
    messages.append({"role":"user","content":FOLLOWUP_PREFIX+json.dumps({
        "confirmed_dialogue":[dict(row(1),age_seconds=2)],"latest_text":"当前文字","current_speaker":"20001"})})
    assert retained_events(messages,{},"followup","2")==["2"]


def test_earlier_picture_is_not_attached_to_current_text_and_trace_records_age():
    messages=anchored_group([dict(row(1),age_seconds=45),dict(row(2),age_seconds=0)])
    source=visual_sources(messages,{},"group_decision","2")["1"]
    metadata=safe_image_references([image(1,**source)])
    result=attach_images(messages,[image(1)],metadata,current_event="2")
    assert result[-1]==messages[-1]  # The current envelope is still plain text.
    assert result[-2]["content"][-1]["type"]=="image_url"
    assert metadata[0]["age_seconds"]==45 and metadata[0]["relation"]=="recent"
    assert result[-2]["content"][0]["text"] == DEFAULTS["earlier_image"]
