"""Shared addressee metadata using synthetic identities and no network calls."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from azusa_bot.group_targets import GroupTargets, explicitly_group_addressed, is_singular_followup
from azusa_bot.settings import private_write


@pytest.fixture
def targets(tmp_path):
    now = [1000.]
    return GroupTargets(tmp_path, clock=lambda:now[0]), now


def test_target_identity_expiry_and_private_content_free_file(targets):
    store, now = targets
    token = store.claim("30001","90001","10001",180)
    assert token and store.get("30001","90001") == {
        "bot_id":"10001","token":token,"expires_at":1180.}
    assert store.get("30002","90001") is None
    assert store.get("30001","90002") is None
    assert store.path.stat().st_mode & 0o777 == 0o600
    with store._db() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(group_targets)")}
    assert columns == {"group_id","user_id","bot_id","token","expires_at","claimed_at"}
    now[0] = 1180.
    assert store.get("30001","90001") is None
    assert not store.refresh("30001","90001","10001",token,180)


def test_primary_and_children_share_one_target_registry(tmp_path):
    manager = (tmp_path / "QQBot").resolve()
    children = []
    for slot in (1,2):
        root = manager / "accounts" / (str(slot)*32)
        private_write(root / "config/instance.json", json.dumps({
            "schema":1,"slot":slot,"manager_root":str(manager)}))
        children.append(root)
    primary, left, right = [GroupTargets(root) for root in (manager,*children)]
    token = left.claim("30001","90001","20001",180)
    assert primary.path == left.path == right.path == manager / "data/group_targets.sqlite3"
    assert right.get("30001","90001")["token"] == primary.get("30001","90001")["token"] == token
    right.release("30001","90001")
    assert left.get("30001","90001") is None


def test_late_receipt_cannot_reclaim_newer_explicit_target(targets):
    store, now = targets
    old = store.claim("30001","90001","10001",180)
    now[0] += 5
    assert store.refresh("30001","90001","10001",old,180)
    assert store.get("30001","90001")["expires_at"] == 1185.
    newer = store.claim("30001","90001","20001",180)
    assert newer != old
    assert not store.refresh("30001","90001","10001",old,180)
    assert not store.refresh("30001","90001","20001",old,180)
    assert store.claim_if_unbound("30001","90001","10001",180) is None
    assert store.get("30001","90001")["token"] == newer
    store.release("30001","90001")
    assert not store.refresh("30001","90001","20001",newer,180)
    assert store.claim_if_unbound("30001","90001","10001",180)


def test_delayed_older_explicit_claim_does_not_overwrite_newer_one(tmp_path):
    older = GroupTargets(tmp_path, clock=lambda:1000.)
    newer = GroupTargets(tmp_path, clock=lambda:1001.)
    token = newer.claim("30001","90001","20001",180)
    assert older.claim("30001","90001","10001",180) is None
    assert newer.get("30001","90001")["token"] == token


def test_concurrent_first_receipts_claim_once_and_explicit_tokens_are_atomic(tmp_path):
    stores = [GroupTargets(tmp_path,clock=lambda:1000.) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        first = list(pool.map(lambda i: stores[i].claim_if_unbound("30001","90001",str(10001+i),180),range(8)))
    assert sum(token is not None for token in first) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda i: stores[i].claim("30001","90001",str(10001+i),180),range(8)))
    assert len(set(tokens)) == 8
    current = stores[0].get("30001","90001")
    assert current["token"] in tokens
    for index, token in enumerate(tokens):
        assert stores[index].refresh("30001","90001",str(10001+index),token,180) == (token == current["token"])


def test_capacity_never_evicts_active_targets_and_expiry_frees_slots(tmp_path):
    now = [1000.]
    store = GroupTargets(tmp_path, clock=lambda:now[0], max_targets=2)
    first = store.claim("30001","90001","10001",30)
    second = store.claim("30001","90002","10001",180)
    assert store.claim("30001","90003","20001",180) is None
    assert store.claim_if_unbound("30001","90003","20001",180) is None
    assert store.get("30001","90001")["token"] == first
    assert store.get("30001","90002")["token"] == second
    # Updating an existing explicit binding does not require a new slot.
    assert store.claim("30001","90002","20001",180)
    now[0] += 31
    assert store.claim("30001","90003","20001",180)


def test_release_bot_removes_only_its_targets(targets):
    store, _ = targets
    store.claim("30001","90001","10001",180)
    store.claim("30002","90001","10001",180)
    other = store.claim("30001","90002","20001",180)
    store.release_bot("10001")
    assert store.get("30001","90001") is None
    assert store.get("30002","90001") is None
    assert store.get("30001","90002")["token"] == other


@pytest.mark.parametrize("value", ["", "0", "01", "abc", "10001\n", 10001, True])
def test_rejects_untrusted_or_non_numeric_identity(targets,value):
    store, _ = targets
    with pytest.raises(ValueError):
        store.claim("30001",value,"10001",180)
    with pytest.raises(ValueError):
        store.get(value,"90001")


@pytest.mark.parametrize("seconds", [0,-1,float("inf"),float("nan"),True,"180",7201])
def test_rejects_invalid_duration(targets,seconds):
    store, _ = targets
    with pytest.raises(ValueError):
        store.claim("30001","90001","10001",seconds)


@pytest.mark.parametrize("text", ["你觉得呢？","您刚才说的是什么意思？","妳还想继续吗", "继续吧", "那再来一次", "那这个呢", "刚才那个再解释一下", "换一种说法"])
def test_singular_continuation_cues(text):
    assert is_singular_followup(text)
    assert not explicitly_group_addressed(text)


@pytest.mark.parametrize("text", ["你们怎么看", "你俩都说说", "两位一起回答吧", "大家觉得呢", "各位继续", "都说吧", "一起回答", "请分别回答", "大家，评价一下", "大家怎么看", "请大家回答", "各位：都说说吧", "能不能一起回答"])
def test_plural_invitations_release_single_addressee(text):
    assert explicitly_group_addressed(text)
    assert not is_singular_followup(text)


@pytest.mark.parametrize("text", ["今天晴天", "他刚才说这个游戏不错", "他们都说今天挺好", "刚才她说想去逛街", "今天刚刚看到新闻", "朋友继续去吃饭了", "新闻说天气会转凉"])
def test_generic_third_person_comments_are_not_singular_followups(text):
    assert not is_singular_followup(text)
    assert not explicitly_group_addressed(text)


@pytest.mark.parametrize("text", ["大家都说你很自恋", "据说大家都说你很自恋", "大家觉得你很可爱", "各位群友都觉得你说得对", "他们一起回答了你的问题"])
def test_reported_group_opinion_keeps_the_singular_addressee(text):
    assert not explicitly_group_addressed(text)
    assert is_singular_followup(text)


def test_target_lookup_can_read_while_another_account_holds_writer_lock(targets):
    store, now = targets
    token = store.claim("30001","90001","10001",1)
    with store._db():
        assert store.get("30001","90001")["token"] == token
        now[0] += 2
        assert store.get("30001","90001") is None
