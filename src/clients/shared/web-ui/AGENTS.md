# src/clients/shared/web-ui

## 目录用途

存放浏览器 `web` 与 Electron renderer `electron-web` 共享的桌面布局 React DOM 组件和展示逻辑。

## 可修改内容

- 已证明跨 `web` 与 `electron-web` 复用的桌面组件、Hooks 和样式。

## 不可修改内容

- 不得导入具体客户端应用入口、路由或浏览器专属数据源。
- 不得加入 React Native 组件或 Electron/VS Code 宿主 API。

## 规范

- 可以依赖 `../core` 和 `src/shared`，不得反向依赖 `clients/web` 或 `clients/electron-web`。
- `web` 与 `electron-web` 的应用入口、路由和运行时适配不放入本目录。
