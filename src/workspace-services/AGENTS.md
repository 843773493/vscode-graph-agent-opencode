# src/workspace-services

## 目录用途

存放由本地开发运行时或发行启动器管理的工作区辅助服务，例如 Browser Manager 和 Terminal Manager；它们不是产品客户端，也不是 Workspace FastAPI 后端业务模块。

## 可修改内容

- Browser、Terminal 辅助服务的 server、client attach 页面、资源和协议适配。

## 不可修改内容

- 不要实现主窗口 UI、Agent 业务规则或 Gateway 工作区注册逻辑。
- 不要依赖 `src/clients/` 中的客户端实现。

## 规范

- 可以依赖 `src/shared` 的协议和常量。
- 服务错误必须快速失败并暴露详细信息。
- 每个新增服务子目录必须包含四段式 `AGENTS.md`。
