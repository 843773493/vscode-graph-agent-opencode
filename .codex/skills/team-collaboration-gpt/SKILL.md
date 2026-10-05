---
name: team-collaboration-gpt
description: 使用用户选择的 GPT 主模型与 gpt-6-luna/max subagent 开展本仓库的长期开发、缺陷排查和独立审查，维护任务范围、验收证据与协作台账。仅准备技能或文档时不启动实施团队。
---

# GPT 团队协作

主模型由用户选择 GPT 系列；技能不能切换正在运行的主模型。

执行前读取 [共同调度流程](memory/2026/10/06/team-collaboration-workflow.md)。队伍人数、派单、目录、提交和验收只在共同流程维护；模型配置只在本入口维护，不写回 `AGENTS.md`。继续实施或处理报错时，再读取 [实时状态与实测改进](memory/2026/10/06/team-collaboration-state.md)。

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

同日用户另指定主线程统一资源寻址术语及引用，按寻址 change 的 1.1/6.6 计划执行，与 Sol 在途业务接口错开；该机械正名单独计量，不混入主线程补救业务实现或模型效率比较。

以同一业务闭包记录首个完整候选所用时间、真实入口遗漏、必要测试首次通过、返工、依赖等待和主线程补救量；接手前的 Luna 投入及已完成前置单列，不把累计耗时差直接归因于模型。结果出来后据证据选择分工及模型，未验收前不宣布 Sol 更快。

已集成切片、当前候选与验收范围只维护在实时台账。本轮主补业务源码仍为零；catalog/residency 稳定差异早审在完整包前发现并闭合生命周期缺口。继续让固定差异早审与调用方迁移、定向验证并行，复用未变审查。主协调错误单列；没有可比计时，不给速度倍率或永久默认结论。

## 按实际交付选择分工

主代理负责依赖协调、独立核验和串行集成，分工由实际结果决定。独立业务可由一个实现者贯穿；公共接口有多个消费者时，唯一提供者先固定完整合同，再按依赖推进消费者。用户建议是试验选项，不能推成全仓模板。粗切片队列曾漏列原任务的typed字段、residency回收和远端URI；按原task ID和入口对应证据，区分真实缺口、已实现待集成与陈旧规划，不把切片名称或总测试数当范围验收。

比较相近闭包的首次完整交付耗时、入口遗漏、必要测试首次通过、返工轮次、依赖等待和主线程补救量。没有可比证据时不宣布最优；原有前置与接手成本单列。已授权纵向链路的必要调用方、fixture 和恢复入口由同一实现者继续，例行参数迁移不反复停问。agent 名称与恢复摘要不能决定实际代码归属或撤销用户新调度。

## 需要记住的严重失败

本入口只保留影响下一次决策的原因；当前效果与证据见[实时台账](memory/2026/10/06/team-collaboration-state.md)，历史细节保留在原报告。

- **交付残缺、漏真实入口。** 手筛 hunk 曾漏签名、删除和 Web caller；CLI stdout迁移漏启动脚本，依赖迁移漏子项目lock。用[交付校验脚本](scripts/verify_delivery.py)从固定树严格重建并核 blob/hash，清点候选外源码。调用图包括 UI/API、CLI、进程入口、恢复、协议与间接消费者；AST直接call计数不能覆盖`options[旧参数]`再`**kwargs`传入，固定候选残留与作者deferred也须分开清点。删旧API名不能证明签名迁移完成：投影消费者曾仍缺必填thread/owner；共享fixture改动须核全部消费者的完整签名，并定向验证实际使用已变fixture/helper的入口，未变依赖复用审查。成批改前核真实callee签名并验证最小入口；不存在的`Saver.main_thread_id`曾阻断28项，AST/Ruff未检出，严禁补alias。同一已授权文件中已识别的同类caller一次迁完，再做单文件验证；失败列表是观察样本，不能把前五项当迁移边界，逐批暴露同类参数错误。mock/build不代入口验收。迁移规划也核原合法输入/输出：新文件合同曾把 read_file 全限文本，漏已有图片结果，独审后在实施前纠正；新增范围不能废除原能力。旧 d164cf6 Web直连Terminal Manager缺owner body的P1在当前持久资源owner入口已闭合，不能沿用旧finding。
- **合同与基线错配。** live、旧 HEAD和固定候选混读曾误判接口；只给接口名称未交完整依赖又形成循环等待。核共同祖先、完整 preimage、真实构造注入/MRO与caller/callee，唯一提供者先固定完整合同，主串行同步。消费者尚待实现的必填字段不能成为获得前置的循环准入条件。公共闭集只在owner维护，消费者具名引用；files规划扩集曾留下两份旧四值表，严格校验未发现语义矛盾，独审后删除重复表。同步清点旧验收任务；Skill读URI禁令曾残留在E2E条目，spec修订不能只加新task。旧schema红灯又驱动重建已撤销的实验兼容；先核当前支持合同，废止能力清caller后下线，不伪造冻结fixture求绿。历史“零落盘”盘点不作当前准入；删旧API的early包可先审，真实caller迁完才原子集成，避免helper绿灯留下悬空调用。
- **身份与异步writer漏项。** Thread/Session混用、只在create带UUID、取消等待却未收`to_thread`writer均曾过helper测试。核所有HTTP/WS、真实user/internal/child及恢复入口；只用已校验admission，internal不造user Turn。幂等先比durable原请求；typed迁移还要在真实writer核owner/slot/revision CAS及未封存约束；R早审又发现descriptor→observation丢root policy，以及prefix/ToolSet各自合法但缺联合校验。核字段完整传递与同一事实的跨载体约束，round-trip或各自hash合法不足以验收；修公共hash联校又误用脱敏后空policy，须核public/protected真实manifest token及旧binding不可复用，不能跳校验求绿。
- **拒绝、未知结果与执行镜像脱节。** 确定拒绝漏失败回执污染owner；资源存活又被误当未知动作已完成。回滚或释放失败不能发布verified/closed或先丢句柄。由现有owner在真实转换边界记录结果并同步既有镜像；未知副作用保持阻断，定点核实身份/revision/终态后结清，普通列表不能清unknown。复验受影响资源，不只补单一状态码或伪payload。
- **生命周期被缓存或监听器遮蔽。** LRU曾驱逐仍被服务持有的catalog，关闭listener又漏Goal/Job派生writer，卸载await窗口允许新claim落盘。唯一registry持有到真实释放；持久写前消费同一closing准入，先收finalizer/后台writer再关catalog。scope构建前交owner，cleanup失败保重试句柄与generation fence；不能删guard求绿。重启/失败重试共用真实owner核实，不缓存一次性恢复结论。测试重启后新进程曾被function scope提前回收，导致后续十项入口失败；新handle须移交原module fixture并保留其finally关闭，先核句柄归属再跑完整消费者组。
- **路径与索引误操作。** 相对编辑、默认cwd、裸Git、自建archive/.venv反复错根，私有index只隔离暂存。每次给绝对workdir/index/产物路径，审查用固定tree，运行复用指定完整WT，不`/tmp`或复制整树。共享index从`--absolute-git-dir`定位；主曾误认私有index，成本单列。裸status刷新stat cache又触发冻结停顿；保现场并独立比对mode/blob/stage，主核暂存语义后记录新SHA再续guard，不凭字节变化判代码覆盖。无精确备份不声称精确恢复，重复误操作者收窄写权限。
- **验证与候选脱节。** live绿灯不归缺装配的固定包，重试不覆盖raw/exit；Web build不代tsc。准备阶段selection/losses正常也不证明发送无损：裸dict overlay曾在真实Chat转换中变空且无诊断；核实际Provider转换及HTTP正文与封存事实，对不支持内容在副作用前明确拒绝。正文无损仍不等于精确重放；request hash须覆盖规范化wire与projector/version/profile，不能用LangChain消息封装代替。合法raw checkpoint被fixture预处理曾掩盖入口缺口，Struct往返又丢数值类型与工具最终回执。共享fixture先核domain枚举、真实callee和恢复字段的权威来源，单例fail-fast，再有界集中收错；不能改golden、输入形态、默认值或增timeout求绿。旧身份迁移只替加密AAD而未替密文，又造成独立认证失败；先核原冻结配对，必要的新身份测试向量显式重新冻结并独立验真，不改生产认证。typed fixture也须对照冻结preimage补字段，不能用通用默认覆盖它。测试与生产须来自完整一致组合，禁止只覆盖测试文件。
- **实现放对目录却越过职责。** Saver早包在seal内拼Provider请求、runtime内做wire映射，并用函数内导入隐藏mapping双向依赖；消费链201项绿仍不能发现这些边界错误。稳定差异早审须对照最近目录规范核顶层职责和直接依赖，纯转换交现有mapping/provider owner，事务与来源编排留原owner；不放宽AGENTS规避错误或保留重导出双轨。读snapshot内恢复详情又触发初始化写锁，真实重启超时；先冻结plan/ref，再于读快照外取原不可变详情，保owner/revision/hash校验与准入。固定ff1增量独审确认职责闭合，88f真实重启及受影响41项通过；整R仍未验收。
- **崩溃测试漏首个持久化窗口。** importer首修只测完整临时文件，空/截断`.next.json`仍阻恢复。覆盖创建、部分写入含UTF-8、fsync、原子发布及另一进程重开；未发布与已发布损坏分开判定，不删审计求绿。真实退出反例四红、最小修复后固定64项绿已验证此调整。
- **恢复后审查对象漂移、主诊断过早。** reviewer读错schema tree误撤缺陷；主未核当前caller就派“Gateway等待缺句柄”，实树已有两handle，探针也可见退出。只看上层`MessageRole.user`又漏掉下游已绑定的`InternalExecutionIntent`，误报真实user Turn；R审查又把无caller的封存后脱敏转换和猜测的transition字段当缺陷。恢复先核基线/候选/报告根、真实类型与完整调用链；底层测试漏pinned claim不等于生产缺口，须先核Service已有capture前置，保guard并迁真实fixture。没有生产caller或规范依据的假设列未验项，未接消费者不阻塞已承诺前置。反证成立即撤单，不补镜像测试。区分旧生成数据、源码缺陷与未证原因，不归错实现者或模型。
- **真实协议与重启权威遗漏。** 远端测试将合法Browser二进制帧当JSON；fresh SQLite只存投影未同事务保存owner元数据，reader漏恢复字段，新客户端又漏登录。按真实协议及认证上下文验收；引用与authority在同owner事务保存，重开核完整对象/游标、失败零副作用。正式远端WS与重启已绿，通用强杀关闭风险仍须另验，正常退出不能证明失败关闭安全。

审查按tree、路径和依赖复用，只审新增差异及受影响入口。主未写实现的窄增量可由主直接独审；复杂公共事务用只读审查者，不为每包重复等待整队。严重失败先纠正具体任务，再简洁更新技能；后续相关交付验证有效才记为已解决，不叠加同义规则或把继承缺陷与主协调错误全归当前模型。
