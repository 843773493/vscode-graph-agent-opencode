---
name: team-collaboration-gpt
description: 使用用户选择的 GPT 主模型与 gpt-6-luna/max subagent 开展本仓库的长期开发、缺陷排查和独立审查，根据实际交付维护协作方法与验收台账。仅整理技能或文档时不启动实施团队。
---

# GPT 团队协作

主模型由用户选择；技能不能切换正在运行的主模型。先读[共同流程](memory/2026/10/10/team-collaboration-workflow.md)，恢复实施时只读[实时台账](memory/2026/10/10/team-collaboration-state.md)的当前租约、收据与队列。人数、路径、Git和派单合同只在共同流程维护，AGENTS.md不绑定模型或策略。默认至少3个常驻、最多15个subagent；等待依赖的常驻成员不必运行任务，不为人数制造工作。主统一调度，subagent不自行派生。全禁Git成员不分配索引；主独持固定与集成索引。

创建subagent显式传入：

```javascript
start_delegated_task({
  task_name,
  model: "gpt-6-luna",
  reasoning_effort: "max",
  fork_turns: "none",
  message,
});
```

模型ID不带provider前缀，不省略或静默替换。工具候选列表不能单独证明模型不可用；实际创建失败时保留并报告原错误。已有[真实探针](../../../out/tests/temp/2026/10/03/154301-gpt-model-probe/coordinator/artifacts/model-probe.json)。复用前核实际模型与effort，follow-up不能换会话模型。

## 当前分工与实施节奏

用户指定 **gpt-6.1-sol/max** 贯穿核心，**gpt-6-luna/max** 只做隔离小修、只读审查和验收，覆盖默认组合。Sol独占ROOT核心；主维护规范、技能、机械固定和串行集成，不手写业务源码或作者测试修复。Luna写入前核Sol租约与真实依赖，只给明确的小范围代码worktree。37 mixin与平行状态表目前只有结构风险，不据此派大型重构。

一次迁齐完整调用闭包：生产源码、直接和间接caller、DTO/协议、fixture及既有真实入口。必要文件清单用于盘点，不在已授权闭包内逐路径审批。多人公共接口由唯一owner固定后再推进消费者；单作者接口与消费者同批完成，不人为加中间门。

按用户要求先完成整批必要改动，Luna提前只读核身份/路由/DTO/目录解析共同前置；快速静态、批末Web build，再用少量真实核心集中调试，最后补受影响边界。运行使用共同流程的固定入口及每次不可覆盖原件，主直接核原件，不重复搬运。作者在同批内完成有限的测试接线与调试，再整体STOP、固定、独审和串行集成；不逐字段冻结、让审查者代跑半成品或反复启动未变基础流程。唯一产品E2E owner可选择具名场景，按真实依赖复用未变通过项，保留原全部验收要求，不新增重复产品入口。

只读调查到约定时间先交可执行结论、精确路径与不确定处。比较完整候选耗时、入口遗漏、首次通过、返工、依赖等待与集成成本；继承投入、上游路由/排队及主代理补救分列。没有同起点A/B，不宣布模型速度倍率，也不据单次建议全面改分工。

每批调度同时处理当前最拖交付的瓶颈：利用已有原件判断成本来自实现、启动接线、重复验证还是等待依赖，优先执行能消除返工的具体调整。下一相关批核实际效果，有效保留、无效撤回；效率维护随交付进行，不另建统计任务、重复审查或报告搬运。

## 已证明需要的纠正

详细证据与后续效果在[失败细则](memory/2026/10/10/delivery-failure-rules.md)，只按当前问题关键词读取。

- **交付遗漏：** 禁Git作者交STOP源码、程序生成的SHA/actual及原raw。主从固定Git树导完整补丁，严格重建、核全部caller和新增/删除文件；只集成固定blob，不add作者live、裁hunk或使用陈旧index。Git blob ID与文件SHA256分别具名。报告引用机器原件，不手抄PID、hash或退出码；原失败保字节，新增finding另存版本。
- **真实入口漏接：** helper绿不证明真实装配、准入、提交与重开读取；DTO有字段不证明SQL/mapper返回嵌套来源坐标。跨owner身份、namespace、事务连接和snapshot/view沿完整链传递，不能从物理顺序或旧carrier猜归属。锁内回读复用writer连接，不重取同锁。测试定位和断言跟随权威catalog及真实DTO；不按旧物理布局拼路径，不将Job或internal execution冒充Turn。旧fixture断言随闭包迁齐；负向输入须先合法到达声称测试的边界，不能把提前schema拒绝算SQL验证。替身只放外部传输边界。
- **路径和Git越界：** 每条命令显式物理workdir，写入前核目标resolve属于租约根；上一shell的cwd不约束下一工具。主供完整Python启动命令：ROOT venv、UV_PROJECT_ENVIRONMENT、--no-sync、唯一所属代码根PYTHONPATH及timeout/ulimit进程外保护，作者直接执行。禁Git任务不能复制含status/rev-parse/show的状态检查模板；“只读”不是豁免。主每次Git subprocess显式私有GIT_INDEX_FILE和GIT_OPTIONAL_LOCKS=0。越界先保现场、核实际影响；重复路径漂移撤写入/运行权并转交，不能自行另建fallback。禁止/tmp、新venv、整仓复制和源码根业务工作区。
- **调试失真：** 诊断必须保留原业务规模、并发机制和观察端点；串行A完成再B不能定位两页Promise.all竞争。短诊断同批给总时间与有限启动次数，不逐报错微交接；有原raw就先定位，不盲跑长矩阵。导航按冻结operation ID配对请求、回执、发布和真实DOM，区分发起页、HTTP派发页与终态处理页；完成时间取当场DOM提交后的双rAF快照。可选诊断、分类阈值及临时断言不能升级成原需求以外的门。具体FS650ms、虚拟化、multipart、轮询和query-hold条件读失败细则对应关键词。
- **假成功或无效重验：** 每attempt保存原raw、actual退出码、耗时与机器SHA，wrapper0/PASS不能推翻测试非零。raw未保存就标工具转录，不补造精确值。整体失败与已过子项分别登记，检查仅按实际依赖SHA和最新接受版本复用。独审可读取固定测试及同闭包必要callee，manifest不是逐路径读取审批。损坏权威index明确失败并保正文，冷读取不初始化/repair；清理失败保留后续重试所需事实，取消传播且独立资源close-all/join-all。具体owner合同按失败细则读取，不扩成通用框架。

## 上游故障与恢复

通用路由报错不能单独判定模型实现能力。用户确认修复后续接原会话，以真实工具/源码交付验证恢复，不沿用旧阻断计数。显示running不证明执行；核具体turn/handle，轮询同一请求，不因观察超时重新派单。`exec_command.session_id`只交`write_stdin`，`functions.exec`的cell ID只交`functions.wait`；handle关闭先查既有终态输出。

长运行由进程在启动前落盘本attempt的raw和源码指纹，结束保存actual，不依赖聊天handle。只有权威终止，或撤销任务且核实中断后，才恢复或交新owner。指定模型连续终态失败且无替代授权时，保独有源码、结束租约、记录原错误；同一阻断连续至少三个goal turn且无其它可推进工作，按goal工具规则标blocked。三个agent失败不能计作三个goal turn。Provider quota等外部同因不反复求绿、不擅改环境；尚未实现的确定性fixture/harness仍需原作者完成，不能全归外部阻塞。

严重失败先纠正派单，再窄改相应规则；真实后续交付验证后才记有效。技能入口只保改变决策的规则，事故细节与状态留在memory；重复或无效规则删除。窄闭包、规范审查或少量绿测试不能勾整个change或结束原goal。
