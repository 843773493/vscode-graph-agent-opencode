# src/webview-ui

## 目录作用

`src/webview-ui/` 是 VS Code 侧边栏 Webview 的 React 前端源码。由 Vite 构建，产物输出到 `src/webview/dist/assets/`，由 `sidebarProvider.js` 通过 `webview.asWebviewUri()` 注入 webview。

如果你主要写后端，这里可以理解成“真正写页面的前端工程”：

- `main.tsx`：页面入口，负责把 React 挂到 `#root`
- `App.tsx`：页面总布局
- `hooks.tsx`：前端状态中心，负责和 Webview Host 通信
- `components/`：页面上的各个区块，比如聊天区、输入框、历史面板
- `utils/`：纯工具函数，比如 Markdown、格式化

## 可以修改

- `src/` 下的所有 React + TS 源码
- `vite.config.ts`、`package.json`、`tsconfig.json`

## 不要修改

- 不要在此目录引入 Node.js built-in 模块（运行在 Webview 沙箱中）
- 不要引入 Vite 以外的构建工具

## 约定

- React 18 + TypeScript + 纯 CSS（不引入 Tailwind）
- 通信协议与 `src/shared/protocol.js` 保持一致
- 组件逻辑就近组织，公共 hook 放在 `src/hooks.tsx`
- 工具函数放在 `src/utils/` 下
- Vite build 输出到 `dist/`，gitignore 中不跟踪 `node_modules/` 和 `dist/`
- Webview 里尽量不要引入 Node.js built-in 模块，因为它跑在浏览器沙箱里
