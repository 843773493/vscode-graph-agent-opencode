# src/clients/electron/preload

## 目录用途

预留 Electron preload 安全桥接边界，把经过白名单审查的原生能力提供给 renderer。

## 可修改内容

- `contextBridge` 暴露的最小类型化 API。
- renderer 与 main 之间的 IPC 请求和响应适配。

## 不可修改内容

- 不得暴露 Node.js、Electron 或任意文件系统 API 的全量对象。
- 不得放桌面页面组件、Gateway API 或 Workspace 业务逻辑。

## 规范

- 默认启用 context isolation，所有桥接能力都必须显式白名单化。
- `electron-web` 浏览器 parity 模式必须有无原生桥接时的明确行为。
