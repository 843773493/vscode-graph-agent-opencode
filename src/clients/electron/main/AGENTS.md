# src/clients/electron/main

## 目录用途

预留 Electron 主进程边界，负责应用生命周期、窗口管理和经过审查的系统集成。

## 可修改内容

- Electron 主进程入口、窗口生命周期和白名单化的系统能力。
- 与 `preload/`、`electron-web/` 之间的明确宿主契约。

## 不可修改内容

- 不得放 React DOM 页面、Workspace 业务逻辑或 Gateway 业务规则。
- 不得绕过 `preload/` 向 renderer 暴露 Node.js 或 Electron 全量 API。

## 规范

- 通过最小 IPC 接口连接 `preload/`，不得让 renderer 直接依赖 Node.js。
- 原生能力必须有独立的 Electron 测试，不能由 `electron-web` 浏览器测试代替。
