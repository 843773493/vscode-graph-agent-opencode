# src/clients/shared/mobile-web-ui

## 目录用途

预留移动 Web parity 客户端可复用的 React DOM 组件、布局片段和展示逻辑。

## 可修改内容

- 已证明由两个或更多移动 Web DOM 调用方复用的组件、Hooks 和样式。
- 与移动布局相关且不依赖具体应用入口的展示逻辑。

## 不可修改内容

- 不得加入 React Native 组件、Electron 宿主 API 或浏览器存储实现。
- 不得放具体页面入口、路由、Gateway 连接实例或真机测试逻辑。

## 规范

- 可以依赖 `../core` 和 `src/shared`，不得依赖 `mobile-web` 或 `mobile` 的具体入口。
- 只有存在真实复用调用方时才提取代码；单个移动 Web 客户端的组件先留在其自身源码目录。
