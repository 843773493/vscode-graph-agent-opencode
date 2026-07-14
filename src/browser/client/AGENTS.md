# 目录用途

`src/browser/client/` 存放独立浏览器 attach 网页。用户从主前端资源视图打开该页面后，可以连接指定浏览器页面、断开连接、输入 URL、点击远程屏幕并查看实时画面。

# 可修改内容

- 可以新增或修改 HTML、CSS、浏览器端 JavaScript。
- 可以调整浏览器 attach/detach 交互、状态提示、错误展示和远程屏幕控制。
- 可以维护原生浏览器 API 与 WebSocket 协议集成。

# 不可修改内容

- 不要把主聊天 UI 或 React 状态管理写到这里。
- 不要依赖 VS Code webview 宿主能力。
- 不要把浏览器端代码写成需要构建才能运行的形式。

# 规范

- 使用浏览器原生 JavaScript，避免引入构建步骤。
- UI 必须显示连接状态和错误信息。
- attach/detach 不应关闭后台浏览器页面。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
