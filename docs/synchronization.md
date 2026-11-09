# Mac 与服务器同步开发

远程：`git@github.com:Hanskrrr/XZBot.git`。使用同一份源码，平台差异放在宿主适配模块中；不要维护互不合并的 Mac / Linux 永久副本。

## 日常流程

```sh
git fetch origin
git switch main
git pull --ff-only origin main
git switch -c linux-support
# 修改并测试；不复制运行数据到代码目录
git add <修改的代码路径>
git diff --cached --stat
.venv/bin/python scripts/check_repository.py --staged
git commit -m "Add Linux host lifecycle adapter"
git push -u origin linux-support
```

在 GitHub 提交 PR、检查变更并合并，再让另一台机器更新 main。运行目录有未提交改动时，先保存成自己的分支/提交；不要使用强制拉取、`reset --hard`、`clean -fdx` 或整目录覆盖。代码更新不等于自动重启已运行进程；更新依赖与数据库之前先备份，验证后只重载相关服务。

首次 clone 后安装本地钩子：`.venv/bin/python scripts/install_git_hooks.py`。Git 不会自动启用 clone 下来的 hooks；两边都需要执行一次。已有其他 hooks 配置时安装器会拒绝覆盖。

## 已拿到私有迁移包的接手 Agent

推荐先 clone 本仓库到单独源码目录，审阅平台支持和测试；私有包用于恢复运行配置/资料，不要把包里的整个旧 `project/` 覆盖进 Git。恢复出的账号、内容与数据库保留在各自忽略目录；正式把源码与运行目录结合前先备份并核对两边文件差异。

如果迁移目录本身没有 `.git`，不要直接 `git add .` 上传。可以在干净 clone 中完成平台适配，再将运行数据恢复/接入该 clone。将密钥只导入目标本地存储；之后更新代码不需要重填。

## 运行数据归属

每个 QQ 指定一台正式运行主机。另一台可以改代码、运行合成测试；不要双端登录同号并回复。聊天、记忆和订阅游标是持续变化的数据库，不以 Git 合并；需要搬机器时使用快照、停旧实例、恢复、核验后切换。

本次代码发布以远程初始化提交为基础，不包含旧的本机私人 Git 历史。旧历史只保存在本机忽略的备份目录中；不要把该 bundle 或旧分支推上远程。

## GitHub → Gitee 发行版

代码镜像和发行版附件是两条同步链路。已配置的 Gitee 镜像负责分支、提交与标签；仓库的 `Sync Gitee release` 工作流负责三端安装包、校验文件、验证报告及发行说明。只需一次配置 `GITEE_TOKEN` Secret；参见[源码构建手册](source-build.md#自动同步-release一次配置)。这项同步不包含本机 API key、QQ 登录缓存、聊天与记忆数据。
