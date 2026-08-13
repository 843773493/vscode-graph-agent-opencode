# src/clients/shared/web-ui

## 目录用途

预留纯 Web 与未来 Electron renderer 可复用的 React DOM 组件和展示逻辑。

## 可修改内容

- 已证明跨 React DOM 客户端复用的组件、Hooks 和样式。

## 不可修改内容

- 不得导入具体客户端应用入口、路由或浏览器专属数据源。
- 不得加入 React Native 组件或 Electron/VS Code 宿主 API。

## 规范

- 可以依赖 `../core` 和 `src/shared`，不得反向依赖 `clients/web`。
- 当前没有第二个 React DOM 调用方时，优先留在纯 Web 客户端。
