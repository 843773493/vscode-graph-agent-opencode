---
name: manage-cross-platform-dev-targets
description: Manage and develop this repository's isolated Linux, Windows, and Docker development targets through validated JSONC configuration, Git snapshots, SSH actions, lifecycle commands, tests, and artifact collection. Use when Codex needs to provision, synchronize, bootstrap, start, stop, restart, inspect, test, or collect from a cross-platform target, diagnose target orchestration, or modify files under tools/cross-platform-development-targets/ and scripts/cross-platform-development-target.mjs.
---

# 管理跨平台开发目标

通过仓库统一入口管理隔离开发目标。复用现有 ESM 编排和平台动作，不另写临时 SSH、Docker 或 PowerShell 流程。

## 使用统一入口

始终从项目根目录运行：

```bash
bun run scripts/cross-platform-development-target.mjs <command> <target-id> [options]
```

默认读取 `${BOXTEAM_HOME:-~/.boxteams-dev}/config/cross-platform-development-targets.jsonc`。仅在用户指定其它配置时使用 `--config <path>`；配置也可由 `BOXTEAM_CROSS_PLATFORM_TARGET_CONFIG` 指定。

支持以下命令：

| 命令 | 用途 | 关键约束 |
| --- | --- | --- |
| `provision` | 创建 Docker Linux 目标 | 仅允许 `provisioner=docker`；`--rebuild` 会重建镜像 |
| `sync` | 创建并推送宿主机工作区快照 | 用 `--activate` 同步后立即激活；默认加 `--no-env` |
| `activate` | 激活目标上最近的快照 | 目标仓库有本地修改时必须失败 |
| `bootstrap` | 安装 `.venv`、Bun 与浏览器依赖 | 仅在快照已激活后执行；按需加 `--submodules` |
| `start` / `stop` / `restart` | 管理服务 | 明确选择 `--profile development` 或 `installed` |
| `status` | 查看目标服务状态 | 把它作为任何修复前后的只读检查 |
| `test` | 在目标上运行测试 | 测试命令放在 `--` 后，禁止拼进管理动作 |
| `shell` | 打开目标 shell 或执行显式命令 | 只在确有交互需求时使用 |
| `collect` | 下载目标产物归档 | 默认输出到 `out/cross-platform-dev-targets/<target-id>/collected/` |

## 执行工作流

1. 确认用户指定的 target、profile 和目标任务。不要从示例猜测真实 VMware 地址、Windows 用户名或盘符。
2. 先运行 `status`。若配置或 SSH 校验失败，直接报告目标、平台和失败阶段。
3. Docker 目标尚未创建时运行 `provision`；外部 Linux/Windows 目标不得自动 provision。
4. 运行 `sync <target-id> --activate --no-env` 推送当前工作区快照。只有用户明确授权把项目 `.env` 同步到该目标时，才省略 `--no-env`；不得输出 `.env` 内容或真实凭据。
5. 首次使用或锁文件变化后运行 `bootstrap`。
6. 运行所需的 `start`、`test` 或其它动作；需要隔离运行目录时显式传递 `--boxteam-home` 和 `--workspace` 绝对路径。
7. 再运行 `status`，并按需运行 `collect --output <path>`。仓库测试产物遵循项目 `out/tests/` 隔离规则；一般目标产物使用命令默认目录。

典型开发目标流程：

```bash
bun run scripts/cross-platform-development-target.mjs status docker-debian
bun run scripts/cross-platform-development-target.mjs sync docker-debian --activate --no-env
bun run scripts/cross-platform-development-target.mjs bootstrap docker-debian
bun run scripts/cross-platform-development-target.mjs restart docker-debian --profile development
bun run scripts/cross-platform-development-target.mjs status docker-debian --profile development
```

## 保持安全边界

- 保持 SSH host key 校验开启；不得使用 `StrictHostKeyChecking=no`、空 known-hosts 文件或等价绕过。
- 不提交、不打印、不嵌入真实 SSH 私钥、密码、`.env`、Gateway token 或实际 VMware 地址。
- 不把宿主机项目根目录挂载为 Docker 目标仓库。通过快照和 SSH 同步源码。
- 不在目标工具中实现 Gateway、Agent 或工作区后端业务逻辑。
- 删除或覆盖前，确认目标是配置声明的精确专用路径。让平台脚本快速失败，不伪造成功。
- 不用测试代码向 Linux/Windows 管理脚本注入任意 shell 程序；优先使用结构化 action。

## 修改目标工具

修改 `tools/cross-platform-development-targets/` 前，读取 [references/platform-rules.md](references/platform-rules.md) 中对应平台规则，并继续遵循目标目录及其子目录的 `AGENTS.md`。

宿主机 JavaScript 始终使用 ESM，并基于运行时工作目录或显式项目根目录解析路径。配置结构变化时同步修改 `target.schema.json`、示例和测试；平台错误必须包含 target、platform 与 stage。

修改后至少运行：

```bash
bun test scripts/cross-platform-development-target.test.mjs
```

对改过的平台脚本再执行可用的语法检查；没有真实 Windows VMware 时，保留明确 TODO，不声称已经完成 Windows 实机验证。
