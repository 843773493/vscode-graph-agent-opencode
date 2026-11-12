# 目录用途

`scripts/release/` 存放面向开发者和 CI 的平台发布打包入口。

# 可修改内容

- 可以维护 Linux、Windows、Windows 交叉打包及已授权 Electron 容器的发布命令入口。
- 可以编排 `packaging/runtime/` 中的构建与验证实现。

# 不可修改内容

- 不在这里实现运行时构建细节、平台模板或 Agent 业务逻辑。
- 不把 release asset、tarball、ZIP 或其他构建产物写入源码目录。

# 规范

- 正式发布只从主 worktree 的 `out/worktrees/release/` 中明确提交或标签构建；`source-policy.mjs` 核对受管记录、detached SHA、干净源码和子模块，不导入根/dev 的未提交补丁和开发 Home。先通过 `bun run worktree create release <ref>` 创建，发布入口不自动切换 checkout。
- 发布支持基线取根 `AGENTS.md` 定义的两次用户完整验收发布提交，目录名、Agent 测试通过和 `HEAD~1` 不替代验收证据。候选发布从 `R_current` 评估升级；正式安装、发布与远端切换按任务授权执行。
- 根或实验 checkout 定向构建验证须显式传 `--development`，这些产物不能直接作为正式发布物；正式安装入口不接受此选项。`policy.mjs` 登记用户已验收且已发布的提交与证据，列出最近两条并生成迁移差异审查清单，不自动迁移。细节见 [worktree 与发布迁移约定](../../docs/architecture/worktree-and-release-policy.md)。
- JavaScript 脚本使用 ESM，失败时保留底层命令的明确退出信息。
- 所有产物写入 `out/packaging/`，版本和下载资源遵守 `packaging/runtime/` 的固定清单。
- 本地与 CI 使用相同的 `package.json` 发布入口。
- Electron 正式回归复用同一发布入口，通过 `--output-root` 将临时构建与归档写入正式测试镜像的 runtime，不复用开发应用 bundle。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
