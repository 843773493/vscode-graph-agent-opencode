# src/clients/electron

## 目录用途

Electron 桌面客户端的最小宿主边界；桌面 renderer 入口位于同级 `electron-web/`，与 Web 使用相同的 Workbench 源码。

## 可修改内容

- `main/` 中的窗口、应用生命周期、固定存储和共享静态服务启动。

## 不可修改内容

- 未经独立 OpenSpec 和用户明确要求，不得新增 Electron 原生产品代码、依赖或测试。
- 不得复制 `web/` 或 `electron-web/` 的桌面 DOM 应用形成分叉。
- 不新增原生文件系统 IPC、原生扩展或其他系统能力；文件和网络继续复用 Web 的后端 URI、虚拟文件系统和 Gateway。

## 规范

- renderer 通过 `clients/electron-web` 导入共享工作台入口，不暴露 Node 或 Electron API，不新增空 preload。
- 原生端测试必须覆盖产物启动、窗口生命周期和持久化；不能由 `electron-web` 浏览器测试代替。

首次执行 `bun install --cwd src/clients/electron` 安装宿主依赖和运行时；`bun run build:electron` 构建共享工作台，`bun run dev:electron --gateway=<Gateway HTTP(S) 地址>` 启动。`bun run package:electron` 生成当前平台的便携目录、压缩包和 SHA-256，输出位于 `out/packaging/electron/`。解压后执行 `boxteam --gateway=<Gateway HTTP(S) 地址>`；主进程不代替已有 Gateway/工作区服务启动器。
