# src/clients/electron/main

## 目录用途

Electron 主进程边界，负责应用生命周期、窗口管理、固定存储与共享静态服务。

## 可修改内容

- Electron 主进程入口、窗口生命周期、存储位置及 renderer 关闭提交契约。

## 不可修改内容

- 不得放 React DOM 页面、Workspace 业务逻辑或 Gateway 业务规则。
- 不得绕过 `preload/` 向 renderer 暴露 Node.js 或 Electron 全量 API。
- 当前容器不需要 preload 或 IPC，不得新增原生文件系统、原生扩展或其他系统能力。

## 规范

- renderer 使用唯一共享 Web 工作台，不依赖 Node.js；主进程仅等待固定的前端关闭提交函数。
- 产物启动、窗口和持久化必须有真实 Electron 验证，不能由 `electron-web` 浏览器测试代替。
