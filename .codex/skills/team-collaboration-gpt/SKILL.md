---
name: team-collaboration-gpt
description: 使用用户选择的 GPT 主模型与 gpt-6-luna/max subagent 开展本仓库的长期开发、缺陷排查和独立审查，维护任务范围、验收证据与协作台账。仅准备技能或文档时不启动实施团队。
---

# GPT 团队协作

主模型由用户选择 GPT 系列；技能不能切换正在运行的主模型。

执行前读取 [共同调度流程](memory/2026/10/05/team-collaboration-workflow.md)。队伍人数、派单、目录、提交和验收只在共同流程维护；模型配置只在本入口维护，不写回 `AGENTS.md`。继续实施或处理报错时，再读取 [实时状态与实测改进](memory/2026/10/05/team-collaboration-state.md)。

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

## 当前效率试验

用户于 2026-10-05 指定一个 `gpt-6.1-sol` / `max` subagent 贯穿实现，暂停 Luna 实现者写代码；本轮按该授权覆盖上述默认配置，独立审查与主线程串行集成保留。实际创建及 `turn_context` 已确认 Sol 模型。暂不把单次试验改成永久默认，也不为满足常驻数量派发重复实现。

以同一业务闭包记录首个完整候选所用时间、真实入口遗漏、必要测试首次通过、返工、依赖等待和主线程补救量；接手前的 Luna 投入及已完成前置单列，不把累计耗时差直接归因于模型。结果出来后据证据选择分工及模型，未验收前不宣布 Sol 更快。

## 按实际交付选择分工

主代理负责依赖协调、独立核验和串行集成，分工由实际结果决定。独立业务可由一个实现者贯穿；公共接口有多个消费者时，唯一提供者先固定完整合同，再按依赖推进消费者。用户建议是试验选项，不能推成全仓模板。

比较相近闭包的首次完整交付耗时、入口遗漏、必要测试首次通过、返工轮次、依赖等待和主线程补救量。没有可比证据时不宣布最优；原有前置与接手成本单列。已授权纵向链路的必要调用方、fixture 和恢复入口由同一实现者继续，例行参数迁移不反复停问。agent 名称与恢复摘要不能决定实际代码归属或撤销用户新调度。

## 需要记住的严重失败

详细证据与后续效果维护在[实时台账](memory/2026/10/05/team-collaboration-state.md)，本入口只保留影响下一次决策的原因。

- **交付包不完整。** 手筛 hunk、旧白名单与只测 helper 多次漏签名、删除、UI caller 和生产接线。用[交付校验脚本](scripts/verify_delivery.py)从固定 base/target 导出标准补丁，严格重建并核 blob/hash；冻结前列候选外源码及未跟踪文件。再从候选的真实入口核 callee、装配和必要测试闭包。`numstat`、AST 或机械绿灯都不证明业务完成；无 `.git` 的既有快照直接按对象核，不再为工具新建全树副本。
- **前置和公共合同错配。** 作者 live、旧 HEAD、固定候选被混当同一源码；旧 union 回退新 owner，消费者被循环依赖拖住。检索显式给 tree，核真实共同祖先和逐路径 preimage；公共合同先固定，主串行同步。共享文件按符号保双方语义，不能选一侧整覆盖或沿旧数量清单造 no-op。
- **真实身份与生命周期漏项。** 合成 Job/Turn、metadata 猜归属、helper 测试和伪 SQLite 播种掩盖准入、取消、fork、恢复缺口。核用户动作到唯一 writer 的真实 owner、transaction/await 边界及副作用顺序；内部 execution 不造 Turn。拒绝、取消、重启和并发都要收口，不能删 guard 或放宽断言求绿。普通 admission writer 不替代 fork journal materialization capability。
- **路径说明没有防住误写。** 默认 cwd、相对编辑、私有 index 下裸 live 检索与自建 archive/.venv 反复错根。给字面绝对 workdir/index/源码/产物路径，核实际写入和进程代码来源；独立 index 只隔离暂存。保独有源码后删可复现副本，对象审查用 Git tree，运行复用主指定完整 worktree，不 `/tmp`、不再复制整树。
- **恢复命令清空共享索引。** WVL 裸 `read-tree --empty` 破坏共享 stage；显式 `--git-dir/--work-tree` 不隔离 index，重复文字提醒已失效。该实现者收窄为指定源码编辑，由主负责 Git 冻结和验证；主在恢复窗口保 index 与 stage inventory。首恢复守界不代表永久解决，工具没有文件系统沙箱；恢复缺精确备份时如实记不确定性，不拿 HEAD 冒充原 stage。
- **修补与审查也会引入返工。** 为 lint 移动 await 曾制造取消窗口，重复断言替换改错邻例；main alias 被误判必须以 thread ID 命名。先核规范和真实输入，小修限定函数并核 postimage，定向验证原失败。当前用户已允许删旧开发数据，不把旧 schema 兼容当阻塞；fresh writer 和必要生产能力仍必须兑现。
- **检查和日志不绑定候选。** 作者额外 workspace 的测试绿不能归给缺接线或语法错误的固定包；重试曾覆盖首失败 raw。每次保候选、命令、源码根和真实 stdout/stderr/exit，attempt 运行前确定。日志遗失如实记工具转录，不补造、不为补日志重跑旧绿。

已通过的审查按 tree、路径和依赖复用；后续只审新增差异和受影响入口。依赖变化、真实失败或未解决疑点才重验。每次严重失败先纠正具体任务，再简洁更新本入口与台账；只有后续交付验证有效的调整才记为已解决，不继续叠加同义规则。
