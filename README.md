# XZBot

可自行部署的 QQ 角色 Bot：Markdown 人设、多 Profile、连续群聊、按范围隔离的记忆，以及可查看的调用轨迹。模型和语音使用云端 API，本地运行 Python 服务、网页管理界面与 SQLite。

当前普通 QQ 接入路径：**QQ → NapCat → OneBot 11 → AstrBot 插件 → XZBot**。AstrBot 用作平台适配器和插件宿主；角色、记忆、参与判断、工具循环和管理页面由本项目实现，并非完整复用 AstrBot 原生对话流程。

## 功能

- DeepSeek、兼容 OpenAI Chat Completions 的接口、可选 Gemini Balance；Fish Audio 音色和模型配置。
- 自定义角色、人设、预设问答、分段发送、图片素材和角色独立背景资料。
- 群内别名唤醒、近期上下文、连续追问、分项参与投票、热烈参与与冷场发言。
- 按账号、角色、群/私聊和成员隔离的历史与记忆；自动整理、检索、查看、纠错、删除。
- 资料分块检索、全文常驻开关、渐进式资料读取、图片理解和联网检索。
- Bilibili 直播/动态/投稿、微博、Steam 状态订阅；共享订阅对象、多目标通知与图文卡片。
- 群级参与配置、记忆预设、调用/发送轨迹、owner 和白名单管理。

部分能力依赖所选云模型、外部平台接口、账号权限及有效登录状态；具体行为以管理界面与发送回执为准。

## 快速开始

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 Git。首先仅启动本机管理核心：

```sh
git clone git@github.com:Hanskrrr/XZBot.git
cd XZBot
sh scripts/setup.sh
.venv/bin/python scripts/install_git_hooks.py
AZUSA_ROOT="$PWD" .venv/bin/python -m azusa_bot.cli serve
```

默认示例为模拟模式，QQ 回复关闭。管理页监听 `127.0.0.1:6196`，管理口令保存在本机 `data/admin.token`。首次设置和平台运行边界见 [开始使用](docs/getting-started.md)。

0.9 提供 Windows、macOS、Linux 原生启动发行包。Windows 在宿主运行 XZBot，并使用单独安装的原生 NapCat + QQ 登录，无需 WSL 或 Lima。三端 CI 会在带空格和中文的新目录解压实际 ZIP、运行对应启动脚本、检查管理核心和 AstrBot 桥接；不登录真实 QQ。安装与真实 QQ 验证边界见 [发行版说明](docs/releases.md) 和 [平台支持](docs/platform-support.md)。

## 下载发行版

在 [GitHub Releases](https://github.com/Hanskrrr/XZBot/releases) 下载对应平台的 ZIP，解压后双击或运行 Start-XZBot。首次需要网络安装 Python 和固定依赖；不包含个人 key 或 QQ 登录状态。

新安装自带阿梓与灰泽满两个同人角色 Profile，含人设、提示词默认版本、预设问答、别名、基础背景资料和音色 ID。语音授权与使用开关默认关闭，下载者需自行确认使用权限。已有部署在「角色档案」从模板新建副本，不覆盖自己的资料。第三方资料保留来源与各自许可，不统一按代码的 MIT 许可重授权。

## 代码与私有数据

本仓库只保存代码、示例和通用文档。`config/local.json`、整个 `content/`、`data/`、`accounts/`、运行环境和私有迁移包均不提交。已有部署更新代码时不会由初始化脚本覆盖这些文件。
经明确选择和筛选的公开基础角色位于 `src/azusa_bot/starter_profiles/`；它们不含聊天、成员记忆、QQ/Owner 绑定、密钥、Cookie、近期动态缓存或推送目标。

- [架构与模块](docs/architecture.md)
- [Mac 与服务器同步开发](docs/synchronization.md)
- [凭据、隐私与仓库边界](docs/privacy.md)
- [私有迁移包导出与恢复](docs/public-migration.md)

## 验证

```sh
.venv/bin/python scripts/check_repository.py --staged
.venv/bin/python -m pytest -q
```

核心测试使用合成数据与隔离的凭据后端。AstrBot、Gemini 子进程和浏览器测试有额外环境要求；跳过不等于真实平台验证。部分语料验收测试依赖可选的本地资料包，资料不随公开源码分发，缺失时会跳过。`tests/ui/` 的部分历史脚本仍有平台或旧界面假设，不能把一次核心测试通过称作所有平台已通过。

## 许可与上游

本项目沿用仓库的 [MIT License](LICENSE)。Steam 状态分类复用模块的许可和来源在 `src/azusa_bot/vendor/steam_monitor/`。AstrBot、NapCat、Gemini Balance 的独立源码及依赖按各自许可使用，不随本仓库分发其运行环境。固定版本见 [平台支持](docs/platform-support.md)。
