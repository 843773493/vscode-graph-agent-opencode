# 目录用途

`src/terminal/` 存放独立的持久终端管理进程及其浏览器 attach 页面。该目录服务于本地开发运行时，由 `scripts/dev.mjs` 启动，不属于 `src/web` 主前端，也不属于 Python FastAPI 后端。

## 可修改内容

- 可以新增或修改终端后端、终端网页前端、终端协议与本地运行脚本。
- 可以维护与 `.boxteam/terminal-manager/` 状态文件相关的实现。
- 可以调整终端 attach/detach、输入输出、resize、kill 等本地终端交互逻辑。

## 不可修改内容

- 不要把 Python 业务服务或 FastAPI 路由实现放到这里。
- 不要在这里实现主聊天 UI 的资源视图；主 UI 仍由 `src/web` 负责。
- 不要把终端状态写到 workspace 以外的位置。

## 规范

- 使用 ESM，通过 `import`/`export` 编写 Node.js 代码。
- 终端错误必须显式返回或抛出，不要静默失败。
- 后端默认监听 `127.0.0.1:8012`，前端默认监听 `127.0.0.1:8013`。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
