"""Explicit QQ commands; model output can never call this management surface.

Invoke only after the bridge's event authentication, whitelist and event ledger
reservation. Policy is also checked here and again at each database commit.
"""
from __future__ import annotations

import re
from .settings import read_settings
from .bot_control import activity_allowed, read_control
from .subscription_sources import SubscriptionError

PLATFORMS = {"B站直播": ("bilibili_live", "room"), "直播": ("bilibili_live", "room"),
             "B站投稿": ("bilibili_video", "uid"), "切片": ("bilibili_video", "uid"), "B站动态": ("bilibili_dynamic", "uid"), "动态": ("bilibili_dynamic", "uid"),
             "Steam": ("steam", "steamid"), "steam": ("steam", "steamid"),
             "微博": ("weibo", "uid")}
LABELS = {"bilibili_live": "B站直播", "bilibili_dynamic": "B站动态", "bilibili_video": "B站投稿", "steam": "Steam", "weibo": "微博"}
HELP = """订阅指令（通知只发到当前会话）：
/订阅 列表
/订阅 添加 B站直播 直播间号 [图文|文字]
/订阅 添加 B站动态 UP的UID [图文|文字]
/订阅 添加 Steam SteamID64 [图文|文字]
/订阅 添加 微博 UID [图文|文字]
/订阅 暂停|恢复|删除|图文|文字 编号
变更需要你在当前群／私聊的 owner 权限；默认图文并启用，首次只记基线，不补发历史。编号可从列表复制。凭据在管理页填写，不要在 QQ 发送。"""


def _rows(service, event):
    return [row for row in service.list() if row["self_id"] == event.app_id
            and row["kind"] == event.kind and row["target_id"] == event.scope_id]


def _check_scope(service, event, epoch=None):
    if not activity_allowed(service.root, epoch):
        raise PermissionError()
    settings = read_settings(service.root)
    if (event.transport != "onebot" or settings.transport != "onebot"
            or not settings.onebot_enabled or event.app_id != settings.onebot_self_id
            or event.user_id == event.app_id or event.kind not in {"dm", "group"}
            or event.kind == "dm" and event.scope_id != event.user_id):
        raise PermissionError()
    allowed = settings.onebot_allowed_groups if event.kind == "group" else settings.onebot_allowed_dm_users
    if event.scope_id not in allowed:
        raise PermissionError()
    return settings


async def handle_subscription_command(service, event, owner: bool) -> str | None:
    words = event.text.strip().split()
    if not words or words[0] != "/订阅":
        return None
    epoch = read_control(service.root)["epoch"]
    try:
        _check_scope(service, event, epoch)
    except PermissionError:
        return "这个会话暂时不能管理订阅，请在管理页检查账号连接与白名单。"
    if len(words) == 1 or words[1:] == ["帮助"]:
        return HELP
    if words[1:] == ["列表"]:
        rows = _rows(service, event)
        if not rows:
            return "当前会话还没有订阅。发 /订阅 帮助 查看用法。"
        lines = [f"{row['id'][:8]} · {LABELS.get(row['type'], '订阅')} {row['source_id']} · "
                 f"{'启用' if row['enabled'] else '暂停'} · {'图文' if row['include_image'] else '文字'}"
                 for row in rows[:20]]
        tail = "\n更多订阅请到管理页查看。" if len(rows) > 20 else ""
        return "当前会话的订阅：\n" + "\n".join(lines) + tail

    def authorize(number=None):
        settings = _check_scope(service, event, epoch)
        if not owner or not settings.is_owner(event.kind, event.scope_id, event.user_id, "onebot"):
            raise PermissionError()
        if number is not None and not any(row["id"] == number for row in _rows(service, event)):
            raise PermissionError()

    try:
        authorize()
        if words[1] == "添加":
            if len(words) not in {4, 5} or words[2] not in PLATFORMS or len(words) == 5 and words[4] not in {"图文", "文字"}:
                return "格式：/订阅 添加 微博 UID [图文|文字]。其他类型见 /订阅 帮助。"
            kind, input_type = PLATFORMS[words[2]]
            # The target is always the authenticated current conversation.
            row = await service.add({"type": kind, "input_type": input_type, "value": words[3],
                "kind": event.kind, "target_id": event.scope_id, "enabled": True,
                "include_image": len(words) == 4 or words[4] == "图文"}, authorize=authorize)
            return f"已添加 {LABELS[kind]} 订阅（{row['id'][:8]}），之后的新消息会发到当前会话。已有内容不会补发。"
        if len(words) != 3 or words[1] not in {"暂停", "恢复", "删除", "图文", "文字"}:
            return "没有这个订阅操作。发 /订阅 帮助 查看用法。"
        if not re.fullmatch(r"[a-f0-9]{8,32}", words[2]):
            return "请从 /订阅 列表 复制订阅编号。"
        matches = [row for row in _rows(service, event) if row["id"].startswith(words[2])]
        if len(matches) != 1:
            return "当前会话没有唯一匹配的订阅，请从 /订阅 列表 核对编号。"
        number = matches[0]["id"]
        guard = lambda: authorize(number)
        if words[1] == "删除":
            service.delete(number, authorize=guard)
            return "已删除这条订阅。"
        update = ({"enabled": words[1] == "恢复"} if words[1] in {"暂停", "恢复"}
                  else {"include_image": words[1] == "图文"})
        await service.update(number, update, authorize=guard)
        return {"暂停": "已暂停这条订阅，未发送的通知已取消。",
                "恢复": "已恢复这条订阅，只提醒后续新内容。",
                "图文": "这条订阅已改为图文通知。",
                "文字": "这条订阅已改为文字通知。"}[words[1]]
    except PermissionError:
        return "只有当前会话已绑定的 owner 可以变更订阅。请在管理页「连接与 Owner」设置，不能靠聊天自称主人获得权限。"
    except (SubscriptionError, ValueError) as error:
        code = str(error)
        if code == "bilibili_subscription_exists":
            return "当前会话已经有这条订阅了，发 /订阅 列表 查看。"
        if code in {"weibo_auth_or_api_rejected", "bilibili_credentials_required", "steam_credentials_required", "subscription_credentials_unavailable"}:
            return "还没能读到订阅内容，请在管理页「订阅与通知」检查平台登录信息并预览；不用把凭据发到 QQ。"
        return "这次没有完成订阅变更，请核对对象编号，或在管理页预览来源后再试。"
