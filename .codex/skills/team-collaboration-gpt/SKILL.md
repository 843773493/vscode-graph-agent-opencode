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

已集成切片、当前候选与验收范围只维护在实时台账。本轮主补业务源码仍为零；catalog/residency 稳定差异早审在完整包前发现并闭合生命周期缺口。继续让固定差异早审与调用方迁移、定向验证并行，复用未变审查。主协调错误单列；没有可比计时，不给速度倍率或永久默认结论。

## 按实际交付选择分工

主代理负责依赖协调、独立核验和串行集成，分工由实际结果决定。独立业务可由一个实现者贯穿；公共接口有多个消费者时，唯一提供者先固定完整合同，再按依赖推进消费者。用户建议是试验选项，不能推成全仓模板。粗切片队列曾漏列原任务的typed字段、residency回收和远端VRN；按原task ID和入口对应证据，区分真实缺口、已实现待集成与陈旧规划，不把切片名称或总测试数当范围验收。

比较相近闭包的首次完整交付耗时、入口遗漏、必要测试首次通过、返工轮次、依赖等待和主线程补救量。没有可比证据时不宣布最优；原有前置与接手成本单列。已授权纵向链路的必要调用方、fixture 和恢复入口由同一实现者继续，例行参数迁移不反复停问。agent 名称与恢复摘要不能决定实际代码归属或撤销用户新调度。

## 需要记住的严重失败

详细证据与后续效果维护在[实时台账](memory/2026/10/05/team-collaboration-state.md)，本入口只保留影响下一次决策的原因。

- **交付残缺或漏实际入口。** 手筛 hunk、旧白名单曾漏签名、删除和 Web caller；Python/Node 测试绿时，真实 Terminal 创建仍因缺字段而400。Workspace identity CLI 改为裸 UUID 后又漏迁移开发启动脚本的 JSON 消费者，主集成才发现整组服务启动失败。下线合同检索包括 CLI stdout、启动器和进程入口，必要时做真实定向调用。既有Web依赖只写package.json漏子项目lock，缓存安装/build掩盖缺口，fresh Docker bootstrap才失败；变更依赖核所属lock并用frozen安装验证。用[交付校验脚本](scripts/verify_delivery.py)从固定树导出、严格重建并核 blob/hash，冻结时清点候选外源码；业务验收沿实际 UI/API→owner→存储或外部服务，不以 helper/mock/build 代替。
- **合同与基线错配。** live、旧 HEAD 与固定候选曾混用；旧 union 回退新 owner，消费者准入形成循环依赖。显式检索 tree，核共同祖先和 preimage，唯一提供者先固定公共合同，主串行同步；共享文件按符号保语义，较新的文档也核合同来源。陈旧注释曾让审查误报已有 endpoint 缺失；派单前核实际路由及消费者，避免重建已有能力。fixture 与 standalone 生成器同属消费者：`--help` 不代真实写入及原产物合同；按授权删除过期生成库，不恢复旧兼容或另造初始化旁路。
- **身份和异步 writer 漏项。** thread/session 混用、仅 create 带 UUID、取消等待却未收 `to_thread` writer均曾过 helper 测试。核全套 HTTP/WS、空列表/health/接管以及真实 user/internal/child 入口；归属只用明确 owner，内部执行不造 user Turn。幂等先比 durable 原请求，解析事实另冻结；fork 映射核 SQL/JSON 且输出可再次合法输入。typed迁移早审仍发现基线已有的无revision CAS替换；不能只验字段round-trip，还须在真实writer验同owner/slot/预期revision及未封存约束。
- **失败回执与可核实事实混淆。** Browser 用户锁的 423 与非法 lock 参数因绕开失败回执边界，污染 owner 状态。确定拒绝带真实失败回执，丢回执仍保 unknown；按既有动作收齐回执，不只补单一状态码。反过来，freeze/wake 部分转换或回滚失败不能因回调抛错就把 snapshot 声称为 verified：真实转换 owner 记录 unknown，执行入口消费阻断。核真实拒绝零副作用、解锁恢复与部分失败，不能只伪造 payload 测试。
- **权威事实与执行镜像脱节。** Browser 启动失败被观测为 verified 后，既有 lease ledger 仍 unknown，管理操作继续拒绝；把同步塞到 acquire 又误伤正常 creating Terminal。在完成权威观测的边界同步既有镜像，保持未知操作阻断；只复验失败入口和共用该边界的已验收资源。公共投影又曾用创建名覆盖 Browser 页面 title；同名字段先核真实消费者合同，不能按新命名假设改测试断言。
- **生命周期被缓存或监听器遮蔽。** 数量 LRU 曾驱逐仍被服务持有的 catalog，重建第二实例且无人 close；注销 listener 曾漏其派生 Goal/Job writer。卸载 listener 的 await 窗口还曾允许新 claim 先落盘再登记 blocker；真实 writer 必须在持久写入前消费同一 closing 准入。崩溃遗留 lease 曾连已核停止的 PTY 也永久锁死；一次性 recovered 缓存又使丢回执后的同代重试无法重新核实。恢复与失败重试共用 owner 的定点核实/结清路径，证明身份、revision 和终态后才结清；未知或仍运行保持阻断，普通列表不清 unknown，也不能隐藏待处理资源。Browser 首轮又发现存活快照清掉持久未知动作：资源存活不证明该动作结果，不能以普通观测结清；公共 owner 修订须定向复验受影响的已验收资源，不重跑无关全组。让唯一 registry 持有到真实释放，先关续跑准入、收 Job finalizer 和后台 writer，再关 catalog。scope 在构建前交 owner；cleanup失败保重试句柄与旧回调 fence，不以删 guard 求绿。Browser又曾先发布closed/verified再await真实释放、callee先丢context句柄：失败可被误报完成。真实释放成功后才清句柄并发布终态，失败保unknown和显式核实重试入口。
- **路径和索引误操作。** 相对编辑、默认 cwd、裸 Git 与自建 archive/.venv反复错根；私有 index只隔离暂存。每次给绝对 workdir/index/编辑/产物路径，对象审查用 Git tree，运行复用指定完整 WT，不 `/tmp`、不复制整树。共享 index 从 `--absolute-git-dir` 定位；主曾误把私有 index当共享 index，约九分钟协调错误单列。无精确备份不声称精确恢复，重复误操作者收窄写权限。
- **验证与候选脱节。** 额外 workspace的绿灯不归给固定包；重试不覆盖原 raw/exit。官方生成类型后单跑 tsc，Web build不查类型；Struct 序列化曾把退出码0变float并漏工具最终回执；业务数值保 typed/presence，实际往返核缺值、0和非零，数值等值断言不证明类型。未变绿项复用，不为补日志重跑。共享fixture批改曾把visibility写成非法full，失败扩散到领域全组180秒硬超时；先按domain合法枚举核共享构造、单文件fail-fast，再恢复必要全组，不以增大timeout掩盖。进程 fixture的配置 owner先就绪；屏障finally释放，路径预算fixture有限构造并先核条件可达。为 lint改await或全局替换断言曾引入新问题，小修限定函数、核postimage并定向复验，不改变合法main alias或生产拒绝条件。

已通过的审查按 tree、路径和依赖复用；后续只审新增差异和受影响入口。小范围增量且主线程未写实现时，可由主线程直接读取固定差异独审；较大公共状态机抽取使用独立只读审查者。本轮 launcher 按此前方式通过独审与真实恢复，不为每包重复等待完整审查队伍。依赖变化、真实失败或未解决疑点才重验。每次严重失败先纠正具体任务，再简洁更新本入口与台账；只有后续交付验证有效的调整才记为已解决，不继续叠加同义规则。
