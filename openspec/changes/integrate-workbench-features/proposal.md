## Why

完整 Code OSS Web 工作台已通过原生交互验收，但 Agent 功能仍只有基础历史、发送和资源摘要。用户需要在独立工作台完成原 Web 已有的业务操作，而不必返回旧界面。

## What Changes

- 接入会话创建、重命名、删除、导航及 Thread 操作。
- 在原版 EditorPane 内挂载既有 React 会话组件，复用聊天、输入、工具、附件和资源界面；补齐每个标签明确的工作区、Session 和 Thread 归属。
- 删除当前手写 DOM 的简化聊天、Goal、任务及资源界面，沿用既有业务 hook、协议和权威写入链路。
- 将工作区文件读取、编辑保存与更改审阅连接到原生编辑器，保留后端 revision 冲突保护。
- 在会话资源内提供 Goal 全生命周期和关联任务操作，在工作区底部面板提供全部自动化管理。
- 接入工作区终端、输出、端口、MCP，以及精确 Thread 关联的运行资源；保留 Gateway 控制面归属。
- 完整迁移验证前保持独立验收入口，不提前删除旧客户端；此前关闭 8111 是一次性操作，不构成后续端口限制。
- 将正式 Web 浏览器回归迁至完整 Workbench，验收聊天及多 Thread 隔离后分批迁移文件、任务和工作区场景，最终切换开发与发布入口并删除旧外壳。

## Capabilities

### New Capabilities

- `workbench-agent-features`: 独立工作台的完整 Agent 业务功能接入与分层归属。

### Modified Capabilities

无；沿用既有后端契约及资源归属。

## Impact

修改 src/clients/workbench 的宿主与 src/clients/web 的可复用会话组件、实例作用域及业务 hook；保留旧 Web 外壳的既有契约。复用现有 Gateway、工作区 API 与权威协议。按业务纵向链路验收，隔离工作区运行真实浏览器，保留原版工作台行为，不向 reference_repo 写入。
