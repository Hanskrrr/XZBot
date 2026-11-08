# 平台支持与依赖

0.9 提供 Windows x64、macOS Apple Silicon、Linux x64 原生启动包。安装、使用和验证边界见 [发行版说明](releases.md)。

| 环境 | 运行方式 |
|---|---|
| macOS / Apple Silicon | 本机业务服务与钥匙串；现有 Lima QQ 路径保持兼容 |
| Windows x64 | 原生 Python 3.12、Windows 文件锁/ACL/凭据管理器；连接单独安装的原生 NapCat，无需 WSL |
| Linux x64 | 原生 Python 3.12、POSIX 文件锁和显式私有文件凭据后端；连接单独安装的原生 NapCat |

Windows/Linux 中 XZBot 电源按钮管理自身业务和桥接，不代管外部 QQ 的进程生命周期。登录自启的图形化开关目前仅支持 macOS；其他平台可按宿主的服务管理方式配置。

- AstrBot：v4.29.4 / `61335414433db721b3f7ecc93f90deafe0a59d78`，固定归档 SHA256 与依赖锁。
- Gemini Balance：可选，2.2.8；仍需目标系统安装 Git 及其依赖，未列为三端 Release 的真实云调用验收项。
- Python 3.12 与核心锁定依赖；Linux 图片通知需要 Noto CJK 字体，Windows 使用系统微软雅黑。

原生安装/本地 API/桥接启动测试与真实腾讯收发是不同层次的验证。CI 不登录 QQ，不含真实云 API key；目标机器仍需扫码并验证文字、语音、图片。QQ 平台的登录与安全限制由腾讯处理，同一 QQ 不得由两套部署同时收发。

## 可选 QQ 接入路径

在每个账号的「连接与 Owner → QQ 登录 → QQ 运行路径」分别选择环境和适配器。

| 组合 | 当前边界 |
|---|---|
| Lima + NapCat | 原有路径，保留原数据与登录缓存 |
| Lima + SnowLuma | 管理已安装的独立 `sl` 虚拟机；官方 Docker 镜像提供 QQ 与 noVNC；与 NapCat VM 互斥启动 |
| macOS 原生 + NapCat | 上游已有原生安装器。本项目独立 QQ 副本仍属试验，必须完成验证后才可选，不能把进程存在当成安装成功 |
| 外部管理 + NapCat / SnowLuma | Windows、Linux 或 Mac 上单独安装适配器；XZBot 下载连接配置并管理桥接，不代管外部 QQ 进程 |

切换先关闭当前账号的回复和运行环境，再保存选择，清除旧连接的在线证据。旧路径的 QQ 数据不会删除。目标环境尚未准备时拒绝切换；外部进程未退出、仍占用连接端口时也拒绝切换。打开登录环境不会启用回复，完成扫码、身份核对后再点「启动 Bot」。角色、人设、记忆、白名单和 API 凭据不随接入器改变。

SnowLuma v2.4.2 有 Windows x64、Linux x64/ARM64 发行包，没有原生 macOS 包。Lima 是 Linux 运行环境，不是与 SnowLuma 同一层的协议。官方推荐在 Linux 使用其 Docker 镜像；Mac 上使用 Lima 承载这一组合。

SnowLuma 通过 `get_status` 的 `online/good`、`get_login_info` 的账号身份及新鲜的反向 WebSocket 状态联合检查。WebUI 能访问不能证明 QQ 在线；发送仍要求真实消息回执，不能据此承诺腾讯侧不风控。SnowLuma 的 QQ 扫码在 noVNC 桌面，首次使用协议在 SnowLuma WebUI；它们和 NapCat 的二维码接口不同。

本机试用环境关闭了自动进程加载。重新启动后，在 SnowLuma「进程注入」选择正确 QQ 并点击「加载」；桌面已登录但仍显示等待登录时，可点击该进程的「刷新管道状态 / 重连」。看到 OneBot 通道已连接后，再启动 XZBot。适配器已在线仍不能替代真实消息收发验证。

2026-11-08 本机验证：Lima + SnowLuma v2.4.2 已通过实际私聊文字与语音收发，并取得发送回执；首次语音有一次未确认且未送达，原因未确定，不能据后续成功宣称长期稳定。原生 Mac 的 App Store / 官网 QQ 独立副本均未通过 NapCat 启动验证；官网副本入口修改还遇到 macOS `Operation not permitted`，保持禁用。原生上游支持不等于本项目已完成适配，不能据此标为可用。

连接配置包包含访问令牌，不得提交 Git。SnowLuma 的 HTTP 状态端口使用账号基础端口加 6，noVNC 使用加 7；均只转发至 Mac 回环地址。已有 `onebot_<QQ号>.json` 会覆盖全局 `onebot.json`，导入时需要同步对应的 HTTP 和反向 WebSocket 设置。

本项目不再分发 SnowLuma 原生组件，不提供捆绑其二进制的公开安装包。请阅读上游许可并使用官方发行渠道。已安装环境的启动/停止与账号配置是本项目的适配层。

依据：[NapCat macOS 官方安装器](https://github.com/NapNeko/NapCat-Mac-Installer)、[SnowLuma 发行包](https://github.com/SnowLuma/SnowLuma/releases/tag/v2.4.2)、[官方 Docker 部署说明](https://snowluma.github.io/zh/docs/guide/deploy/docker)、[SnowLuma EULA](https://github.com/SnowLuma/SnowLuma/blob/v2.4.2/EULA.md)。
