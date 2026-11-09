# src/workspace-services/browser

## 目录用途

`src/workspace-services/browser/` 存放 developer-tools 插件中的 Browser 工作区服务及其 attach 页面。不属于 `src/clients/web` 主前端，也不属于 Python FastAPI 后端；运行实例由 Gateway 插件服务生命周期管理。

## 可修改内容

- 可以新增或修改浏览器后端、浏览器网页前端与浏览器协议。
- 可以维护与 `.boxteam/browser-manager/` 状态文件相关的实现。
- 可以调整浏览器 attach/detach、远程屏幕、Playwright 操控和资源状态逻辑。

## 不可修改内容

- 不要把 Python 业务服务或 FastAPI 路由实现放到这里。
- 不要在这里实现主聊天 UI 的资源视图；主 UI 仍由 `src/clients/web` 负责。
- 不要把浏览器状态写到 workspace 以外的位置。

## 规范

- 使用 ESM，通过 `import`/`export` 编写 Node.js 代码。
- 浏览器错误必须显式返回或抛出，不要静默失败。
- Browser 资源通过 Gateway 插件服务入口访问；不得新增独立 Browser backend 或 attach 页面启动器。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
