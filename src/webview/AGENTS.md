# src/webview

## 目录作用

VS Code 侧边栏 Webview 的 Host 侧入口代码。这里主要负责把后端状态整理好，再通过 HTML 模板注入到当前真正生效的 Webview UI（`src/webview-ui/dist`）。

如果你主要看当前生效链路，可以记成：

- `sidebarProvider.js`：跑在扩展 Host 侧，负责拿后端数据、维护状态、把状态发给 Webview
- `html.js`：只负责读取 HTML 模板、注入占位符，不写业务逻辑

## 可以修改

- `html.js`：侧边栏 HTML 模板加载逻辑
- `main.html`：侧边栏主 UI 的 HTML 模板
- `shell.html`：纯壳调试模式的 HTML 模板
- `sidebarProvider.js`：扩展 Host 端 Webview 提供者（状态管理、API 代理、SSE 事件监听）

## 不要修改

- 不要在此目录直接调用后端 API（通过 `shared/api.js` 转发）
- 不要在此目录定义共享常量（在 `shared/constants.js` 中定义）
- 不要把 Node.js 的服务端模块直接引入到 Webview 运行时代码里

## 约定

- `sidebarProvider.js` 是单一数据源，所有状态变更通过它同步到 Webview
- `html.js` 通过独立 HTML 文件生成页面骨架，注入 CSP nonce
- Webview 与 Host 之间通信使用 `vscode.postMessage` / `onDidReceiveMessage`，消息类型定义在 `shared/protocol.js`
- 当前侧边栏主 UI 来自 `src/webview-ui/dist`，`src/webview` 只负责 Host 侧承接与注入# src/webview

## 目录作用

VS Code 侧边栏 Webview 的 Host 侧入口代码。这里主要负责把后端状态整理好，再通过 `html.js` 注入到当前真正生效的 Webview UI（`src/webview-ui/dist`）。

如果你主要看当前生效链路，可以记成：

- `sidebarProvider.js`：跑在扩展 Host 侧，负责拿后端数据、维护状态、把状态发给 Webview
- `html.js`：只负责拼出 Webview 页面骨架，不写业务逻辑

## 可以修改

- `html.js`：侧边栏 HTML 模板
- `sidebarProvider.js`：扩展 Host 端 Webview 提供者（状态管理、API 代理、SSE 事件监听）

## 不要修改

- 不要在此目录直接调用后端 API（通过 `shared/api.js` 转发）
- 不要在此目录定义共享常量（在 `shared/constants.js` 中定义）
- 不要把 Node.js 的服务端模块直接引入到 Webview 运行时代码里

## 约定

- `sidebarProvider.js` 是单一数据源，所有状态变更通过它同步到 Webview
- `html.js` 中的 HTML 模板通过 `renderSidebarHtml` 函数生成，注入 CSP nonce
- Webview 与 Host 之间通信使用 `vscode.postMessage` / `onDidReceiveMessage`，消息类型定义在 `shared/protocol.js`
- 当前侧边栏主 UI 来自 `src/webview-ui/dist`，`src/webview` 只负责 Host 侧承接与注入
