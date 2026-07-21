## Context

当前 Docker Gateway E2E 通过 `tools/docker-compose.gateway-test.yml` 把宿主机仓库挂载到固定 `/workspace/vscode-graph-agent-opencode`，`scripts/gateway-docker-target.mjs` 和 `tests/e2e/gateway/gateway_docker.py` 又共同依赖该路径与 `.venv_docker_<os>`。这种方式共享宿主机工作区、Git 状态和生成的 runtime manifest，不能代表一台独立 Linux/Windows 开发机，也无法复用于 VMware SSH 目标。

目标环境需要同时满足三种用途：作为可人工 SSH 或本地操作的完整源码开发机；运行跨平台兼容性 E2E；运行纯净安装发行版并被其他 Gateway 按真实联邦流程连接。项目已有开发与安装 Home 隔离约定，因此目标端需要同时持久化 `.boxteams-dev` 与 `.boxteams`，而不是为测试发明第三套长期状态模型。

## Goals / Non-Goals

**Goals:**

- 让 Docker、Linux VM 和 Windows VM 都表现为拥有完整普通 Git 仓库的独立开发端。
- 在不碰宿主机真实 index/分支的情况下，经 SSH 增量同步当前脏工作区快照。
- 保护目标端人工开发内容，任何自动激活都以目标工作区干净为前提。
- 统一目标配置与宿主机命令表面，把平台差异收敛到 Linux shell 和 Windows PowerShell 适配器。
- 让 Docker 的仓库、运行环境、双 BoxTeam Home、SSH 状态和产物跨容器重建持久存在。
- 让目标既能运行正式隔离 E2E，也能用于手工功能测试和真实 Gateway 联邦连接。

**Non-Goals:**

- 不在本变更中实现 Windows 兼容性测试用例或自动创建 VMware 虚拟机。
- 不改变 Gateway 联邦协议、认证范围、工作区 API 或安装包格式。
- 不实现宿主机与目标端的双向自动合并，也不自动处理目标端本地修改。
- 不同步已忽略文件、子模块工作树内容、Git LFS 外部对象或开发机密；`.env` 仅走独立 SSH 文件传输。
- 不允许同一运行配置、同一 `BOXTEAM_HOME` 同时启动多个 Gateway。

## Decisions

### 1. 每个目标只有一个完整普通仓库

目标端使用普通非 bare 仓库，例如 Linux 的 `/opt/boxteam-dev/repository` 或 Windows 配置指定的绝对目录。该仓库可直接编辑、切分支、运行工具和提交代码，不再维护“接收用 bare 仓库 + 测试 worktree”两层结构。

宿主机推送到 `refs/boxteam/snapshots/<snapshot-id>` 等非检出引用，目标端脚本再显式激活到 `boxteam-host-snapshot` 托管分支。直接推送当前检出分支会受到 `receive.denyCurrentBranch` 限制，也会把传输与工作区破坏耦合，因此不采用。

### 2. 使用临时 Git index 制作脏工作区提交

宿主机管理脚本创建临时 index，先 `read-tree HEAD`，再加入已跟踪变化、删除和未忽略的未跟踪文件，最后使用 `write-tree` 与 `commit-tree` 创建以当前 `HEAD` 为父提交的快照。这样产生的 Git 对象可以由原生 SSH push 增量协商，同时不会 checkout、切分支、stash 或修改真实 index。

快照前输出文件数量、总体积和变更摘要；已初始化子模块若为脏状态则快速失败，因为主仓库提交只能记录 gitlink。未初始化的 `reference_repo/*` 默认保持未初始化，需要时由显式 `bootstrap --submodules` 单独处理。

### 3. 推送与激活是两个阶段

`sync` 先更新目标快照引用；`sync --activate` 或后续 `activate` 才操作目标工作区。激活前运行完整 `git status --porcelain --untracked-files=all` 检查，发现任何非忽略变化立即失败。第一版不提供自动 stash、自动 clean 或隐式 force；这符合本项目快速失败原则，也允许目标端安全承担独立开发用途。

### 4. `.env` 使用同一 SSH 配置单独传输

真实目标配置位于宿主机开发 Home 的 config 下且不提交，例如 `${BOXTEAM_HOME:-~/.boxteams-dev}/config/cross-platform-development-targets.jsonc`。其中 SSH identity 与 known-hosts 路径供 Git、远程命令和文件复制共同使用，禁止关闭 host key 校验。

`.env` 上传到目标仓库同目录的唯一临时文件，宿主机和目标分别计算 SHA-256；一致后 Linux 使用 rename，Windows 使用同卷 `Move-Item` 替换，并设置仅目标用户可读的权限或 ACL。日志只记录路径、字节数和哈希，不记录内容。目标专属的 `BOXTEAM_HOME`、Python 路径与测试隔离路径由启动进程环境显式覆盖，不改写复制来的 `.env`。

### 5. 目标配置由公共 schema 驱动，平台动作分层

宿主机入口采用易读的 ESM 脚本 `scripts/cross-platform-dev-target.mjs`，命令包括 `provision`、`sync`、`bootstrap`、`start`、`stop`、`restart`、`status`、`shell`、`test` 和 `collect`。公共工具放在 `tools/cross-platform-dev-targets/`，包含目标示例与 schema、Docker provisioner、Linux 管理脚本和 Windows PowerShell 管理脚本；新增源码子目录按项目规则配置 `AGENTS.md`。

配置显式区分 `platform: linux | windows` 与 `provisioner: docker | external`。Docker 只负责容器生命周期，其余操作和外部 VMware 一样经 SSH 进入目标，避免 E2E 再直接调用 `docker exec`。平台适配器接收结构化操作和参数，不接收由测试拼接的任意 shell 片段。

### 6. Docker 把目标状态持久化到 out 专用目录

每个 Docker 目标使用：

```text
out/cross-platform-dev-targets/<target-id>/
├── repository/
├── home/
├── caches/
├── ssh/
└── artifacts/
```

Compose 分别把这些目录挂载到明确的容器路径，不再挂载宿主机项目根目录。持久 SSH host key 和 authorized keys 防止普通容器重建改变目标身份。镜像或 entrypoint 使用显式 UID/GID 创建 SSH 用户并校验目录所有权，避免 root 与宿主机用户交替产生不可写文件。

### 7. 每个目标仓库构建标准 `.venv`

`bootstrap` 在目标仓库运行 `uv sync --frozen` 和 Bun 的锁定依赖安装。Linux 只使用 `.venv/bin/python`，Windows 只使用 `.venv\Scripts\python.exe`。目标记录构建平台、Python 主次版本和锁文件摘要；持久环境与当前镜像/VM 不匹配时明确删除并重建目标自己的 `.venv`。旧 `.venv_docker_debian13` 只在确认没有活动进程引用后删除，相关 find/wrapper 逻辑同步移除。

### 8. 开发与安装采用两个明确 profile

`development` profile 运行目标仓库源码与开发 runtime manifest，默认 `BOXTEAM_HOME=~/.boxteams-dev`，默认工作区为 `~/.boxteams-dev/boxteam_workspace`。`installed` profile 运行目标已经安装的发行版，默认使用 `~/.boxteams` 和 `~/.boxteams/boxteam_workspace`。profile 由管理命令选择，不能由 `.env` 中碰巧存在的值决定。

仓库正式 E2E 仍按测试 fixture 显式覆盖到 `out/tests/<测试路径>/` 对应的隔离 Home、工作区和产物路径；手工源码开发及常驻目标服务才使用 `.boxteams-dev`。两种 profile 若需要并行运行，调用者必须显式配置不冲突端口，否则实例锁或端口预检快速失败。

### 9. E2E 依赖目标接口而不是 Docker 细节

将 `tests/e2e/gateway/gateway_docker.py` 中可复用部分下沉为目标无关的 SSH fixture/客户端，Docker fixture 仅负责 provision 和选择目标。测试通过结构化接口请求启动服务、查询状态和收集产物，不再知道容器仓库绝对路径，也不拼接 `nohup`、`kill` 或 PowerShell。

新增 `tests/e2e/windows/` 目录及符合项目要求的 `AGENTS.md` 和包骨架，当前不写测试。后续 Windows 用例复用同一接口，通过 Windows adapter 运行。

## Risks / Trade-offs

- [目标仓库既用于自动化又允许人工开发，自动激活可能打断工作] → 推送和激活分离，脏状态一律失败，不提供隐式强制覆盖。
- [临时快照可能包含用户不希望传输的未跟踪文件] → 只包含 Git 未忽略文件，推送前展示摘要和大小；机密文件必须进入 `.gitignore`，`.env` 固定走独立通道。
- [主仓库 push 不包含子模块对象或 LFS 外部对象] → 默认不初始化参考子模块，脏子模块快速失败；需要这些资源时使用显式 bootstrap 并要求目标具备相应网络访问。
- [Docker bind mount 产生 UID/GID 权限冲突] → provision 时固定并验证目标用户身份，初始化阶段修正专用挂载目录而不操作宽泛路径。
- [持久 `.venv` 在镜像升级后引用旧解释器] → bootstrap 校验平台、Python 版本和锁摘要，不兼容时重建并报告原因。
- [Windows OpenSSH、PowerShell 与 POSIX 行为差异] → 目标配置显式声明平台，文件、进程和路径操作全部进入平台 adapter；公共层只传结构化参数。
- [持久 SSH 密钥扩大本地测试目标的攻击面] → 只接受配置的公钥、启用 host key 校验、默认不暴露 Gateway 控制端口，连接仍走现有联邦认证。

## Migration Plan

1. 新增目标 schema、示例、公共编排器、平台 adapter 和 Docker 持久化 provisioner，但暂时保留旧测试入口。
2. 创建 Docker 独立完整仓库，验证快照、`.env`、bootstrap、development profile 与 SSH 联邦连接。
3. 把 Gateway Docker E2E fixture 和用例迁移到目标接口，并确认正式测试产物仍写入各自 `out/tests/` 路径。
4. 停止引用旧 Docker 虚拟环境的进程，删除 `.venv_docker_debian13`，移除旧 Compose bind mount、wrapper 和固定路径。
5. 添加 Windows 测试目录骨架与 PowerShell adapter 静态验证；真实 VMware 配置由用户本地配置接入。

回滚时恢复旧 Docker Compose、脚本和 E2E fixture；`out/cross-platform-dev-targets/` 为忽略的持久数据，不参与源码回滚。删除旧 `.venv_docker_debian13` 后如需回滚，可由旧脚本重新执行 `uv sync` 生成。

## Open Questions

无阻塞性问题。真实 VMware 的地址、用户、Windows OpenSSH 安装状态和密钥路径属于部署配置，在接入具体机器时填写，不影响公共实现。
