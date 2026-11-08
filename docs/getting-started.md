# 开始使用

## 初始化

在项目目录运行 `sh scripts/setup.sh`：按锁文件建立 Python 环境，仅在文件不存在时复制示例配置和示例人设。它不会开启 QQ，也不会覆盖已有角色、人设、密钥或白名单。`content/` 是本地可编辑数据，示例位于 `examples/`。

运行 `AZUSA_ROOT="$PWD" .venv/bin/python -m azusa_bot.cli serve` 启动回环管理服务。`data/admin.token` 是本地管理口令，在页面中输入；不要发到聊天、提交到 Git 或公开 URL。Mac 的应用入口可通过 `.venv/bin/python scripts/install_desktop.py` 安装；Linux 不运行 Mac 桌面安装脚本。

## 配置顺序

1. 在「模型与语音」填写供应商地址、模型和凭据；兼容接口 key 与目标地址绑定。
2. 在「角色档案」「完整人设」设置角色、别名、语音和示范问答；背景资料和图片素材通过各自页面导入。
3. 完成适用于当前宿主的 QQ 环境安装、登录和账号核对。普通 QQ 使用 NapCat；保留的官方 QQ 适配器属于另一种身份体系，不能互换 owner 标识。
4. 配置白名单与 owner。先验证本人私聊，再按需启用群聊参与、长期采集和订阅。

QQ 运行环境及进程控制的支持边界见 [平台支持](platform-support.md)。不要在两个运行实例中同时启用同一个 QQ。

## 本地数据

默认角色内容在 `content/`，额外 Profile 在 `data/profiles/`；额外 QQ 账号在 `accounts/`。数据库和凭据由应用管理，不通过 Git 同步。更新前备份，恢复时保留原作用域和账号身份。
