# src/webview-ui/src

## 目录作用

这是 `src/webview-ui/` 里真正写 React 前端代码的地方。这里的代码会被 Vite 编译后，放进 VS Code Webview 里显示。

如果你主要会后端，这里可以理解成“页面代码本体”：

- `main.tsx`：页面入口，负责把 React 挂到 `#root`
- `App.tsx`：页面总布局，决定页面怎么分区
- `hooks.tsx`：前端状态中心，负责和 Webview Host 传消息、存状态
- `components/`：页面上的具体区块，比如聊天区、输入框、历史面板
- `utils/`：纯工具函数，比如 Markdown 渲染、文本格式化
- `vscode.ts`：Webview 和 VS Code 通信的小封装
- `types/`：前端类型定义

## 可以修改

- `App.tsx`
- `hooks.tsx`
- `main.tsx`
- `types.ts`
- `vscode.ts`
- `components/` 和 `utils/` 下面的前端文件
- `index.css`

## 不要修改

- 不要在这里引入 Node.js built-in 模块（Webview 沙箱里不能直接用）
- 不要在这里写后端业务逻辑
- 不要把共享协议和前端类型拆散到别处

## 约定

- 页面状态尽量走 `hooks.tsx`，不要每个组件自己私藏一份业务状态
- 组件只负责展示和局部交互，通用逻辑放到 hook 或 utils 里
- 共享类型要和 `src/shared/protocol.js` 保持一致；如果协议已废弃，优先删掉对应前端引用
- 新增页面能力时，优先先补类型，再补 UI，再补消息流
- 这里的代码默认运行在浏览器环境，不要依赖文件系统、进程、路径等 Node 能力
