# 从源码安装、验证与构建

源码安装与制作发行包是两件事。前者可以直接运行并保留 Git 更新；后者把已提交的公开文件和本机平台 uv 打包。网页前端由 FastAPI 提供静态文件，无需 Node、npm 或单独的前端构建服务。

## 安装源码

安装 [Git](https://git-scm.com/downloads) 和固定版本 [uv 0.11.8](https://github.com/astral-sh/uv/releases/tag/0.11.8)。uv 自动准备 Python 3.12。克隆并进入项目：

```sh
git clone https://github.com/Hanskrrr/XZBot.git
cd XZBot
uv run --no-project --python 3.12 scripts/install_release.py --no-open
```

也可将克隆地址换为 `https://gitee.com/hanskrrr/XZBot.git`；先核对镜像提交。安装器按锁文件安装核心依赖，并下载与校验固定提交的 AstrBot，保留已有配置。第一次需要访问 Python、PyPI、GitHub 下载服务；Gitee 镜像并不消除这些网络依赖。仅开发管理核心可加 `--core-only`，此时不能接 QQ。

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

## 自动发布与更新清单

`.github/workflows/release.yml` 在推送 `v*` 标签时运行三个原生系统的测试、打包、全新目录安装验证，全部通过后才发布。普通提交只跑源码隐私检查；Actions 的手动运行可验证三端构建，但不发布无标签版本。不要删除正在发布的标签；旧版若需修复，使用新的候选版本标签。

汇总三个系统验证成功的产物到同一目录后执行：

```sh
python scripts/build_update_feed.py dist/release
```

生成的 `updates.json` 和 ZIP 一起上传到同一版本的 GitHub Release。自动发布流程会完成这一步。清单不是替代安装测试：它要求验证报告成功且 ZIP 的 SHA256 一致。GitHub 更新源使用公开 Releases API，无需 PAT；预发布版受界面的候选版本开关控制。

## Gitee 镜像与发行版

关联 GitHub 或导入一次，不代表已配置持续同步。若账号提供镜像功能，在 Gitee 仓库「管理 → 仓库镜像管理」检查 **Pull、自动从 GitHub 同步** 以及对应 WebHook；界面与可用额度以 Gitee 当前账号为准。[官方镜像说明](https://help.gitee.com/repository/settings/sync-between-gitee-github)

镜像同步分支、标签和提交，不会执行 GitHub Actions，也不复制 Release 附件。Gitee 可以建立相同标签的发行版并上传三个已验证 ZIP、校验文件和报告，无需重新构建；当前官方说明的普通仓库单附件上限为 100MB。也可在 Gitee 发行说明链接到 GitHub 下载。[官方发行版说明](https://help.gitee.com/repository/release/create)

### 自动同步 Release（一次配置）

仓库已提供 **Sync Gitee release** 工作流。三端构建、验证及 GitHub 发布成功后会直接调用它；手工发布或修改 GitHub 发行说明也会触发。向 `main` 提交同步脚本或本工作流的修复时，会自动核对并补同步最新发行版。不能只依赖 `release` 事件：由 GitHub 内置 `GITHUB_TOKEN` 创建的发行版不会再次触发普通 Release 工作流。

1. 在 Gitee「设置 → 私人令牌」创建仅含仓库所需 **projects** 权限的令牌。令牌所属账号需要能发布目标仓库的 Release；不要放入聊天、源码、普通 Actions Variable 或 Bot 配置。
2. 在 GitHub 仓库「Settings → Secrets and variables → Actions → New repository secret」添加 **`GITEE_TOKEN`**。过期或撤销后在这里替换即可，读者下载不需要此令牌。
3. 默认同步到 GitHub 同名的 Gitee 仓库（用户名转小写）。不同路径时，添加 Actions **Variable** `GITEE_REPOSITORY`，值为 `用户名/仓库名`。这不是 Secret。
4. 首次补同步：打开 GitHub「Actions → Sync Gitee release → Run workflow」，选择 `main`，填写已有标签，例如 `v0.9.0-rc.6`；留空选最近发布的版本，包含候选版。勾选 `dry_run` 只校验 GitHub 附件，不上传、也不需要 Gitee 令牌。
5. 查看这次运行的 Summary：显示上传数量、已存在且校验一致的数量与 Gitee 发行版链接。未配置令牌时明确显示“同步尚未启用”，不会把 GitHub 发布判失败，也不代表已经同步。

每个版本复制 **3 个安装 ZIP + 3 个 SHA256 文件 + 3 个原生验证报告**，并复制标题、完整发行说明和候选版状态。`updates.json` 不复制，程序“官方更新源”继续使用 GitHub；Gitee 下载镜像与程序自动更新是独立的。

上传前验证三端包的哈希、原生验证报告及 GitHub 标签提交；最多等待约 6 分钟，让 Gitee 的代码镜像追上同一标签提交。上传后重新下载 Gitee 附件计算哈希；全部成功后才写入最终发行说明。在此之前新发行版标为候选版，保留完整发行说明，明确标注“附件镜像尚未完成”，并列出 GitHub 原始安装包备用链接；这些链接不代表 Gitee 附件已上传成功。

重复运行只补缺少的附件，同名且哈希相同则跳过；同名内容不同、重复附件或标签长期未同步会明确失败，不自动覆盖、删除文件或改写标签。同步在 GitHub 的 Ubuntu 托管运行器执行（只传文件，不重新构建三端包），使用较小的网络包并启用路径 MTU 探测，处理小请求正常但大文件连接停滞的情况。上传使用 runner 自带的 curl，小文件有 1 分钟、安装包有 5 分钟整体时限，并记录实际传输字节和速度；令牌只通过标准输入传入，不出现在命令参数中。上传回执丢失时先查询实际附件，避免重复上传。修复原因后对原运行选择 **Re-run failed jobs**，或按第 4 步补同步即可。镜像账户容量满时需要维护者自行处理；工作流不会自动清理旧版。

如果 Gitee 发布失败，GitHub 上已发布的版本仍可下载。Gitee 的仓库主页不是更新清单，不能直接填进更新框。脚本使用 [Gitee v5 官方 Release/附件接口](https://gitee.com/api/v5/swagger)，发布凭据仅通过 Secret 注入。
