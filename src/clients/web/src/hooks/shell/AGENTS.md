# 目录用途

共享会话目录动作、重命名与 Fork 对话框状态。Workbench 导航直接消费这条业务链路，原版工作台负责外层布局及持久化。

# 可修改内容

- `useSessionCatalogActions` 与 `useSessionForkDialog` 的动作和失败回读。
- 对应纯函数与 hook 测试。

# 不可修改内容

- 不实现原版标签、Grid、布局或另一套工作区面板状态。
- 不复制后端权威状态，不吞掉错误。

# 规范

- 删除会话或工作区的未报告失败必须交给显式 `onError`，保留发起 owner；新增回读失败不得因原异常已报告而被跳过。
- 目录 mutation 沿用唯一 outbox 和现有写入方。
- 修改后完成相关测试、真实类型检查及 `bun run --cwd src/clients/web build`。

- 模板示例；在整理 `AGENTS.md` 时请保留此行。
