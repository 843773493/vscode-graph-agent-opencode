---
name: team-collaboration-deepseek
description: 使用 DeepSeek 全队开展本仓库的长期开发、缺陷排查和独立审查，维护任务范围、验收证据与协作台账。仅准备技能或文档时不启动实施团队。
---

# DeepSeek 团队协作

主模型由用户选择 `newapi-local/deepseek-v4.1-flash`；技能不能切换正在运行的主模型。主模型不符时说明情况，由用户选择团队版本。

执行前读取 [共同调度流程](../team-collaboration-gpt/memory/2026/10/04/team-collaboration-workflow.md)。队伍人数、派单、目录、提交和验收只在共同流程维护；模型配置只在本入口维护，不写回 `AGENTS.md`。继续实施或处理报错时，再读取 [实时状态与实测改进](../team-collaboration-gpt/memory/2026/10/04/team-collaboration-state.md)。

创建 subagent 时显式传入：

```javascript
start_delegated_task({
  task_name,
  model: "newapi-local/deepseek-v4.1-flash",
  reasoning_effort: "high",
  fork_turns: "none",
  message,
});
```

`task_name` 与 `message` 按共同流程分配，实际工具名以当次 schema 为准。默认 effort 为 `high`；用户指定时可使用 `low/high/xhigh/max`。省略 effort 曾导致 400，不能省略。

创建失败时明确报告原始错误并记录受影响任务，不继承默认模型或自动换模型。复用与中断恢复按共同流程核验；选模相关修正更新本文件，运行状态更新共同台账。
