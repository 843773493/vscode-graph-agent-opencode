# src/clients

## 目录用途

`src/clients/` 按客户端运行面组织源码。`workbench/` 是 Web 与 Electron 共用的完整 Code OSS 工作台，`web/` 提供复用的 React 会话产品；其余运行面包括 `electron/`、`electron-web/`、`mobile/` 和 `mobile-web/`，以及共享的 `core/`、`web-ui/` 和可选 `mobile-web-ui/` 层。

## 可修改内容

- `workbench/` 中的唯一工作台入口与外层布局，以及 `web/` 中的 React 会话产品。
- `electron/` 中的 main/preload 原生宿主边界。
- `electron-web/` 中可在浏览器运行的 Electron renderer parity 客户端。
- `mobile/` 中的 React Native 客户端。
- `mobile-web/` 中可在浏览器运行的移动布局 parity 客户端。
- `shared/` 中已有真实复用需求的核心逻辑、桌面 DOM 展示层和移动 Web DOM 展示层。
- 各目录的职责说明和 TODO。

## 不可修改内容

- 不要把 Electron main/preload、React Native 原生模块或移动 Web parity 客户端实现混入其他运行面。
- 不要让共享层反向导入具体客户端入口。
- 不要把 Workspace 后端或辅助服务放入客户端目录。

## 规范

- 外层导航、标签、Grid、命令和设置在 `workbench/`，聊天及会话资源产品在 `web/`；不得恢复旧 App 外壳。新增原生能力必须明确其运行面。
- `shared/core` 不依赖任何 UI 或平台；`shared/web-ui` 只放桌面 DOM；`shared/mobile-web-ui` 只放移动 Web DOM。
- `electron-web` 与 `mobile-web` 只能验证浏览器可运行的非原生功能，不能宣称完成原生端验证。
- 新增源码子目录必须有四段式 `AGENTS.md`。
