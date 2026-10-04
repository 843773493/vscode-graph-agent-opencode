---
name: team-collaboration-gpt
description: 使用用户选择的 GPT 主模型与 gpt-6-luna/max subagent 开展本仓库的长期开发、缺陷排查和独立审查，维护任务范围、验收证据与协作台账。仅准备技能或文档时不启动实施团队。
---

# GPT 团队协作

主模型由用户选择 GPT 系列；技能不能切换正在运行的主模型。

执行前读取 [共同调度流程](memory/2026/10/04/team-collaboration-workflow.md)。队伍人数、派单、目录、提交和验收只在共同流程维护；模型配置只在本入口维护，不写回 `AGENTS.md`。继续实施或处理报错时，再读取 [实时状态与实测改进](memory/2026/10/04/team-collaboration-state.md)。

创建 subagent 时显式传入：

```javascript
start_delegated_task({
  task_name,
  model: "gpt-6-luna",
  reasoning_effort: "max",
  fork_turns: "none",
  message,
});
```

`task_name` 与 `message` 按共同流程分配，实际工具名以当次 schema 为准。模型 ID **不带 provider 前缀**，不能省略模型，也不能擅自添加 `newapi-local/`。

上述参数已通过只读探针，创建、回复和 `turn_context` 均确认成功，见 [模型探针记录](../../../out/tests/temp/2026/10/03/154301-gpt-model-probe/coordinator/artifacts/model-probe.json)。工具说明中的候选列表不能单独证明某个 ID 不可用；实际创建失败时明确报告原始错误并记录受影响任务，不自动换模型。

复用与中断恢复按共同流程核验；用户变更模型选择时直接修订本入口的唯一配置，运行状态更新共同台账。

## 按实践减少返工

主代理继续负责调用链、依赖、独审与串行集成。分工按实际返工、入口缺陷和合并成本选择：独立纵向业务可由一个实现者贯穿；公共合同由唯一提供者先固定，消费者按依赖顺序跟进。先减少返工，再比较效果，不把某种分工设为所有任务的模板。

当前严重拖慢交付的实测原因与调整：

- E02 手筛 patch hunk，补丁畸形且漏核心签名；V02 固定包漏 Web caller 和删除。交付由固定 Git base/target tree 直接导出、严格重建、核对 blob/hash，并列出候选外全部源码与未跟踪文件；通过后才交消费者。
- 公共 owner 合同多树并改，消费树缺生产模块或使用错误三方基线，造成 collection 失败与接口回退。派单登记完整依赖树和提供者；同步时用真实共同祖先，先验真实 import/入口，再开始消费者验证，不要求主代理反复人工重建残包。
- R27 测试直接写 execution/membership 关系，绿灯掩盖生产 writer 未接线。验收必须有真实生产写入到读取/DTO 的一条证据；明确区分 reader 算法测试与入口闭包。
- E02 Git 多句命令漏索引变量，恢复默认 cwd 又误读别树；产物名和 agent 名混淆重复建根。每个命令单元固定绝对 workdir、整次环境中的独立索引和已分配产物根；失败先核现场，不重新复制源码。

已通过的检查按候选 tree、路径与依赖保留；后续只审新增差异和受影响入口。依赖变化才重验受影响项，不为补日志复跑绿灯。具体证据、调整效果和下一轮边界维护在日期目录的实时台账；未实测的机械化步骤不能记作成功。
