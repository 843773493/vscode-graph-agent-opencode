# src/clients/shared

## 目录用途

存放能够被多个客户端真实复用的客户端代码；`core/` 是运行时无关的纯 TypeScript，`web-ui/` 是桌面 React DOM 复用层，`mobile-web-ui/` 是移动布局的 React DOM 复用层。

## 可修改内容

- 已有两个或更多明确调用方，或纯 Web 正在提取的稳定业务模型、状态转换与 DOM 组件。

## 不可修改内容

- 不要把 Electron main/preload、React Native 组件或 VS Code adapter 放入共享层。
- 不要放具体页面入口、路由、浏览器存储或平台宿主通信。

## 规范

- `web-ui` 和 `mobile-web-ui` 可以依赖 `core`，共享层不得反向依赖具体客户端入口。
- 只有复用关系明确后才提取，避免为未来假设制造抽象。
