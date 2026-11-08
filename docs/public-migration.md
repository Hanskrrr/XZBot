# 私有迁移导出与恢复

本文件是交接入口。先核对代码版本、[平台支持](platform-support.md) 和目标机器；恢复成功不代表 QQ 端到端部署完成。

接手 muse：私有 ZIP 已包含导出时的源码、导入脚本与数据；不必再手工录入各项配置。`bundle.json` 中的 `source_head` 是代码提交依据，不能仅凭相同的 RC 版本号判断源码一致。GitHub main 用于后续代码同步，公开 Release 不包含私人数据。不要把本 ZIP 上传到公开仓库或 Release，也不要在聊天、终端输出或部署日志中展开密钥。

仅在用户明确授权导出凭据及私有数据时执行：

```sh
.venv/bin/python scripts/export_muse.py --include-private-credentials --output dist/XZBot-PRIVATE.zip
```

导出含现用 API key / Cookie、本地管理凭据、所有账号与 Profile、人设与音色、可编辑提示词与预设对话、背景资料与素材、群聊/记忆预设、记忆/历史/调用轨迹及订阅的私有 ZIP。兼容接口密钥包含当前配置的基础地址对应项，不枚举历史上其他地址的废弃凭据。运行数据通过 SQLite 备份 API 取快照；不会停止原实例。包不含 QQ 登录缓存、虚拟机、虚拟环境或平台二进制。ZIP 未加密，不可提交或公开分享。

## 接收方

Linux / WSL2，Python 3.12。先设置 `umask 077` 并解压到私有目录。以下在包的 `XZBot-muse/project/` 内执行，将临时 Python 环境放在完整性校验目录之外：

```sh
uv venv --python 3.12 ../../migration-env
uv pip install --python ../../migration-env/bin/python -r requirements-lock.txt
../../migration-env/bin/python scripts/restore_muse.py --verify-only
../../migration-env/bin/python scripts/restore_muse.py --destination <不存在的新目录>
```

恢复将凭据导入明确的私有文件后端、重写子账号目录，并使所有账号以 stopped 状态启动。Linux 会将所有账号的运行环境改为 external，避免继承 Mac 的 Lima 配置。原功能开关、角色资料、预设、订阅目标与游标、当天调用次数保留；DSH 后备关闭，因为有效凭据已转存。实际生效的默认音色/模型会转存，其他 Profile 的独立音色保持原值。

然后进入恢复出的新目录，确保 `uv` 在 PATH 中，执行：

```sh
python3 scripts/install_release.py --no-open
AZUSA_ROOT="$PWD" .venv/bin/python -m azusa_bot.cli serve
```

第一条只安装 Python 3.12、固定依赖和 AstrBot，并初始化缺失文件，不启动 QQ。第二条启动本机管理服务；可用服务器进程管理器运行它。随后按 [发行版说明](releases.md) 安装原生 NapCat，为每个账号下载各自的连接配置，使用不同的实例、端口及登录目录。启动管理服务和扫码不等于启用业务；保持所有账号暂停，直到接管条件满足。不要将管理口令或 NapCat 令牌公开到互联网；服务器管理页可通过 SSH 隧道访问。

首次管理启动、依赖重建和 Git 同步见 [开始使用](getting-started.md)、[同步开发](synchronization.md)。这份导入脚本要求新目录，不能覆盖已有部署；如果已经 clone GitHub，将该 clone 留作干净源码目录，先在独立新目录恢复和验收。

Mac 仍使用原钥匙串。Linux/Windows 的 QQ 生命周期、端口/鉴权、媒体通道和可选 Gemini 运行环境需要接收方适配，不能直接沿用 Mac 应用入口。服务器可能要求扫码；过期 Cookie 或 API key 也不能靠导出恢复有效性。

正式接管前先停止旧主机同一账号，必要时重新导出停用后的最终快照。确保 QQ 身份、白名单、owner、文字/语音/图文回执正常后，再启用群参与和订阅。原迁移包包含什么以包内清单为准，不在日志中输出凭据正文。

验收时分别检查「收到消息」「实际在 QQ 看见回复」「程序记录发送回执」。接口在线不能代替真实收发验证；回执未确认也不能直接断言未送达，避免自动重发。`cloud_budget` 是账号级本地调用次数上限，在「模型与语音 → 调用额度」配置；不能把它当作供应商余额不足而反复更换 key。

迁移验证边界（2026-11-08）：合成数据的两账号、多 Profile、凭据导入、SQLite 快照、权限与完整性校验、Linux 环境重写已通过 `tests/test_migration.py`。目标 Linux 上的腾讯登录、真实收发和云服务有效性仍需 muse 部署后验证；完成后记录所用提交、服务器平台及实际验收结果，不要将尚未验证的项目写成成功。
