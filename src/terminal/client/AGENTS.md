# 目录用途

`src/terminal/client/` 存放独立终端 attach 网页。用户从主前端资源视图打开该页面后，可以连接指定终端、断开连接、输入命令并查看实时输出。

## 可修改内容

- 可以新增或修改 HTML、CSS、浏览器端 JavaScript。
- 可以调整终端 attach/detach 交互、状态提示和错误展示。
- 可以维护 xterm 相关前端集成。

## 不可修改内容

- 不要把主聊天 UI 或 React 状态管理写到这里。
- 不要依赖 VS Code webview 宿主能力。
- 不要把浏览器端代码写成需要构建才能运行的形式。

## 规范

- 使用浏览器原生 JavaScript，避免引入构建步骤。
- UI 必须显示连接状态和错误信息。
- attach/detach 不应杀死后台终端。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
