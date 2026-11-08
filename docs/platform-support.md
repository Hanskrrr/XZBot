# 平台支持与依赖

| 环境 | 当前边界 |
|---|---|
| macOS / Apple Silicon | 现有完整运行路径，NapCat 位于隔离 Linux 环境，管理启停与桌面入口面向 Mac |
| Linux | 核心、SQLite、Web UI、POSIX OneBot 桥可复用；迁移恢复和明确的私有文件凭据后端已实现；systemd/容器生命周期、目标 QQ 和媒体链路仍需适配验收 |
| Windows + WSL2 | 可考虑将核心放在 WSL2、NapCat 原生运行；必须适配双向地址和媒体路径，不能视两个 localhost 为同一个主机 |
| 原生 Windows | NapCat 上游提供支持；XZBot 尚需处理 fcntl、POSIX 权限、符号链接、可执行文件路径和宿主启停，未宣称直接兼容 |

已有代码中的 `scripts/napcat_runtime.py`、`bot_runtime_action.py`、桌面/监督器入口仍面向 Mac。迁移时不要仅修改持久电源开关，就假定 QQ 进程管理已经完成。

- AstrBot：v4.29.4 / `61335414433db721b3f7ecc93f90deafe0a59d78`，见 `requirements-astrbot.lock`。
- Gemini Balance：2.2.8 / `5512f7ff218a85970ecb643e2d04e44e7de96efd`，可选，见 `requirements-gemini-balance.lock`。
- 核心依赖：`requirements-lock.txt`，Python 3.12。
- Linux 图片通知需要合适的中文字体，例如 Noto CJK。

QQ 登录和平台安全限制由腾讯处理，迁移环境不保证消除限制。保持固定的账号归属和正常验证流程，不让同一个账号在两套 Bot 中同时收发。

上游安装资料：[AstrBot](https://docs.astrbot.app/)、[NapCat](https://napneko.github.io/guide/boot/Shell)、[Gemini Balance](https://github.com/gemini-balance/gemini-balance)。
