# 从源码安装、验证与构建

源码安装与制作发行包是两件事。前者可以直接运行并保留 Git 更新；后者把已提交的公开文件和本机平台 uv 打包。网页前端由 FastAPI 提供静态文件，无需 Node、npm 或单独的前端构建服务。

## 安装源码

安装 [Git](https://git-scm.com/downloads) 和固定版本 [uv 0.11.8](https://github.com/astral-sh/uv/releases/tag/0.11.8)。uv 自动准备 Python 3.12。克隆并进入项目：

```sh
git clone https://gitee.com/hanskrrr/XZBot.git
cd XZBot
uv run --no-project --python 3.12 scripts/install_release.py --no-open
```

安装器按锁文件安装核心依赖，并下载与校验固定提交的 AstrBot，保留已有配置。第一次需要访问 Python、PyPI 及上游依赖下载服务；本项目托管在 Gitee 不代表所有第三方依赖也托管于此。仅开发管理核心可加 `--core-only`，此时不能接 QQ。

启动：

| 系统 | 在项目目录执行 |
|---|---|
| Windows PowerShell | `.\.venv\Scripts\python.exe scripts/release_start.py` |
| macOS / Linux | `.venv/bin/python scripts/release_start.py` |

管理页默认 http://127.0.0.1:6196 。QQ 环境需按 [三端部署说明](releases.md) 单独安装与登录。开发目录保留 `.git`；网页更新器可以查新版本，但不会覆盖 Git 工作区。

源码升级：先在管理页备份，再执行 `git pull --ff-only`、重跑安装命令，然后重启核心和桥接。已有未提交修改时先审阅和提交/暂存，不使用 `reset --hard`。更新不会迁移另一台机器的 QQ 登录态；密钥和私人资料不通过 Git 同步。

## 在目标系统上制作发行包

必须分别在 Windows x64、Apple Silicon macOS、Linux x64 上构建。`--platform` 只是目标确认参数，不是交叉编译；不能在 Mac 上改成 windows 就声称得到 Windows 可用包。

1. 使用干净、已提交的源码树。检查版本号与拟发布标签一致，例如 `0.9.0rc5` 对应 `v0.9.0-rc.5`。
2. 执行上述完整安装；确认 `uv --version` 是 0.11.8。
3. 准备 uv 的两份许可证。macOS / Linux：

```sh
mkdir -p dist
curl --fail --location https://raw.githubusercontent.com/astral-sh/uv/0.11.8/LICENSE-MIT -o dist/uv-LICENSE-MIT
curl --fail --location https://raw.githubusercontent.com/astral-sh/uv/0.11.8/LICENSE-APACHE -o dist/uv-LICENSE-APACHE
.venv/bin/python scripts/check_repository.py --ref HEAD
.venv/bin/python scripts/build_release.py --platform macos
.venv/bin/python scripts/verify_release_archive.py --local
```

Linux 将 `--platform macos` 改为 `--platform linux`。Windows PowerShell：

```powershell
New-Item -ItemType Directory -Force dist | Out-Null
Invoke-WebRequest https://raw.githubusercontent.com/astral-sh/uv/0.11.8/LICENSE-MIT -OutFile dist/uv-LICENSE-MIT
Invoke-WebRequest https://raw.githubusercontent.com/astral-sh/uv/0.11.8/LICENSE-APACHE -OutFile dist/uv-LICENSE-APACHE
.\.venv\Scripts\python.exe scripts/check_repository.py --ref HEAD
.\.venv\Scripts\python.exe scripts/build_release.py --platform windows
.\.venv\Scripts\python.exe scripts/verify_release_archive.py --local
```

uv 不在 PATH 时为打包命令加 `--uv` 指定对应系统的 uv 可执行文件。输出在 `dist/release/`：ZIP、SHA256、验证报告。手动验证需加 `--local`，并确保 6196、6197、6199 未被占用；在单独构建机器上运行最省事。脚本会拒绝已占用端口，不会停止现有 Bot。验证脚本将实际 ZIP 解压到带中文和空格的新目录，重新安装并启动核心/桥接；不会登录 QQ、调用付费模型或读取工作区密钥。

## 发布与更新清单

汇总三个系统验证成功的 ZIP、SHA256 和 verification.json 到同一目录后执行：

```sh
python scripts/build_update_feed.py dist/release
```

在本项目 [Gitee 发行版](https://gitee.com/hanskrrr/XZBot/releases) 为对应源码标签建立 Release，填写版本说明，上传三个 ZIP、三个 SHA256 和三个验证报告。核对安装包完整后，**最后上传 `updates.json`**。更新器只选有更新清单的完整版本，不会使用尚未完成的上传。

清单必须和 ZIP 来自同一次已验证的构建：生成器会核对验证报告和文件哈希。清单内使用文件名，不含其他发布站地址。安装包仍需在目标系统验证，不能只生成一个清单就宣称可用。

新版本的“官方更新源”使用本项目 Gitee 公开 Release 接口，按平台与架构匹配附件，下载后再次核对 SHA256 和包内文件清单。下载者不需要私人令牌；候选版受管理界面的开关控制。已有自定义更新站保持原样。

## 自动发布与手动补传

维护者配置的 CI 会完成三端构建、验证和发布，不依赖任何聊天助手在线。上传速度由运行器到 Gitee 的网络决定；此次实测较慢，所以不能承诺自动上传比本机手动上传更快。

发布程序先处理小文件，再并行上传三个安装包。已存在且哈希一致的附件会跳过；同名内容不同则停止，不自动覆盖。连接停滞会终止，只有确认没有完整上传、目标也不存在完整附件时才自动再试一次。最终状态和错误码保存在流水线，失败不会发布可供更新器使用的半成品清单。

选择手动上传也可以：从构建产物取得三端 ZIP、校验文件、报告和 `updates.json`，按上一节的顺序上传到相同标签即可。之后再次运行自动发布会校验并复用这些附件，不会重复上传一致的文件。普通仓库附件限制请查 [Gitee 官方说明](https://help.gitee.com/repository/release/create)。

源码和发行版是不同内容：Git 拉取会更新代码，不会安装 ZIP。程序更新会备份应用文件并保留私有配置；聊天记录和凭据不进入公开发布流程。
