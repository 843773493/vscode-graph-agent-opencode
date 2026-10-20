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
| 本地打包并安装当前 BoxTeam | `mask install release` |
| 安装用户级配置 | `bun run install:config` |
| 执行 package.json 脚本 | `bun run <script-name>` |
| 构建当前纯 Web 客户端 | `bun run build:web` |
| 查看测试套件矩阵 | `bun run test:matrix -- --list` |
| 运行指定测试套件 | `bun run test:matrix -- --suite=contracts-python` |
| 按 Node/Bun 测试框架运行 Browser、Terminal 的 JS 单测 | `bun run test:matrix -- --suite=unit-workspace-services` |
| 验证 Browser 动态元素引用与点击（真实 Chromium） | `bun run test:matrix -- --suite=integration-browser-element-references` |
| 验证 Browser 表单检查点捕获与恢复（真实 Chromium） | `bun run test:matrix -- --suite=integration-browser-checkpoint-form-state` |
| 运行 Web E2E 全目录（Section 8 默认场景） | `BOXTEAM_E2E_SECTION8_FOCUS=default bun run test:matrix -- --suite=e2e-web` |
| 运行 Web Section 8 默认场景集 | `BOXTEAM_E2E_SECTION8_FOCUS=default bun run test:matrix -- --suite=e2e-web-basic-chat-tool-loop` |
| 聚焦附件复制 Web 场景 | `BOXTEAM_E2E_SECTION8_FOCUS=copy_missing_attachment bun run test:matrix -- --suite=e2e-web-basic-chat-tool-loop` |

### Python — uv

| 用途 | 命令 |
|------|------|
| 运行脚本 | `uv run python <script.py>` |
| 添加依赖 | `uv add <package>` |
| 移除依赖 | `uv remove <package>` |
| 同步依赖（首次/拉取后） | `uv sync` |
| 运行单元测试 | `bun run test:matrix -- --suite=unit-python` |
| 运行 lint | `uv run ruff check .` |

> 当前只开发 `src/clients/web/` 纯 Web 客户端。Electron、React Native 及其 parity 客户端仅保留清晰的运行面边界。首次运行请分别执行根目录 `bun install` 与 `bun install --cwd src/clients/web`。

`mask install release` 会检查当前平台的本地打包指纹；产物缺失或源码变化时，先重新打包，再从本地 CLI 与 runtime tarball 安装，不从公共 npm registry 获取 BoxTeam 包。安装完成后运行 `boxteam start`；安装版 Launcher 会启动内置 Gateway，Gateway 再启动 Workspace 后端并托管打包后的 Web UI，不会启动 Vite dev server。

## 工作区 API 的显式身份

直接调用 Workspace 后端时，使用 `/api/v1/workspaces/{workspace_id}/...`，例如 `/api/v1/workspaces/{workspace_id}/sessions`。这里的 `workspace_id` 是后端已挂载工作区的稳定 UUID。也可以在 `/api/v1/...` 请求中提供等价的 `X-BoxTeam-Workspace-Id` 请求头；同时提供路径和请求头时，两者必须一致。

旧的无工作区路径前缀、也无身份请求头的后端调用会被拒绝；后端不再用激活工作区或 `WORKSPACE_ROOT` 补齐身份。缺少或非法身份返回 400，两载体冲突返回 400，合法但未挂载的 UUID 返回 404。客户端应在调用前固定目标，不能靠切换激活态改变已指定的工作区。

浏览器经过 Gateway 的同源 `/api/v1/...` 调用继续由 Gateway 选路并显式注入后端 UUID。Gateway 注册表中的 target ID 用于控制面选路，不能替代直接后端请求的 UUID。Gateway 自有接口仍为 `/api/gateway/*`。当前接口合同以相应服务的 OpenAPI 为准。

## 测试分层

- `tests/unit/`：单模块或纯函数测试。
- `tests/contracts/`：HTTP、SSE、生成类型和辅助服务公开协议契约。
- `tests/integration/`：关键链路含 stub、fake、mock、固定响应、替代服务或替代运行面的组合测试。
- `tests/e2e/`：不含替身的真实进程、真实传输和真实外部依赖链路。

缺少 E2E 真实前置条件时必须明确失败、跳过或报告 `UNMET_PREREQUISITE`，不得自动运行 Integration 替身版本。
