## 1. 目标配置与代码结构

- [x] 1.1 创建 `tools/cross-platform-development-targets/` 及 Linux、Windows、Docker 子目录，为每个新增源码目录补齐含“目录用途、可修改内容、不可修改内容、规范”四部分的 `AGENTS.md`
- [x] 1.2 添加可读的目标配置示例和 JSON schema，覆盖 target ID、platform、provisioner、SSH identity、known-hosts、远程 Home、仓库与持久目录，并拒绝未知字段和相对远程路径
- [x] 1.3 实现 `scripts/cross-platform-development-target.mjs` ESM 命令入口及 `provision/sync/activate/bootstrap/start/stop/restart/status/shell/test/collect` 参数解析，统一输出目标 ID、平台与阶段化错误
- [x] 1.4 添加配置加载与命令参数的 Bun 单元测试，确认真实配置、密钥和目标地址不会从示例或日志泄漏

## 2. Git 快照与安全环境同步

- [x] 2.1 实现基于临时 Git index 的脏工作区快照创建，包含删除和未忽略的未跟踪文件，同时保持宿主机分支、真实 index 与工作区字节不变
- [x] 2.2 实现子模块状态、快照文件数量与大小预检；脏子模块或异常大/不可读输入以明确错误停止，未初始化参考子模块保持未初始化
- [x] 2.3 通过带 identity 与 known-hosts 校验的 SSH Git URL 把快照增量推送到目标完整仓库的非检出 `refs/boxteam/snapshots/*` 引用
- [x] 2.4 实现目标工作区干净检查和显式 activate；脏目标列出路径并保持分支与文件不变，第一版不实现自动 stash、clean 或 force
- [x] 2.5 实现 `.env` 临时上传、双端 SHA-256 校验、Linux/Windows 原子替换和权限设置，确保日志不输出内容且失败时保留原文件
- [x] 2.6 添加快照、重复增量推送、脏目标拒绝、子模块拒绝及 `.env` 校验失败的隔离测试

## 3. 平台适配与独立运行时

- [x] 3.1 实现 Linux POSIX 管理脚本，只接受结构化动作参数，并支持仓库初始化、进程生命周期、健康状态、测试运行和产物收集
- [x] 3.2 实现 Windows PowerShell 管理脚本的同等操作，使用 Windows 路径、`.venv\Scripts\python.exe`、进程管理和 ACL，不依赖 `sh/nohup/kill`（保留真实 VMware 验证 TODO）
- [x] 3.3 实现 bootstrap：在目标完整仓库执行 `uv sync --frozen` 与 Bun 锁定依赖安装，并记录/校验平台、Python 主次版本和锁文件摘要
- [x] 3.4 实现 `development` 与 `installed` profile，分别显式选择 `.boxteams-dev/boxteam_workspace` 和 `.boxteams/boxteam_workspace`，同时允许正式 E2E fixture 覆盖到测试专用隔离路径
- [x] 3.5 添加 Linux 与 PowerShell adapter 的静态/单元测试，覆盖路径转义、profile 环境、端口冲突、过期 PID、环境失效重建和非零错误传播

## 4. 持久 Docker 开发目标

- [x] 4.1 重构 Dockerfile 与 Compose，在 `out/cross-platform-dev-targets/<target-id>/` 下分别挂载 repository、home、caches、ssh 和 artifacts，不再挂载宿主机仓库
- [x] 4.2 配置持久 OpenSSH host key、authorized keys、目标用户 Home 和显式 UID/GID，启动时校验专用持久目录所有权而不修改宽泛宿主机路径
- [x] 4.3 让 Docker provisioner 只管理容器生命周期，其余初始化、同步、启动和状态操作全部通过与外部 VM 相同的 SSH 目标接口
- [x] 4.4 验证容器删除并重建后完整 Git 仓库、`.venv`、`.boxteams-dev`、`.boxteams`、两个默认工作区和 SSH 身份仍然存在
- [x] 4.5 验证 Docker 目标可本地运行完整开发服务，并能被另一 Gateway 通过现有 SSH 联邦认证和协议协商导入

## 5. E2E 迁移与 Windows 骨架

- [x] 5.1 将 `tests/e2e/gateway/gateway_docker.py` 的通用部分重构为目标无关 SSH fixture/客户端，移除容器名、`/workspace/vscode-graph-agent-opencode` 和任意 POSIX 命令拼接
- [x] 5.2 迁移现有 Gateway Docker 路由与 Browser 资源 E2E，确保其工作区、`BOXTEAM_HOME`、日志和产物仍位于各自 `out/tests/<同名测试路径>/`
- [x] 5.3 新增 `tests/e2e/windows/AGENTS.md` 和包目录骨架，说明未来 VMware Windows 兼容性测试边界，本次不添加 Windows 测试用例
- [x] 5.4 添加目标接口的模拟 Linux/Windows 测试，证明同一 E2E 调用在不同 platform 下选择正确 adapter 且不泄漏 SSH 凭据

## 6. 旧逻辑清理与验收

- [x] 6.1 检查活动进程不再引用 `.venv_docker_debian13` 后删除该目录，并移除 `.venv_docker_*` 的 ignore、创建、查找、wrapper 和固定 Python 路径逻辑
- [x] 6.2 删除或替换旧 `gateway-docker-target.mjs`、Gateway 测试 Compose 和 Docker 固定路径入口，不保留双轨兼容层
- [x] 6.3 使用 Bun 对所有新增/修改 ESM 代码运行静态分析和单元测试，使用 `uv run pytest` 运行目标管理及 Gateway E2E 相关测试
- [x] 6.4 执行 Docker 独立目标全流程验收：provision、首次 sync、重复增量 sync、`.env` 更新、bootstrap、development 启停、容器重建、正式隔离 E2E、联邦连接和 collect
- [x] 6.5 运行 `openspec validate add-cross-platform-development-targets --strict`，核对实现与能力规格一致并记录任何尚需真实 VMware 环境验证的非阻塞项
