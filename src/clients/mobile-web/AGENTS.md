# src/clients/mobile-web

## 目录用途

预留移动布局的浏览器 parity 客户端，用于在不连接真机或模拟器的情况下开发和测试 React Native 端约 90% 的非原生功能。

## 可修改内容

- 移动 Web 布局、React DOM 组件、页面状态和 Gateway API 交互。
- 适合浏览器的移动端测试入口和 mock 运行时。

## 不可修改内容

- 不得放 React Native 组件、原生模块、真机权限或模拟器控制逻辑。
- 不得把 parity 测试结果当作移动端真机或模拟器验证。
- 不得新增一套与 `mobile/` 不兼容的业务协议。

## 规范

- 复用 `shared/core` 和稳定协议；移动 Web DOM 组件需要跨客户端复用时放入 `shared/mobile-web-ui`。
- 与 `mobile/` 共享业务契约，但不强行共享 React DOM 和 React Native 组件。
