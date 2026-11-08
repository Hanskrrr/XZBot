# 私有迁移导出与恢复

本文件是交接入口。先核对代码版本、[平台支持](platform-support.md) 和目标机器；恢复成功不代表 QQ 端到端部署完成。

仅在用户明确授权导出凭据及私有数据时执行：

```sh
.venv/bin/python scripts/export_muse.py --include-private-credentials --output dist/XZBot-PRIVATE.zip
```

导出含现用 API key / Cookie、本地管理凭据、所有账号与 Profile、角色资料、记忆/历史及订阅的私有 ZIP。运行数据通过 SQLite 备份 API 取快照；不会停止原实例。包不含 QQ 登录缓存、虚拟机、虚拟环境或平台二进制。ZIP 未加密，不可提交或公开分享。

## 接收方

Linux / WSL2，Python 3.12。先设置 `umask 077` 并解压到私有目录。以下在包的 `XZBot-muse/project/` 内执行，将临时 Python 环境放在完整性校验目录之外：

```sh
uv venv --python 3.12 ../../migration-env
uv pip install --python ../../migration-env/bin/python -r requirements-lock.txt
../../migration-env/bin/python scripts/restore_muse.py --verify-only
../../migration-env/bin/python scripts/restore_muse.py --destination <不存在的新目录>
```

恢复将凭据导入明确的私有文件后端、重写子账号目录，并使所有账号以 stopped 状态启动。原功能开关、角色资料和订阅目标保留；DSH 后备关闭，因为有效凭据已转存。首次管理启动、依赖重建和 Git 同步见 [开始使用](getting-started.md)、[同步开发](synchronization.md)。

Mac 仍使用原钥匙串。Linux/Windows 的 QQ 生命周期、端口/鉴权、媒体通道和可选 Gemini 运行环境需要接收方适配，不能直接沿用 Mac 应用入口。服务器可能要求扫码；过期 Cookie 或 API key 也不能靠导出恢复有效性。

正式接管前先停止旧主机同一账号，必要时重新导出停用后的最终快照。确保 QQ 身份、白名单、owner、文字/语音/图文回执正常后，再启用群参与和订阅。原迁移包包含什么以包内清单为准，不在日志中输出凭据正文。
