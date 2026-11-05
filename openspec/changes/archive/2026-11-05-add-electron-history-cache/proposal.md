## Why

需要把现有完整 Web 工作台构建为可迁移的 Electron 桌面产物，连接已有 Gateway，并在应用重启后恢复前端视图和会话历史镜像。

## What Changes

- 同一份 Workbench 前端源码构建 Web 和 Electron，复用全部界面、状态、后端 URI、虚拟文件系统及 Gateway 网络链路。
- 构建最小宿主并打包 Electron 运行时和静态资源；产物可移出仓库启动，不依赖开发服务器、Bun、Node 或源码目录。
- 固定用户数据目录、持久化 partition 和本地页面来源；首次分配的回环端口写入宿主运行态，重启复用，冲突明确报错。
- 关闭窗口前等待历史缓存写入，后端仍是业务状态唯一权威。
- 增加真实 Electron 重启恢复、隔离和宿主安全验证。

## Capabilities

### New Capabilities

- `electron-thread-history`: Electron 工作台入口、历史镜像持久化和重启恢复。

### Modified Capabilities

无。

## Impact

涉及 `src/clients/electron`、`electron-web`、Workbench 启动与静态代理复用、历史缓存提交生命周期。新增独立 Electron 依赖；新增 `packaging/electron` 与发布入口。提供当前宿主平台便携目录和压缩包；不修改 Gateway/工作区 API，不增加原生文件系统 IPC、原生扩展体系、自动更新或其他系统能力。
