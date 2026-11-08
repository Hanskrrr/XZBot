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
