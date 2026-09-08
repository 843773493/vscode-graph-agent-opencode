# src/clients/shared/core

## 目录用途

存放 Web、Electron renderer、移动 Web 和 React Native 都可复用的运行时无关客户端核心模型、状态机、协议和用例。

## 可修改内容

- 不依赖 UI 或平台运行时的纯 TypeScript 逻辑及其共置单元测试。

## 不可修改内容

- 不得依赖 React、DOM、Node.js、Electron、VS Code 或 React Native。
- 不得放 API 端点拼接、浏览器存储、具体页面状态或任何原生宿主调用。

## 规范

- 输入输出使用稳定协议类型；副作用由调用方注入。
- 输入输出使用稳定协议类型；副作用由各运行面注入。
