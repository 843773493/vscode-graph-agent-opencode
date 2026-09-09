# 通用指令

## 常用命令

### JS/TS — Bun

仓库内优先使用本地 `tools/bun.exe`，避免依赖全局 bun。

| 用途 | 命令 |
|------|------|
| 运行脚本 | `bun run <script.js>` |
| 添加依赖 | `bun add <package>` |
| 移除依赖 | `bun remove <package>` |
| 安装所有依赖 | `bun install` |
| 安装纯 Web 依赖 | `bun install --cwd src/clients/web` |
| 安装已发布 BoxTeam | `bun run scripts/install/install-release.mjs` |
| 安装用户级配置 | `bun run install:config` |
| 执行 package.json 脚本 | `bun run <script-name>` |
| 构建当前纯 Web 客户端 | `bun run build:web` |
| 查看测试套件矩阵 | `bun run test:matrix -- --list` |
| 运行指定测试套件 | `bun run test:matrix -- --suite=contracts-python` |

### Python — uv

| 用途 | 命令 |
|------|------|
| 运行脚本 | `uv run python <script.py>` |
| 添加依赖 | `uv add <package>` |
| 移除依赖 | `uv remove <package>` |
| 同步依赖（首次/拉取后） | `uv sync` |
| 运行测试 | `uv run pytest` |
| 运行 lint | `uv run ruff check .` |

> 当前只开发 `src/clients/web/` 纯 Web 客户端。Electron、React Native 及其 parity 客户端仅保留清晰的运行面边界。首次运行请分别执行根目录 `bun install` 与 `bun install --cwd src/clients/web`。

`bun run scripts/install/install-release.mjs` 会直接从 npm 安装当前项目发布版本，平台 runtime 由 npm 的可选依赖自动安装。安装完成后运行 `boxteam start`；安装版 Launcher 会启动内置 Gateway，Gateway 再启动 Workspace 后端并托管打包后的 Web UI，不会启动 Vite dev server。

## 测试分层

- `tests/unit/`：单模块或纯函数测试。
- `tests/contracts/`：HTTP、SSE、生成类型和辅助服务公开协议契约。
- `tests/integration/`：关键链路含 stub、fake、mock、固定响应、替代服务或替代运行面的组合测试。
- `tests/e2e/`：不含替身的真实进程、真实传输和真实外部依赖链路。

缺少 E2E 真实前置条件时必须明确失败、跳过或报告 `UNMET_PREREQUISITE`，不得自动运行 Integration 替身版本。
