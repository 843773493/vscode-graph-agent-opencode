# src/clients/electron

## 目录用途

预留 Electron 桌面客户端的 `main/`、`preload/` 原生宿主边界；桌面 renderer 的非原生界面位于同级 `electron-web/`，并与 `web/` 共享桌面 DOM 层。

## 可修改内容

- `main/` 中的窗口、应用生命周期和系统能力规划。
- `preload/` 中的最小安全 IPC bridge 规划。

## 不可修改内容

- 未经独立 OpenSpec 和用户明确要求，不得新增 Electron 原生产品代码、依赖或测试。
- 不得复制 `web/` 或 `electron-web/` 的桌面 DOM 应用形成分叉。

## 规范

- renderer 使用 `clients/electron-web` 的非原生 UI；`main/preload` 只暴露最小、白名单化的安全桥接。
- 原生端测试必须覆盖 IPC、窗口和系统能力；不能由 `electron-web` 浏览器测试代替。
