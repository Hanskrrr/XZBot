# XZBot 0.9.0 RC6

更新设置默认显示“官方更新源”，不在界面的地址输入框或示例中展示个人仓库地址。后台继续通过原 GitHub Releases 获取更新，无需重新配置；保存检查、下载等开关不会清空更新源。

需要其他下载站时选择“自定义更新源”，再填写 HTTPS 清单或 GitHub Releases API 地址。已有自定义设置保留，支持切换回官方。

这次调整的是界面展示，不改变实际下载来源，也不提供发布者匿名性。保留 RC5 的角色、群聊、引用、订阅和更新校验能力；私人密钥、资料及 QQ 登录态不进入发行包。

Windows x64、macOS Apple Silicon、Linux x64 各附 ZIP、SHA256 与原生安装验证报告。首次启动需要联网安装依赖；QQ/NapCat 仍需单独安装并扫码。完整步骤见包内 docs/user-guide.md 与 docs/releases.md。
