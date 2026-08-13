# src/clients/shared/core

## 目录用途

预留运行时无关的客户端核心模型、状态机和用例。

## 可修改内容

- 不依赖 UI 或平台运行时的纯 TypeScript 逻辑及其共置单元测试。

## 不可修改内容

- 不得依赖 React、DOM、Node.js、Electron、VS Code 或 React Native。
- 不得放 API 端点拼接、浏览器存储和具体页面状态。

## 规范

- 输入输出使用稳定协议类型；副作用由调用方注入。
- 当前没有真实复用需求时保持空目录，不创建占位实现。
