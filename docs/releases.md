# XZBot 0.9 原生发行版

本版为 Release Candidate。三端共用同一业务核心和网页管理界面；Windows 无需 WSL。
发布包包含程序、阿梓与灰泽满两个公开基础角色、固定版本 uv 启动工具与许可证；不含个人凭据、私人历史、成员记忆或 QQ 会话。

## 下载与启动

解压到自己的固定目录，不要在压缩包内直接运行。首次启动需要网络下载 Python 3.12 和锁定依赖，之后复用本地环境。

| 系统 | 入口 | QQ 环境 |
|---|---|---|
| Windows x64 | 双击 `Start-XZBot.cmd` | 单独安装原生 NapCat / QQ；不使用 WSL 或 Lima |
| macOS Apple Silicon | 双击 `Start-XZBot.command` | 既有 Lima 路径；首次需安装 Homebrew、Lima 并准备 QQ 环境 |
| Linux x64 | `sh Start-XZBot.sh` | 单独安装原生 NapCat / QQ；服务器可通过 SSH 隧道访问 |

首次启动时有依赖安装进度，完成后打开 http://127.0.0.1:6196 。
Windows 会在本机打开 `data/admin.token`，将其中口令粘贴到管理页；不要发给别人。
Mac 原有启动入口自动连接；Linux 服务器在本机查看该文件，通过 SSH 隧道连接管理页。
关闭网页不会停止服务；关闭 Bot 使用界面上方的电源按钮。

此发行包不是签名的 MSI/DMG，也不内置 QQ、NapCat 或 AI 推理模型。Windows 采用原生 Python 进程，`cmd` 只是双击入口。首次安装后不要移动目录中的虚拟环境；迁移到新目录时重建环境。

## Windows / Linux 连接原生 NapCat

1. 按 [NapCat 官方说明](https://napneko.github.io/guide/boot/Shell)安装对应系统与 CPU 架构的 NapCat，退出它后配置。
   Windows x64 可使用官方 `NapCat.Shell.Windows.OneKey.zip`：运行 `NapCatInstaller.exe`，随后用 `napcat.bat` 启动；手动安装方式则先安装 QQ，再使用 `NapCat.Shell.zip` 的 `launcher.bat`（Windows 10 用 `launcher-win10.bat`）。本项目不会替第三方安装器接受协议或捆绑 QQ。
2. 在 XZBot「连接与权限」展开「Windows / Linux 原生 NapCat 连接配置」，下载当前账号的配置包，解压到 NapCat 的 `config` 目录。
3. 如果已有 `onebot11_QQ号.json`，其中账号专属设置优先于通用 `onebot11.json`，将下载配置中的 `network.websocketClients` 同步到账号专属文件，或先备份再移走旧账号专属配置。不要保留旧的额外消息连接。
4. 启动 NapCat。XZBot 默认连接本机 NapCat WebUI 6200，反向 WebSocket 6199；两者都只绑定回环地址。
5. 在 XZBot 管理页扫码登录、采用登录账号，然后配置 owner、白名单、模型/API key 和语音。

原生外部模式下，XZBot 的电源按钮暂停/恢复业务与自己的桥接。**它不终止外部 NapCat/QQ 进程**；退出该程序由 NapCat 自己管理。停 Bot 并不意味着已释放 QQ 进程内存。
多账号使用独立 NapCat 实例和独立配置；每个账号都从自己的管理页下载连接配置，不共用登录目录。

## Mac 首次准备

先安装 `brew install lima`。首次安装完成后，在项目目录运行：

```sh
.venv/bin/python scripts/napcat_runtime.py setup
```

该操作会下载固定版本 NapCat 与独立 Linux 运行环境。之后用 `Start-XZBot.command` 或安装已有 XZBot.app 桌面入口。
已有 Mac 项目升级只同步清理后的源码，再运行安装入口；保留本机数据和钥匙串，不能拿公开包覆盖个人配置。

## 凭据与数据

- macOS 使用系统钥匙串，Windows 使用 Windows 凭据管理器。
- Linux 首次安装显式建立仅本机账号可访问的私有文件凭据后端；这是文件权限保护，不声称磁盘加密。
- 所有平台的 `data/`、`config/local.json`、`content/` 和 `accounts/` 均不进入公开仓库或 Release。
- API key 在管理页输入。NapCat 连接配置包含本机访问令牌，不要上传 GitHub。
- 不要将同一个 QQ 同时放在两套 Bot 中运行。更新前使用管理页备份；Linux 或 Windows 不直接使用 Mac 虚拟环境/钥匙串。

## 内置角色与升级

首次安装会创建阿梓、灰泽满两个 Profile，默认选择阿梓。包含完整人设、额外提示词默认版本、预设问答、别名、基础资料（阿梓 285 份、灰泽满 2 份）与 Fish 音色 ID。公开订阅来源已列入对象库，角色动态同步默认关闭；没有 QQ 接收目标或别人账号的白名单。填好自己的模型 Key、登录 QQ 并设置 Owner 后使用；语音需自行确认音色权限后开启。

已有 `data/profiles.json` 或私人 `content/persona.md` 时不会自动覆盖。需要模板时，到「角色档案」选择「从阿梓模板新建」或「从灰泽满模板新建」。模板中的第三方资料保留各自来源与许可；萌娘百科资料标注 CC BY-NC-SA 4.0，不能将整套资料视作 MIT 授权。

更新前备份。可将新包的程序文件合并到原目录，保留 `data/`、`content/`、`accounts/`、`config/local.json` 和 `config/runtime.json` 等本地配置，再运行启动入口；安装器按依赖指纹更新环境。不要用空模板覆盖现有数据，也不要把旧虚拟环境复制到另一台电脑。

## 本版新增

「群聊参与 → 冷场发言 → 立即发起一次聊天」向当前选中的群生成并发送一次文字或语音开场（遵循当前角色的语音开关与授权），使用当前角色。
它跳过自动冷场等待、时段、自动参与额度和评分，但仍受 Bot 总开关、白名单、QQ 通道、云端总额度约束。
相同请求不会重发；角色、权限或群聊上下文在生成期间变化会取消旧稿。
结果和引用资料可在「调用轨迹 → 手动发起群聊」查看；额外提示词在角色的可编辑提示词中。

## 验证边界

GitHub Actions 分别在 Windows、macOS、Linux 测试原生文件锁/权限、管理 API、角色模板、提示词默认版本与手动发言机制。随后将实际待发布 ZIP 解压到带空格和中文的新目录，执行包内启动入口 `--no-open`，检查新装角色与真实固定版本 AstrBot 桥接。每个平台附带 `.verification.json`，记录该包 SHA256、提交号、检查结果及没有执行真实 QQ 登录这一边界。
CI 不登录 QQ，也不使用任何真实模型密钥。Mac 现有 QQ 环境可本机验证；Windows/Linux 的实际扫码、腾讯收发、语音和订阅媒体需要在目标机器验收。
可选 Gemini Balance 仍需目标平台安装其 Git 和依赖；发布测试不等于已经验证它在每个系统上运行。


## RC4 / RC5：GitHub 更新与自定义下载源

管理页「更新与备份 → 版本更新」默认使用 `https://api.github.com/repos/Hanskrrr/XZBot/releases`，无需 GitHub 密钥；可每 6 小时检查，并可选择自动下载。也支持自定义可信 HTTPS `updates.json`。安装由管理员点“安装并重启 XZBot”触发，不在聊天中静默重启。首次从 RC3 升级到带此功能的版本仍需手动更新一次；RC3 本身没有更新器和程序文件清单。

发行包包含 `RELEASE-FILES.json`。更新前校验整包 SHA256、平台、架构、版本、提交号、内部文件清单及本地代码是否被修改。只允许替换公开程序文件，拒绝写 data/content/accounts/config/local.json 等私有路径、符号链接、Windows 特殊路径及 Git 开发目录。安装期间持有各账号启动锁，验证 PID 命令行与工作目录后只停止 XZBot 核心和 AstrBot 桥接，不停止 QQ/NapCat/SnowLuma/虚拟机。保留原先运行的实例列表；只恢复那些实例。

`data/updates/rollback-*` 保存旧程序和 SQLite 一致性备份。失败时尝试恢复旧程序及依赖再重启；数据库备份供必要时人工恢复，不自动回灌数据库而覆盖更新期间新增的聊天。跨版本数据库模式是否支持向后兼容仍须在发布前验收。错误状态留在更新页；进程被断电或终止不能保证自动回滚，备份目录应保留。

清单可以与 ZIP 一同放在独立的 HTTPS 静态下载站点。运行 `python scripts/build_update_feed.py <已验证发行资产目录>` 生成不绑定个人 GitHub 的 `updates.json`，使用相对 ZIP 地址，自定义静态站点要求清单和 ZIP 同源且没有跨站重定向；GitHub 专用源仅额外接受其官方发行资产 CDN 的签名重定向。三个 ZIP 必须各自有成功的 verification.json。SHA256 校验保证下载完整性；目前发布者身份依赖管理员信任的 HTTPS 更新源，未另做离线签名。

三端 CI 验证的是原生安装、核心/桥接启动及更新器回归，不含真实 QQ 扫码或发送。完整 QQ 登录与第三方平台收发仍需目标机器验收。源码构建、Gitee 镜像与附件发布步骤见 [源码构建手册](source-build.md)，日常使用见 [简明操作手册](user-guide.md)。

若需要避免与个人 GitHub 账号关联，仅更换下载 URL 不够：发行包中 README、作者署名、源代码引用和旧仓库链接也须在正式分发前审查。既有公开仓库与 RC3 Release 并未因此隐藏或删除。
