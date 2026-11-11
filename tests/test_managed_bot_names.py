"""Read active peer aliases without creating or repairing another role store."""

import json

import pytest

from azusa_bot.accounts import AccountError, managed_bot_names
from azusa_bot.group_addressing import vocative_targets, vocative_target_groups
from azusa_bot.profiles import ProfileStore
from azusa_bot.settings import private_write, read_settings, write_settings
from test_accounts import create_child, manager, ready


def profile_file(root):
    return root / "data/profiles.json"


def configure_names(root, display, aliases):
    path = profile_file(root)
    data = json.loads(path.read_text())
    row = next(row for row in data["profiles"] if row["id"] == data["active_id"])
    row.update(name=display,address_names=aliases)
    private_write(path,json.dumps(data,ensure_ascii=False))


def test_reads_primary_and_active_child_names_without_mutating_stores(manager, monkeypatch):
    _, child = create_child(manager)
    ready(child,"20001")
    configure_names(manager.root,"主角色",["主昵称"])
    store = ProfileStore(child)
    created = store.create({"name":"乙角色","address_names":["小乙","ＢＯＴ","bot"],
                            "persona":"这是另一个用于合成测试的角色。"})
    store.activate(created["id"])
    before = {path:path.read_bytes() for path in (profile_file(manager.root),profile_file(child))}
    monkeypatch.setattr("azusa_bot.accounts.ProfileStore",lambda *_:pytest.fail("Read must not initialize ProfileStore"))
    assert managed_bot_names(child) == {"10001":("主昵称","主角色"), "20001":("小乙","ＢＯＴ","乙角色")}
    assert all(path.read_bytes() == data for path,data in before.items())


@pytest.mark.parametrize("corruption", ["missing","json","oversized","no_active","duplicate","bad_alias","symlink"])
def test_invalid_peer_profile_is_empty_without_repairing_it(manager, corruption):
    _, child = create_child(manager)
    ready(child,"20001")
    path = profile_file(child)
    data = json.loads(path.read_text())
    if corruption == "missing":
        path.unlink()
    elif corruption == "json":
        private_write(path,"not json")
    elif corruption == "oversized":
        private_write(path," "*(256*1024+1))
    elif corruption == "no_active":
        data["active_id"] = "missing"
        private_write(path,json.dumps(data))
    elif corruption == "duplicate":
        data["profiles"].append(data["profiles"][0])
        private_write(path,json.dumps(data))
    elif corruption == "bad_alias":
        data["profiles"][0]["address_names"] = ["x"]
        private_write(path,json.dumps(data))
    else:
        path.unlink()
        path.symlink_to(profile_file(manager.root))
    assert managed_bot_names(manager.root)["20001"] == ()
    if corruption == "missing":
        assert not path.exists()
    if corruption == "symlink":
        assert path.is_symlink()


def test_only_configured_numeric_onebot_accounts_are_returned(manager):
    _, unbound = create_child(manager)
    _, official = create_child(manager)
    ready(official,"20002")
    write_settings(official,read_settings(official).model_copy(update={"transport":"official"}))
    assert set(managed_bot_names(unbound)) == {"10001"}


def test_invalid_account_registry_is_not_hidden(manager):
    private_write(manager.registry.path,"not json")
    with pytest.raises(AccountError,match="accounts_manifest_invalid"):
        managed_bot_names(manager.root)


@pytest.mark.parametrize("text,expected", [
    ("满区", {"10001"}),
    ("  ‘满区’，你怎么又来了？", {"10001"}),
    ("满区你怎么这么说", {"10001"}),
    ("满区：您最近怎么样？", {"10001"}),
    ("满区 妳怎么看", {"10001"}),
    ("满区、阿梓，你们都说说", {"10001", "20001"}),
    ("满区 / 阿梓：最近过得好吗", {"10001", "20001"}),
    ("满区/阿梓/Ann：你们都说说", {"10001", "20001", "30001"}),
    ("满区 阿梓 你们最近如何", {"10001", "20001"}),
    ("阿梓今天直播了，你觉得呢", set()),
    ("你觉得阿梓怎么样", set()),
    ("今天满区和阿梓都来了", set()),
    ("满区今天心情不错", set()),
    ("ann, 你怎么看", {"30001"}),
    ("ＡＮＮ：你怎么看", {"30001"}),
    ("Anna, 你怎么看", set()),
    ("banner，你怎么看", set()),
    ("ann_name，你怎么看", set()),
    ("Ann's work is good, what do you think?", set()),
    ("ann2，你怎么看", set()),
])
def test_vocative_targets_only_claim_leading_addressees(text, expected):
    assert vocative_targets(text,{"10001":("满区",),"20001":("阿梓",),"30001":("Ann",)}) == expected


def test_vocative_targets_preserve_ambiguous_aliases_and_match_longest_name():
    mapping = {"10001":("小梓","Ann"),"20001":("小梓","Anna")}
    assert vocative_targets("小梓，你怎么看",mapping) == {"10001","20001"}
    assert vocative_targets("Anna, 你怎么看",mapping) == {"20001"}
    assert vocative_targets("Ann Anna, 你们都说说",mapping) == {"10001","20001"}


def test_vocative_targets_are_bounded_and_ignore_malformed_names():
    assert vocative_targets("名字很长"*20+"，你怎么看",{"10001":("名字很长"*20,)}) == set()
    assert vocative_targets("满区，你怎么看",{"10001":("满区",None),"20001":"满区"}) == {"10001"}
    assert vocative_targets(" "*600+"满区，你怎么看",{"10001":("满区",)}) == set()


def test_vocative_groups_distinguish_a_shared_name_from_two_named_accounts():
    mapping = {"10001":("小梓","满区","满满"),"20001":("小梓","阿梓")}
    assert vocative_target_groups("小梓，你怎么看",mapping) == (frozenset({"10001","20001"}),)
    assert vocative_target_groups("满区，阿梓，你们怎么看",mapping) == (
        frozenset({"10001"}),frozenset({"20001"}))
    assert vocative_target_groups("小梓，小梓，你怎么看",mapping) == (frozenset({"10001","20001"}),)
    assert vocative_target_groups("满区，满满，你怎么看",mapping) == (
        frozenset({"10001"}),frozenset({"10001"}))
    assert vocative_target_groups("阿梓今天直播了，你觉得呢",mapping) == ()
