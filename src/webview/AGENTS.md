# src/webview

## 目录作用

VS Code 侧边栏 Webview 的前端代码。包含 HTML 模板渲染、前端交互逻辑（sidebarApp.js）和扩展 Host 端 Webview 提供者（sidebarProvider.js）。

## 可以修改

- `html.js`：侧边栏 HTML 模板和 CSS 样式
- `sidebarApp.js`：Webview 前端应用（DOM 操作、事件处理、消息收发、Markdown 渲染）
- `sidebarProvider.js`：扩展 Host 端 Webview 提供者（状态管理、API 代理、SSE 事件监听）

## 不要修改

- 不要在此目录直接调用后端 API（通过 `shared/api.js` 转发）
- 不要在此目录定义共享常量（在 `shared/constants.js` 中定义）
- 不要在 `sidebarApp.js` 中引入 Node.js 模块（运行在 Webview 沙箱中）

## 约定

- `sidebarProvider.js` 是单一数据源，所有状态变更通过它同步到 Webview
- `sidebarApp.js` 使用乐观更新（optimistic update），在后端确认前先显示本地状态
- `html.js` 中的 HTML 模板通过 `renderSidebarHtml` 函数生成，注入 CSP nonce
- Webview 与 Host 之间通信使用 `vscode.postMessage` / `onDidReceiveMessage`，消息类型定义在 `shared/protocol.js`
- Markdown 渲染在 `sidebarApp.js` 中实现，支持代码块、标题、列表、引用、行内格式
- 代码块操作按钮支持：复制、插入光标、替换选中、终端执行、新建文件、查看差异