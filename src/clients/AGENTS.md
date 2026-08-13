# src/clients

## 目录用途

`src/clients/` 按运行面组织客户端源码。当前只有 `web/` 是可开发、构建和验证的产品客户端；`electron/`、`mobile/`、`vscode/` 仅预留未来边界。

## 可修改内容

- `web/` 中的纯 Web 客户端。
- `shared/` 中已有真实复用需求的核心逻辑和 React DOM 展示层。
- 各目录的职责说明和 TODO。

## 不可修改内容

- 不要在本阶段实现 Electron、React Native 或新的 VS Code 客户端。
- 不要让共享层反向导入具体客户端入口。
- 不要把 Workspace 后端或辅助服务放入客户端目录。

## 规范

- 当前功能默认只落到 `web/`。
- 对未来客户端只考虑协议和纯模型不被浏览器 API 污染，不编写适配实现。
- 新增源码子目录必须有四段式 `AGENTS.md`。
