# 架构

```text
QQ / NapCat → OneBot 11 → 固定版本 AstrBot + 接入插件
                                  ↓ 已认证事件 / 发送回执
                         XZBot Python 核心
                         ├─ 权限、账号与 Profile
                         ├─ 近期上下文、参与投票、工具循环
                         ├─ Markdown 人设与背景检索
                         ├─ SQLite 历史、记忆、订阅、轨迹
                         └─ 云模型、识图、检索、语音
```

主要目录：

| 位置 | 职责 |
|---|---|
| `src/azusa_bot/` | FastAPI、业务服务和存储 |
| `astrbot_plugin_azusa_onebot/` | 普通 QQ 事件桥、发送和回执 |
| `astrbot_plugin_azusa_bridge/` | 保留的官方 QQ 桥接 |
| `ui/index.html` | 本机管理界面 |
| `scripts/` | 初始化、运行、维护和迁移 |
| `tests/` | 合成数据回归测试 |

`core.py` 组织事件处理；`profiles.py` 管理角色隔离；`group_policies.py`、`participation_vote.py` 控制群参与；`memory.py` 保存历史、事实、群记忆及整理进度；`lore.py` 管理背景资料；`trajectories.py` 记录送入模型的上下文来源与结果。

个人记忆采用先限定 scope/subject、再计算词项相关度的检索；背景资料使用分块与全文索引。两者不是同一种检索器，也没有本地嵌入推理模型。真实图片只进入适用的近期事件上下文，旧历史不是默认附图来源。

账号间不共享实时登录，Profile 间不共享角色记忆。owner 根据认证接入提供的稳定 ID 判断，聊天内容、昵称和模型判断不能授予管理权。

AstrBot 原生 PersonaManager/ConversationManager/Provider 不构成本项目当前的角色管线；预设问答、分段发送和工具执行是本项目的实现。独立宿主适配应保持上述业务层不分叉。
