# 目录用途

`src/browser/` 存放独立的可附加浏览器管理进程及其浏览器 attach 页面。该目录服务于本地开发运行时，由 `scripts/dev.mjs` 启动，不属于 `src/web` 主前端，也不属于 Python FastAPI 后端。

# 可修改内容

- 可以新增或修改浏览器后端、浏览器网页前端、浏览器协议与本地运行脚本。
- 可以维护与 `.boxteam/browser-manager/` 状态文件相关的实现。
- 可以调整浏览器 attach/detach、远程屏幕、Playwright 操控和资源状态逻辑。

# 不可修改内容

- 不要把 Python 业务服务或 FastAPI 路由实现放到这里。
- 不要在这里实现主聊天 UI 的资源视图；主 UI 仍由 `src/web` 负责。
- 不要把浏览器状态写到 workspace 以外的位置。

# 规范

- 使用 ESM，通过 `import`/`export` 编写 Node.js 代码。
- 浏览器错误必须显式返回或抛出，不要静默失败。
- 独立后端默认监听 `127.0.0.1:8015`；`scripts/dev.mjs` 为了让 Windows Codex app 访问，会显式传入 `BOXTEAM_BROWSER_LISTEN_HOST`（默认 `0.0.0.0`）同时启动后端与前端。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
