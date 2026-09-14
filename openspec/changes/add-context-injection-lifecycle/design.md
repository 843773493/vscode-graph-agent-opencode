## Context

参见 `proposal.md` 的动机。已有 `add-itemized-rollout-context` 提供 v2 canonical item、`ContextRequestPlan`、source overlay、sealed assembly 和 Saver ownership；本设计补齐统一 mutation 边界、producer、追踪、ToolSet hard rebase、wire role 与稳定前缀合同，不建立第二套上下文存储。

当前 Agent 配置、runtime identity、Todo/Filesystem/Skill/AGENTS/压缩/Memory middleware 会在不同阶段拼接 system prompt；Goal、委派、跨会话、团队、终端、retry 和 checkpoint reminder 又会直接产生 `HumanMessage` 或通过内部 `MessageRole.user` 创建 Job/Turn。最后，`PromptReplayCaptureMiddleware` 再从已组装请求反推 source 边界。这些路径会重新组装旧 system prompt、重复投影 source、绕过 checkpoint owner，或者让内部状态伪装成真实用户输入。

目标链路为：

```text
user/assistant/tool protocol ── AppendCanonicalItemIntent ───────────┐
SkillCatalog/AGENTS/team ───── ApplySourceLifecycleDecision (CSM) ──┤
tool selection/policy ──────── SwitchToolSetIntent ─────────────────┤
compaction/rewind ──────────── RebuildContextEpochIntent ───────────┤
                                                                   ▼
RolloutCheckpointSaver / ContextStore mutation owner
    │ immutable items + versioned control state + ToolSet binding
    ▼
ContextRequestPlan -> sealed ContextAssemblySnapshot
    │ Saver-issued SealedAssemblyDispatchRef
    ▼
optional stateless framework dispatch bridge -> Provider adapter
    │
    └── sealed snapshot -> history/diagnostic projector
```

## Goals / Non-Goals

**Goals:**

- 把已提交 wire context 的字节级稳定设为所有 source lifecycle 决策的首要约束。
- 为当前生产代码中的 initial instruction、tracked file、runtime event、derived compaction、真实 user、ToolSet 和模型/工具协议事实建立完整迁移闭包与唯一 owner。
- 让 canonical append、source lifecycle、ToolSet switch 和 epoch rebuild 共用同一持久化/assembly transaction 边界，但保持各自的 domain owner 与不变量。
- 把任意有效 ToolSet 变化定义为 model-call safe boundary 上的 hard rebase，并让同一 Turn 可以跨多个 prefix epoch。
- 统一 AGENTS、Skill metadata/activation、团队状态和其它内部 source 的 identity、revision、diff、追踪及恢复语义。
- 用代码内显式装配的资源观察流水线统一已知文件、Gateway内部快照和权威内存状态的稳定读取、语义diff与不可变snapshot发布；正常model-call preparation只消费内存revision/snapshot。
- 建立Virtual Resource Namespace（VRN），让模型能够引用和理解资源的逻辑来源，同时隐藏物理路径、endpoint、credential和provider locator。
- 让资源变化的观察、业务desired state与某个SessionThread的context activation相互解耦，并支持默认`turn`、可选`model_call`两种确定性激活边界。
- 删除 PromptReplay 及所有 request-time 反向捕获，让 Provider dispatch只消费已封存 assembly；framework middleware 若不可避免，只承担无状态 bridge 职责。
- 提供只暴露名称的 `skill_load`，完整定义 `snapshot`、`tracked` 和 `untrack`。
- 保持 canonical item、CSM control state、active view、wire role 和真实用户 Turn 相互独立。
- 让 retry、restart、rewind、compaction 和多 projector 使用同一个 sealed selection 与 source provenance。
- 让main thread长期用户上下文、child thread协作上下文、Session内部team状态和跨Session无状态协作各自拥有明确边界。
- 让durable thread的ContextStore/CSM事实可在resident runtime卸载后按原binding恢复，并保证idle unload不改变stable prefix或source lifecycle。

**Non-Goals:**

- 不建立资源插件安装/卸载宿主、plugin manifest监视、第三方进程内provider ABI或任意外部网络资源自动注入；外部服务的工具扩展沿用MCP，未来MCP resource订阅需要独立设计和实现，不能假称当前runtime已支持。
- 不设计按 Turn/请求次数自动到期、注入次数或即时 Skill 移除。
- 不让模型看到或传入 Skill 的绝对路径、相对路径、catalog 层级或 Gateway 内部 locator。
- Skill metadata 暂不解析 `name`、`description` 之外的 frontmatter 字段。
- 不把watch事件当作内容或一致性权威，不使用`rg`周期扫盘，也不允许provider递归发现未登记资源；watch丢失/溢出只能触发有界reconcile。
- 不让每个SessionThread建立独立文件watcher，不因资源变化主动唤醒全部thread或异步修改已经封存的context assembly。
- 不在本 change 启用 Anthropic 中途 system item；只保留明确 TODO/capability extension point。
- 不改变 Provider 原生 tool call/tool result 所要求的协议 role；本设计的 user-role 规则只约束 CSM 注入 item。
- 不把用户消息、assistant/reasoning、tool call/result 纳入 CSM revision/tracking，也不把 ToolSet 变化编码成软上下文通知。
- 不保留 PromptReplay 作为诊断、兼容或漏接 source 的 fallback，也不允许 dispatch bridge维护 Session/source/assembly 状态。
- 不为跨Session协作建立共享team/member/task/role/Goal状态；跨Session只保留显式send/read/wait及其幂等、相关性和审计。
- 不把30分钟runtime idle unload解释为context item、Skill/source过期或durable thread删除，也不要求history查看唤醒Agent runtime。

## Decisions

### 1. ContextStore 是统一 mutation owner，CSM 只管理 source lifecycle

一个 `SessionThread` 在任意时刻只有一个逻辑 RolloutCheckpointSaver/ContextStore mutation owner。owner identity和durable state与thread同寿命，但resident owner实例可以按ThreadRuntime idle policy关闭并在execution admission时从持久状态重建；runtime generation/lease保证旧实例不能继续写入。Session 是产品导航、共享资源与 thread catalog 容器，不是 canonical context owner；所有会影响该 thread canonical history、active view、source control state、ToolSet binding 或 sealed assembly 的变更都必须经当前generation owner的同一 read snapshot 与 transaction 提交，业务层不能直接改 LangChain state、rollout JSONL、SQLite 或 Provider request。

统一边界使用语义互斥的 mutation intent，而不是把所有内容都伪装成 source：

```text
ContextMutationIntent =
    AppendCanonicalItemIntent
  | ApplySourceLifecycleDecision
  | SwitchToolSetIntent
  | RebuildContextEpochIntent
```

- `AppendCanonicalItemIntent`：真实 user input/attachment、assistant/reasoning、tool call/result 等一次性 canonical 事实；保留原始 item identity、origin Turn、协议配对和 append ordinal，不建立 source revision/tracking。
- `ApplySourceLifecycleDecision`：CSM 对 instruction/file/runtime source 的 base、delta、tracking 和恢复决策；可产生 ambient/pending source item与控制状态。
- `SwitchToolSetIntent`：应用模型可见工具及 policy 的新 desired revision，并触发 hard rebase；ToolSet 不是 message/source item。
- `RebuildContextEpochIntent`：compaction或rewind先提交active view与版本化CSM control state，并登记只可由下一次model-call preparation消费的`PendingPrefixEpochTransition(reason, view_revision, control_revision)`；首次assembly由owner直接初始化epoch。

`ContextSourceManager`（CSM）是该 owner 下的 source-specific 子管理器。producer向CSM提交结构化observation或名称化Skill操作；CSM负责source identity、`observed/pending/applied` revision、diff基准、tracking state和reconciliation决策，但只能返回`ApplySourceLifecycleDecision`，不能截获普通canonical append、重组整个消息历史或直接生成LangChain/Provider message。team fanout、跨Session inbox或其它异步producer可以在没有model call时提交幂等pending observation/ambient item和wakeup intent，此时不得推进`applied/injected_revision`、diff基准或创建assembly；下一次真正model-call preparation才把pending事实选择进plan并原子推进applied状态。已经创建的pending item仍是不可变事实，失败重试按identity复用而非覆盖。

CSM 的输入/输出和持久控制记录必须复用 itemized change 的“核心决策字段闭合、扩展数据开放”合同：`SourceObservation`/`ApplySourceLifecycleDecision` 显式携带 owner thread、source/facet identity、published semantic revision、activation boundary、tracking mode/state、base/delta relation、desired/applied revision 和幂等键，typed enum/union 与中文字段注释是唯一决策入口。`ContextContribution.selection_role`、`replacement_policy`、typed `source_binding` 由 producer 声明并由 itemized registry/selection 校验；owner-thread registry 才分配 `RegisteredContribution.source_ordinal`，CSM、watch reaction和ledger均不得从事件到达顺序、内存计数或 `extensions` 推导/补造该序号。旧 `selection_only`、`replaceable_source`、overlay alias和 ordinal metadata flags不保留第二解释路径。producer可提交 namespaced/versioned/protected `extensions` 供审计或展示，但未知扩展原样保留、不影响 tracking、diff 基准、pending/applied推进、role、ToolSet hard rebase、epoch或dispatch；若任何新增代码需要改变这些行为，须先定义显式 typed capability/intent 与验证，不能靠扩展 key 触发。该约束同样适用于 checkpoint restore 和 fork/remap，缺核心字段必须失败而不能以当前文件、watch事件或扩展值补齐。

owner在一次model-call preparation中按确定顺序消费当时已提交的canonical事实和pending epoch transition，完成tool protocol convergence、source reconciliation、active-view/ToolSet selection、plan创建与assembly seal。tool protocol convergence是source选择前的硬门槛：只要前一assistant item声明的任一outstanding tool call还没有同一causal execution内匹配的terminal `tool_result`，期间由`skill_load`、team、terminal callback或其它producer提交的source/runtime事实只能保持pending，不得被排到tool call与result之间或进入可dispatch selection。全部配对result收敛后，plan必须按协议因果先选择assistant tool-call group和其全部result，再按稳定source identity/ordinal选择期间积累的pending source；物理`item_sequence`/JSONL offset或提交时间不能替代该顺序。该次preparation任一步失败都不得部分推进新item、source committed revision、applied ToolSet revision、applied prefix epoch或消费pending transition；但先前由用户显式提交的rewind/compaction active view保持有效。成功seal首个新epoch assembly与source materialization/applied ToolSet推进在同一owner transaction完成，再把transition标记consumed。

每个物理/外部来源由owner scope与软件登记的稳定source identity标识，provider locator只参与私有定位和重建绑定，不进入identity或模型输入；每个CSM source identity再绑定一个语义resource/facet。Skill的metadata与activation即使来自同一个`SKILL.md`，也使用不同语义facet identity：

```text
skill:<catalog-entry-id>:metadata
skill:<catalog-entry-id>:activation
```

### 2. 已提交上下文前缀必须字节级稳定

将投影后的 wire context 定义为按 plan ordinal 排序的不可变 item frame 串联：

```text
W(n) = frame(item_0) || frame(item_1) || ... || frame(item_n)
```

在同一个 `prefix_epoch` 内，后续请求必须满足：

```text
W(next)[0:len(W(previous))] == W(previous)
```

稳定范围包括 item 顺序、role、content block 结构、文本、空白、编码和 Provider profile 下的 item serialization。同一 epoch 的 compatibility key 还必须固定 Provider projection profile、精确 `ToolSetRef` 及影响 root/tool visibility 的 policy hash。Provider headers、认证、request ID、模型参数和 JSON envelope 不属于 context prefix；它们继续由 request hash/profile 单独约束。

只有四个边界允许登记并在下一份成功sealed assembly中应用新的`prefix_epoch`：

1. SessionThread/branch 的首次上下文组装；
2. 上下文压缩实际执行重建；
3. rewind 实际执行 active view 重建；
4. effective ToolSet/工具 policy 变化在 model-call safe boundary 执行 hard rebase。

普通 canonical append、source change、`skill_load`、`untrack`、团队事件、checkpoint restore、重复 model attempt 和 transport retry 都不能重建旧前缀。rewind/compaction可以先在没有model call时提交view和pending transition，但pending状态没有可dispatch wire bytes，也不是已应用prefix epoch；只有首个assembly成功seal时才原子应用。每个sealed assembly保存`prefix_epoch`、`epoch_reason`、`parent_assembly_id`、`stable_prefix_byte_length`、`stable_prefix_hash`、ToolSet compatibility key和新增item references；dispatch前验证父前缀，不一致时fail closed。同一assembly的retry复用已封存的Provider bytes，不能重新投影。

Provider/model projection profile同样不是第五种epoch reason。尚未有sealed assembly的SessionThread可以在initial assembly选择当前desired profile；已有assembly后，控制面改变模型只更新desired profile。若下一次dispatch前没有实际compaction、rewind或有效ToolSet hard rebase这一合法边界，owner必须返回`provider-profile-change-requires-rebuild`并保持原applied profile/assembly不变，不能静默使用旧模型、偷建epoch或把no-op ToolSet change伪造成rebase。合法边界发生时，profile切换与首个新epochassembly一起原子applied并记录旧/新profile provenance。

该约束同时禁止把新 source 合并进旧 system/user message，也禁止 Provider adapter 为满足交替 role 而合并相邻 user item。某 Provider 无法表达独立追加 item 时必须显式 reject。

### 3. Provider ToolSet 变化必须 hard rebase；扩展目标目录是另一种版本化事实

ToolSetSnapshot/ToolSetRef 只描述 Provider `tools` 中的少量直接工具与始终存在、schema/description固定的 `invoke_extension_tool(tool_name, arguments)`。直接工具或该信封的名称、schema、description、Provider可见启停/确认policy发生有效变化才产生新的 desired ToolSet revision并触发hard rebase；不区分添加和删除。内层`ExtensionToolCatalog`由其domain owner管理，包含非直接内置工具、自定义工具和MCP工具的稳定target identity、schema hash、权限/确认策略与revision。目标增删、描述/schema变化、连接generation变化或执行期权限撤销，不得改变Provider可见的信封schema/description，也不得仅因此制造ToolSetRef或prefix epoch。只有显式提升/降级为直接工具、或信封本身改变，才跨越这条边界。Provider描述不能枚举内部target名称/schema；模型从Skill、AGENTS、用户输入及受控的MCP工具指引来源了解可用目标，指引不授予执行权限。

扩展调用必须在产生该tool call的sealed model-call上冻结`ExtensionCatalogBindingRef`，并由dispatcher按该ref解析target，而不是按执行时的最新目录悄悄改投。目录与指引必须在同一资源激活边界原子选中：默认Turn固定；显式`model_call`时只在下一安全preparation消费Registry已发布snapshot。每次实际调用仍按最新不可关闭的身份/完整性校验和可热变的权限策略重新授权，撤权返回与原`tool_call_id`配对的明确失败，不修改已sealed的调用身份、schema或目标。未知/已删除目标显式报错，不伪造成功。内层目录ref、target/schema revision与结果provenance独立持久化和校验，不塞进Provider ToolSetRef、也不以目录revision扰动同epoch前缀。

ToolSelectionStore/ToolService继续拥有 workspace/agent级 desired selection及其控制面 revision；它们不写 Session context。其直接工具选择产生Provider ToolSet desired revision；内层扩展目标选择只产生ExtensionToolCatalog desired revision。每个 SessionThread 的 ContextStore owner分别持久化最后观察到的desired、当前applied ToolSetRef和内层catalog binding，并仅对前者的有效Provider形状变化构造`SwitchToolSetIntent`。历史assembly永远只读自身封存的两种binding，不能用当前ToolSelectionStore反推。

运行中的 sealed model call 永远绑定其 applied ToolSet revision，用户或控制面此时修改工具选择只能更新 pending desired revision，不能改写该请求。owner 在下一次 model-call safe boundary 按以下顺序生效：

1. 停止为旧 ToolSet 创建新的 model call；
2. 让旧 assembly 已产生的 outstanding tool call得到真实且配对的 terminal outcome：已开始执行的调用完成或返回真实失败；尚未执行且权限已撤销的调用返回绑定原 `tool_call_id` 的 policy-denied结果，不伪造“已执行”或无配对取消；
3. 原子封存新的 ToolSetSnapshot/ToolSetRef，并把 desired revision推进为 applied revision；
4. 创建 `epoch_reason=toolset_changed` 的新 `prefix_epoch`；
5. CSM只对当前 source registration/revision执行 reconciliation，assembly compiler据此重建 root context、active canonical view、source projection与 tools；
6. seal 新 assembly 后才允许下一次 Provider dispatch。

hard rebase不修改已有 canonical item identity、正文或因果顺序，也不把“工具已切换”追加成 user-role控制文本。它和 compaction都允许重新编译请求前缀，但 compaction会改变 active history view并产生/选择 compaction summary；ToolSet hard rebase默认保留同一 active canonical history，只改变 epoch、条件化 root projection与工具 binding。因此一个 Turn可跨多个 model-call-scoped prefix epoch，`prefix_epoch` 不能再被解释为 Turn 属性。

多次 selection变化若都发生在下一个 safe boundary前，可以把中间 desired状态合并为最终 revision再 seal，但控制面仍需保留可审计的 revision因果，不能把已经 applied/sealed 的 ToolSet历史改写掉。若 Provider无法在新 ToolSet下合法投影 active history中的旧 tool call/result，系统必须明确阻止 rebase，并要求适用的显式 compaction或终止该 execution；不得删除、改写或伪造历史工具事实。

### 4. source owner 声明根指令资格；普通追加永远使用 user

每个source owner以必填typed `root_placement=root_eligible|tail_only`声明其受信内容是否有资格进入新epoch的根指令；CSM不得从文件名、路径、wire role或自由extensions推断资格。首次组装只把当时有效的`root_eligible`来源按稳定顺序编译成一个不可变最顶层`wire_role=system` item。`tail_only`来源包括默认不可信的外部MCP工具指引，只能作为上下文数据进入独立user item。第一条真实用户消息之后、且没有实际新epoch边界时，任何source完整内容、diff、重新加载或恢复都只能在尾部新增独立`wire_role=user` item，不因资格或内容是full/delta而改变：

| CSM item | 当前 wire role |
|---|---|
| 首次组装的唯一 root context | `system` |
| `skill_load` 的完整 snapshot/tracked activation | `user` |
| AGENTS、Skill metadata/activation、team/runtime delta | `user` |
| 同epoch的source完整恢复或合并后的delta | `user` |
| 实际compaction、rewind、Provider ToolSet hard rebase或fork首次组装形成的新root中的`root_eligible`有效内容 | 新epoch唯一顶层`system` |
| 同一新epoch中的`tail_only`有效内容 | 独立`user` |

这些 `wire_role=user` item 仍是 ambient runtime/source item，不是 canonical `user_input`，没有 `turn_id`，不能成为 Turn root/member。Provider 原生的 assistant/tool call/tool result role 不受此表影响。

合法新epoch重建时，compiler可从已提交active view及冻结的受信ResourceSnapshot，把`root_eligible`来源的base+delta物化为新root中的当前完整状态；必须记录确定性顺序、source refs/hash、覆盖的revision链和旧item lineage，避免同一语义在新root与tail重复投影。旧canonical item/detail/assembly字节不变，历史读取仍是旧sealed selection；fork只对目标新thread首次assembly应用此规则，不重编译source历史。`tail_only`即使在新epoch也不得借摘要或合并进入system。任何source变化本身不得制造epoch；若无合法重建边界，不能为提高优先级而前插system。为Anthropic中途system只保留关闭的capability TODO。

### 5. Resource Observation Platform、VRN 与 SkillCatalog

SkillCatalog 合并三层来源：

```text
bundled resources/skills
    < ${BOXTEAM_HOME}/skills
    < ${workspace_abs_path}/.boxteam/skills
```

同名项按上述优先级确定唯一有效 entry；名称冲突、非法 metadata 或不可读文件必须显式诊断。`${BOXTEAM_HOME}/skills` 是 Gateway 级全局 catalog，默认可用于所有工作区；Gateway 负责全局 catalog/control-plane 发现，但不得写工作区 Session SQLite。Workspace 后端取得内部 catalog descriptor 后，由当前 Session 的 CSM 完成加载和持久化。

资源流水线区分“观察的来源”和“消费的语义资源”，再接一条只读激活链。内置适配和业务解析器由各进程composition root在代码中显式装配；typed port只用于清晰边界与测试替身，不构成可安装插件宿主：

```text
代码内composition root
  ├── 内置file monitor / stable reader
  ├── Gateway受认证snapshot adapter / 权威memory mutation adapter
  └── AGENTS / Skill / config / team业务loader和owner reaction
          │
          └── dirty/gap事件 ──> EventChannelService
                                # resource.observe/* 与 job.events/* 等独立channel
                                │
                                ▼
                       SourceReconciler ──CAS──> ObservedSourceRevision
                              │                         │
                              └──> ResourceDerivationGraph ──CAS──> ResourceRegistry immutable ResourceSnapshot
                                           │
                                           ▼
                       ResourceActivationCoordinator ──> Turn/ModelCall ResourceActivationSnapshot
                                           │
                                           ▼
                       CSM decision -> ContextStore owner -> sealed assembly

LifetimeScope ──仅持有/释放──> monitor task、订阅handle、client、worker、子scope
                             # 与上述事件/快照传递链正交；不解析事件或决定业务状态
```

同一种机制只有一个抽象，传递机制不承载业务决策。进程内资源的**持有与释放**统一由薄`LifetimeScope`承担：它可基于`AsyncExitStack`和结构化task supervision实现，持有可异步关闭的task、订阅句柄、client及子scope；关闭先拒绝新登记、停止/排空子task，再按逆取得顺序释放句柄。同一scope的并发close只执行一次并等待相同结果；成功后重复close幂等，失败保持可观察`close_failed`与未释放handle诊断、后续调用重报同一失败，不能虚报`closed`或用GC finalizer代替显式关闭。`LifetimeScope`只向调用方返回释放结果/错误，不依赖事件服务、不选择channel，也不发布业务状态；持有该scope的domain/composition owner负责把失败映射为其资源域的typed状态事件并投到相应状态channel，保留原始错误供调用方处理。若事件服务已不可用，owner仍须显式返回/抛出释放错误并记录诊断，不能把投递失败当作释放成功。`LifetimeScope`不认识Skill、AGENTS、terminal、Turn、30分钟阈值、config reload policy或ResourceSnapshot，不持久化事实，也不决定何时close。既有`TurnExecutionScope`组合`CancellationSignal`与该唯一释放机制：cancel传递中止意图，close释放运行期持有物；清理/订阅登记须返回可撤销handle，关闭child时解除父信号hook，禁止注册后立即创建无人持有的异步task。Web React effect仍以effect cleanup持有其局部订阅，不再建立平行的全局DisposableStore。

Task lifecycle是`LifetimeScope`的所有权树：process root持有内置file monitor、source reconcile、derivation与业务reaction worker所属的scope，child ThreadRuntime只持有自身runtime scope；停止父scope取消并排空其子scope。数据依赖另为有向无环图（DAG），不能用task树表达资源派生。file monitor的`subscribe(watch_descriptor)`返回释放该consumer引用的opaque handle；观察层内部以`monitor instance + 规范化私有locator + recursive/filter/exclude/correlation/options`为完整共享watch key并引用计数，仅最后一个handle释放时停止底层watch。共享watch归进程级file monitor而非ThreadRuntime或通用scope；不同语义不得误合并。配置/Skill/AGENTS/team的loader与reaction由各domain owner在代码中显式接线，测试可替换typed port；Registry只保存已登记来源和已发布语义revision，不存可执行插件定义、factory或活task。CSM与ContextStore writer不暴露给资源适配器。

Event service按channel隔离队列、cursor、背压和故障域，至少包含`job.events/{job_id}`、`resource.observe/{provider_instance}`、`resource.state/{owner_domain}`、`config.lifecycle/{domain}`和`context.source/{workspace_id}`。现有Job event bus成为通用`EventChannelService`上的typed adapter，不允许资源变化继续挤入job专属队列。observation event只携带`provider_instance_id`、内部`source_id/watch_key`、event sequence、dirty/change/gap/overflow kind和时间，不携带文件正文、diff、credential或模型上下文；重复、乱序、rename burst和overflow可以合并为dirty/gap，只有SourceReconciler有权重新读取来源，ResourceDerivationGraph才有权发布语义snapshot。业务durable state仍由各domain owner保存，event队列不是事实数据库。

内置file monitor只监视已由descriptor注册的精确文件或固定一层entry boundary；Skill根变化可以使catalog descriptor集合dirty，但reconcile仍只枚举固定`<root>/<entry>/SKILL.md`一层，不递归未知目录、不跟随越界symlink、不运行`rg`。Gateway全局Skill通过Gateway owner的受认证内部snapshot/revision提供给Workspace，team等内存状态只由权威owner的显式mutation event发布；它们共享source/semantic revision与CAS合同，但不能假装拥有文件双读语义。MCP工具目录由明确的`McpCatalogOwner`按启动、`tools/list_changed`、重连及配置candidate切换做完整分页`tools/list`校验和语义快照，发布轻量事件到独立channel；没有通知能力时只承诺显式重连/刷新或配置的有界轮询，不谎称立即生效。`McpToolGuidanceProducer`只从已验证工具目录派生确定性、受限长度的name/description/参数指引与增删改tombstone，作为明确登记的`tail_only` CSM source；原始MCP prompts/resources/server instructions不自动注入，未来若要消费它们另行设计。连接或配置candidate须shadow校验、原子发布、generation lease保护in-flight调用；失败保留旧valid generation并显式报告。watcher startup、overflow、Gateway snapshot重连或进程恢复都先把受影响descriptor标记dirty，执行有界initial/full reconcile并通过readiness gate后才允许依赖资源的新model dispatch；恢复不得扫描未登记工作区或猜测owner。

为避免“配置负责创建监视器、监视器又负责发现配置变化”的自举环，每个owner进程在代码中建立不可被动态配置移除的最小`ResourcePlatformBootstrap`。它启动本进程的`EventChannelService`、process-root `LifetimeScope`、内置file snapshot能力，并登记本进程配置域的发行内置配置、用户覆盖及允许的Workspace覆盖这些已知精确locator；Gateway bootstrap不得读取Workspace `.boxteam/workspace.jsonc`，Workspace bootstrap不得接管Gateway控制面配置。bootstrap不能注册模型source、取得ContextStore writer或执行业务reaction。config domain owner决定配置candidate创建、schema校验、readiness/health证明和原子发布：候选配置及其受影响的来源订阅在独立shadow scope中验证，失败只关闭candidate且旧active配置与来源绑定保持可用；成功先发布新配置/来源generation并以generation fence禁止旧scope再发布revision，然后排空旧scope。`LifetimeScope`只执行关闭，不判断candidate是否有效或何时切换。冷启动没有任何有效内置/合并配置时直接启动失败，不能以空图或旧字段兼容配置继续。动态配置只能变更bootstrap之下的已知source registration与业务policy，不能安装可执行provider/loader/reaction、关闭bootstrap自身或移除identity/integrity校验与事件故障报告。

来源身份与语义资源身份分开：来源registry持有`source_id -> ObservedSourceDescriptor`和实际owner私有locator/watch反向索引，语义`ResourceRegistry`只持有`resource_id -> SemanticResourceDescriptor`及其published revision。路径、URI和watch key都不是持久identity；同一`SKILL.md`形成metadata与activation两个语义资源，同一有效Workspace配置由多层JSONC来源合成，AGENTS链可依赖多个已登记文件，Session团队资源只能从权威ledger/内存状态适配派生而不能复制状态。两层descriptor至少包含：

```text
ObservedSourceDescriptor {          # provider-private，不进入模型、history或canonical item
  source_id                     # owner-scope内稳定，locator改变不自动改identity
  owner_scope
  provider_instance_id
  provider_locator_or_read_handle
  watch_key_and_options
  sensitivity
}
SemanticResourceDescriptor {       # 公开部分不含provider locator
  resource_id
  display_uri
  resource_kind
  owner_scope
  source_dependency_ids          # zero-or-more；也可依赖其它semantic resource
  resource_dependency_ids
  loader_id
  reaction_id
  capabilities
  sensitivity
}
```

SourceReconciler经代码内装配的来源适配稳定读取候选并按来源类型校验，发布不可变`ObservedSourceRevision(source_id, source_revision, raw_hash_or_version, snapshot_ref, availability, diagnostics)`；完整原始byte hash是file source的要求，Gateway内部snapshot与权威内存状态使用各自可验证的版本与一致性token，不能假装拥有文件的双读语义。`ResourceDerivationGraph`只消费已接受的来源revision及其它语义resource revision，执行版本化解析/facet extraction、依赖拓扑排序、typed semantic diff和CAS，发布不可变`ResourceSnapshot(resource_id, semantic_revision, semantic_hash, source_lineage, parsed_ref, availability, diagnostics)`。依赖图必须拒绝环与缺失required依赖；多输入快照必须在一致的generation/vector上导出，不能混合新旧来源。原始来源变化但目标facet payload不变时，不推进该语义resource revision，也不追加context item；多次未激活变化可由CSM相对最新可见已提交revision合并成一个delta。watch事件只标脏，不能直接增加任一revision。读取/解析失败保留上一份valid snapshot并显式发布unavailable/diagnostic状态；依赖required资源的新dispatch fail closed，不能用旧valid快照、空值或当前文件偷偷替代。

#### 5.1 Virtual Resource Namespace

VRN是模型与工具可见的逻辑地址层，不是provider文件系统。规范URI复用现有`boxteam://` scheme：

```text
boxteam://workspace/{workspace_id}/resources/agent-spec/root/AGENTS.md
boxteam://workspace/{workspace_id}/resources/skills/{skill_name}/SKILL.md
boxteam://gateway/{gateway_id}/resources/skills/{skill_name}/SKILL.md
boxteam://builtin/{distribution_id}/resources/skills/{skill_name}/SKILL.md
boxteam://memory/{scope}/{logical_resource_name}
```

同一资源同时具有四种不能混用的标识/定位：模型可见且可安全展示的`display_uri`；来源registry内稳定的`source_id`；语义Registry内稳定、跨rename/locator变化保持不变的`resource_id`；实际owner私有的`provider_locator`（绝对路径、Gateway内部snapshot引用、credential ref、memory key等）。`display_uri`表达逻辑scope/kind/name，但不是任一内部identity、授权凭据、dedupe key或业务幂等键。URI不携带revision；精确revision/hash/snapshot_ref由activation/assembly provenance另存。解析必须通过`VirtualResourceResolver.resolve(uri, operation, ResolutionContext)`得到绑定`resource_id/source_id/provider/语义revision/snapshot_ref/capabilities`的typed handle，不能percent-decode后直接join到文件系统。

规范parser只接受注册的authority/path grammar，percent decode恰好一次，拒绝userinfo、credential、未知query/fragment、控制字符、反斜杠、空segment、`.`/`..`、编码歧义和越界scope。核心功能的policy默认允许不等于取消这些identity/integrity约束。每次实际operation仍按当前principal、workspace/gateway binding、resource capability和activation snapshot校验；URI本身不授予读取、激活、监视或跨workspace权限。provider locator、内部handle、credential和绝对路径不得进入模型prompt、普通工具结果、history projection、日志或URI。

`skill_load`保持name-only：模型输入`name + mode`，软件在当前冻结的SkillCatalogSnapshot中把name解析为精确resource handle。成功结果和context item envelope可以返回`display_uri`说明来源，但`source_uri`属于ResourceProvenance而不是Skill metadata；name/description仍是唯一模型可见metadata。通用`read_file`只处理普通workspace文件，不能接受Skill VRN来建立activation；其它工具若要消费VRN，必须显式声明typed operation/capability，不能提供一个绕过loader/CSM的万能虚拟文件读写口。

#### 5.2 Resource activation boundary

Resource observation、domain desired state与thread applied context分离。provider/reconciler可以随时异步发布新snapshot，但不得因此唤醒每个thread、追加context item或改写sealed assembly。`ResourceActivationCoordinator`先在queue entry取得active execution slot时冻结不可变`ResourceActivationPolicySnapshot(policy_revision, policy_hash, default_boundary, effective_kind_boundaries)`，并据此建立有序`TurnResourceSnapshot`。同一Turn内policy snapshot永不重读或改变；config热发布的新policy只影响后续取得active slot的Turn，不能把正在运行的Turn从`turn`切到`model_call`或反向切换。

- `turn`（默认）：`TurnResourceSnapshot`冻结所有effective boundary为`turn`的resource binding，并作为该Turn的基线；该Turn内所有model call都复用这些binding。Turn期间的新变化只影响下一个取得active slot的Turn。
- `model_call`：若冻结policy中至少一个resource kind使用`model_call`，每次tool protocol收敛后的model-call preparation从Registry内存状态建立`ModelCallResourceSnapshot(parent_turn_snapshot_id, model_call_id)`；它逐字节复用parent中全部turn-bound binding，只重新冻结model-call-bound kind，并形成该assembly的组合snapshot。已sealed/in-flight call保持不变。

因此一个assembly可以同时包含turn-bound AGENTS和model-call-bound tracked Skill；effective boundary记录在每个resource binding上，而不是错误地给整个assembly只标一个`turn|model_call`值。`skill_load`必须用产生该tool call的ModelCall/Turn ResourceSnapshot解析名称与revision，不能在工具执行时跳到更新的catalog；这保证模型看到的Skill名称与实际加载来源一致。

ResourceActivationCoordinator只构造和校验typed snapshot，不直接写thread SQLite、JSONL或detail store；它把snapshot交给唯一Saver/ContextStore owner，具体schema、assembly binding、hash、恢复和history projection由`add-itemized-rollout-context`拥有。两份change之间不得各自建立snapshot writer或catalog。

配置只允许`turn|model_call`，不提供`immediate`、按请求次数、TTL或注入次数。默认值是`turn`，可按`agent_spec`、`skill_catalog`、`tracked_skill_activation`、`mcp_tool_catalog`等resource kind覆盖。MCP目录binding与其派生指引在同一边界冻结，不能只更新提示或只更新dispatcher。`model_call`也只读Registry内存snapshot/revision，不在请求路径执行stat/read/目录枚举或网络fetch。若对应resource仍dirty/gap/reconciling，required consumer等待有界reconcile或明确失败；不得绕过Registry自行读取。config自身的`immediate|next_job|restart`等runtime reload policy与“何时把上下文资源revision激活进模型”是两套不同维度，不能混成一个枚举；前者即使立即发布新的desired配置，activation policy仍只在下一Turn active-slot边界生效。

该公共配置属于Workspace配置域，使用现有`workspace_inline.jsonc → 用户workspace.jsonc → workspace_local.jsonc → ${workspace}/.boxteam/workspace.jsonc`递归对象合并；`workspace_dev.jsonc`只作为完整开发模板，不自动参与运行时合并。新增结构固定为：

```jsonc
{
  "context": {
    "resource_activation": {
      "default_boundary": "turn",
      "overrides": {
        "agent_spec": "turn",
        "skill_catalog": "turn",
        "tracked_skill_activation": "turn",
        "team_state": "turn",
        "mcp_tool_catalog": "turn"
      }
    }
  }
}
```

`overrides`按resource kind整体key覆盖，value闭集为`turn|model_call`；未列kind继承`default_boundary`。schema校验最终合并配置，未知boundary/kind和错误类型直接失败。该字段是新增且默认行为完整定义，不为不存在的旧字段设计alias、deprecated兼容分支或双内部模型；默认、schema、dev模板、配置诊断来源和focused merge/reload测试必须同时更新。若未来变更字段层级或既有值语义，再按Workspace `config_version`执行显式迁移，不能静默猜测。

Turn/ModelCall snapshot将`display_uri`映射到精确`resource_id + semantic_revision + semantic_hash + source_lineage_digest/ref + snapshot_ref`并交给ContextRequestPlan/assembly；source lineage只进入provenance/integrity合同，不因来源原始字节变化而令未变的模型可见facet重建plan hash。旧context item和旧assembly永远读取其已封存snapshot/detail，不通过display URI解析当前资源。tracked registration也绑定exact resource identity；同名高优先级Skill后来出现只改变未来catalog snapshot，既有registration不自动改绑。配置变更影响已知来源登记时由config owner在独立shadow scope中完成candidate校验、initial reconcile和health校验，成功后原子切换来源绑定generation、fence旧发布并drain旧scope；失败只关闭candidate，继续保留旧generation且显式诊断，不留下两套同时发布的monitor。

所有 Skill metadata、Skill activation、AGENTS 和其它文件 source 都经同一个 `StableSourceReader`，不能由 middleware 各自执行一次普通 `read_text`。reader 输入是 provider 所有的 `SourceReadHandle` 和允许根，不要求 ContextStore 能直接访问物理路径：workspace 文件可由 workspace provider 本地读取，Gateway global Skill 必须由 Gateway provider 读取后，以仅内部可用且受认证的 `StableSourceSnapshot(entry_identity, source_identity, revision_hash, byte_length, content_bytes)` 返回；该 envelope 和 handle 都不得进入模型、普通工具结果或 canonical history。handle 必须能在 Gateway/backend 重启后由持久 catalog entry identity 重新解析，不能依赖进程内临时路径 token。

一次稳定读取使用有界的双读确认，而不是把 `mtime` 当 revision：每个 attempt 都重新校验允许根 containment，拒绝非普通文件及不允许的 symlink，使用 no-follow handle 打开并在读前/读后比较文件 identity、size 和高精度时间签名；随后再次独立打开并读取，只有两次读都各自稳定、filesystem identity/signature一致且精确内容 hash 相同才接受。最多尝试三次，仍变化则返回 `source-changed-during-read` 并阻止本次候选、seal 和 dispatch。正文超过产品固定上限返回 `source-too-large`，不是截断；文本不是严格 UTF-8 返回 `source-invalid-encoding`。`source_snapshot_hash`覆盖接受的完整原始字节，换行、BOM、空白和 Unicode 不做隐式规范化；各logical facet另保存由版本化extractor产生的`facet_revision_hash`，只有模型实际可见的facet payload变化才追加context item。AGENTS等整文件facet直接使用原始正文；Skill metadata facet只序列化name/description，activation facet只使用frontmatter之后的精确正文bytes。持久 item再使用版本化确定性serializer。

SkillCatalog的published snapshot至少绑定catalog revision、entry/resource/source identity、metadata revision/hash、display URI和Registry-owned provider binding ref。`skill_load`只从当前activation snapshot取得已经稳定读取并发布的exact entry snapshot；若entry availability、catalog generation或metadata binding与activation snapshot冲突，则显式返回`skill-catalog-snapshot-conflict`，不得在工具调用路径重新枚举目录、读取文件或把旧名称映射到新文件。tracked registration只保存resource/source identity和catalog/provider binding revision，不保存或解析provider-owned handle/locator；后续activation boundary读取Registry中原resource的最新published snapshot，因此同名优先级变化不会隐式改绑。

模型只看到当前有效 entry 的 `name` 和 `description`。frontmatter通过禁用custom tag/alias/object构造的安全解析器处理，name/description必须是唯一、有界scalar；语法错误、重复字段或类型错误使entry显式invalid。其它字段可存在但暂不进入metadata/activation payload、facet revision或策略判断；它们改变时`source_snapshot_hash`可变化，两个facet payload不变则不追加metadata/activation item。activation正文从已接受snapshot的frontmatter结束byte offset后精确取得，不把未支持字段带入模型。工具输入`name`只作为已构造catalog map的精确key，统一校验长度、控制字符、分隔符、`.`/`..`和编码歧义，绝不用于路径拼接；路径解析和稳定读取均在软件内部完成，路径不得进入模型可见参数、普通工具结果或历史正文。

activation registration绑定`name + catalog_entry_identity + resolved_source_identity`，不是只绑定可被覆盖的name；同时每个`(session_id, thread_id, normalized_skill_name)`至多一个active tracked registration。snapshot/tracked自动检查都不会因同名workspace/global/bundled优先级变化而静默改绑另一个文件；原tracked source不可读时返回明确`tracked-source-unavailable`并阻止未记录的dispatch。模型显式再次调用`skill_load(name, tracked)`时，若effective entry identity未变则幂等复用；若已变化，则在一个owner事务中把旧registration冻结为untracked、为当前effective entry追加完整activation并建立新tracked registration，旧context item保持不变，结果返回不含路径的`source_rebound=true`。同一source revision已在active view可见时，重复snapshot调用返回`already_active`而不追加；revision不同的显式snapshot调用可追加新的不可变activation。tool invocation retry仍以调用幂等键优先恢复原结果。

Metadata 与 activation 独立生效：metadata 让模型知道可选 Skill；activation 是 `skill_load` 后实际进入上下文的 Skill 正文。首次组装已有 metadata 可进入 root system item，运行中 metadata 变化只能追加 user-role delta，不能改写 root item。

#### 5.3 目标源码目录与依赖边界

以下是实施后的**目标目录**，不是当前文件系统已存在清单；新建的每个源码子目录都必须同时提供四段式`AGENTS.md`。文件名仅示意owner，不要求为同一职责再造第二套入口：

```text
app/
├── core/lifecycle.py                            # 唯一进程内LifetimeScope/释放handle；无业务策略
├── abstractions/                                 # 内置来源适配/registry/activation的typed ports；测试可替换
├── domain/
│   ├── resources/                               # 来源与语义revision、依赖和snapshot的纯值对象
│   └── itemized/                                # sealed context/ref与canonical领域事实（既有）
├── services/
│   ├── infrastructure/
│   │   ├── events/                              # 跨业务channel的短期通知传输与backpressure
│   │   ├── external_resource_leases.py          # 跨Turn外部资源的持久操作占用/恢复账本
│   │   ├── mcp/                                 # MCP连接、完整工具目录relist与generation lease（既有目录重构）
│   │   ├── tool_catalog_service.py              # Provider直接工具与ExtensionToolCatalog的desired视图（既有）
│   │   ├── resource_platform/                   # 进程内资源流水线；代码内装配
│   │   │   ├── bootstrap.py                     # 固定启动根与已知配置locator
│   │   │   ├── observation/                     # provider内部共享watch、引用计数与dirty/gap事件
│   │   │   ├── sources/                         # 私有locator、稳定读取与ObservedSourceRevision
│   │   │   ├── derivation/                      # source→semantic依赖DAG、解析调度与CAS
│   │   │   ├── registry/                        # 已发布语义ResourceSnapshot与只读索引
│   │   │   ├── virtual_resources/               # boxteam://语法、身份绑定与typed resolver
│   │   │   └── adapters/                        # 内置file、Gateway内部snapshot、权威memory适配
│   │   ├── config/                              # 既有配置层合并、schema与reload owner
│   │   └── rollout_context/                     # 既有SessionThread唯一I/O owner
│   │       ├── runtime/context_sources/         # CSM registration、diff与恢复决策
│   │       ├── assembly/                       # frozen activation ref和sealed selection
│   │       ├── checkpoint/                     # 唯一ContextStore/Saver writer
│   │       └── storage/                        # thread-local SQLite/JSONL/detail持久化
│   ├── business/resource_derivations/            # AGENTS/Skill/team/MCP工具指引的业务解析与reaction policy
│   ├── business/session_resource_providers.py    # 会话资源控制适配；委托实际owner核实
│   ├── orchestration/resource_activation/        # active-slot冻结、Turn/ModelCall绑定与readiness
│   ├── orchestration/thread_residency.py          # thread idle/lease/generation准入与scope关闭时机
│   └── mapping/itemized/                         # 不读源的history/Provider DTO纯投影（既有）
└── agents/tools/                                 # name-only skill_load与固定invoke_extension_tool适配（既有目录重构）
```

| 目录 | 精确职责与禁止跨越的边界 |
|---|---|
| `app/core/lifecycle.py`、`app/abstractions/` | `lifecycle.py`只实现唯一进程内`LifetimeScope`和可撤销释放handle，可用标准库`AsyncExitStack`/task supervision；它不实现cancel理由、idle policy、watch key、generation发布或持久外部资源停止。`abstractions/`定义已知来源读取、共享文件监视、业务解析、Registry查询、activation snapshot等typed port，供代码内装配和测试替身使用；文件订阅返回可被scope持有的释放handle，不暴露可安装插件API。 |
| `app/domain/`、`app/domain/resources/` | `domain/`只承载纯领域规则；`resources/`冻结`source_id`/`resource_id`、来源/语义revision、依赖边、availability、typed snapshot ref等不可变值对象和校验，不持有provider locator、路径、文件句柄、event queue或数据库。 |
| `app/domain/itemized/` | 保存既有canonical item、ContextRef、ResourceActivationSnapshotRef与ResourceProvenanceRef的纯schema/hash；不把ResourceRegistry的当前状态当历史事实。 |
| `app/services/`、`app/services/infrastructure/` | 前者按基础设施/业务/编排/映射分层；后者只实现I/O与进程级设施，不吸收Skill解析规则、Agent决策或Session业务事实。 |
| `app/services/infrastructure/events/` | 通用`EventChannelService`拥有按channel隔离的queue/cursor/backpressure/gap；`job.events`及resource/config/context均以typed adapter接入。它是短期通知传输，不是durable事实库，也不替代现有Session事件提交。 |
| `app/services/infrastructure/external_resource_leases.py` | 旧`ResourceManager`收敛后的跨Turn操作占用账本，只保存经实际owner验证的typed资源identity、operation/lease identity、状态与恢复引用；不猜工具参数、不存`cleanup_policy`或不可恢复的stopper、不决定关闭terminal/browser/MCP。持久SessionOperationLease、runtime generation lease与共享watch引用各守其原有owner/持久性边界，不强行合并成一个计数器。 |
| `app/services/infrastructure/mcp/`、`tool_catalog_service.py` | MCP目录owner负责协议能力、完整分页relist、语义hash、通知/重连、shadow发布与generation lease；工具目录service只组合Provider直接集合、固定信封和独立ExtensionToolCatalog desired视图，不把target清单塞进Provider ToolSetRef。两者都不拥有CSM、指引文本或Session writer。 |
| `app/services/infrastructure/resource_platform/` | 资源设施的composition root；在Gateway/Workspace各自代码中显式装配已知适配、loader和owner reaction，不成为巨型`ResourceManager`、插件宿主或第二ContextStore。 |
| `.../bootstrap.py` | 固定启动本进程事件服务、process-root scope、内置file snapshot能力和已知配置locator，不受动态配置关闭。config owner可借独立`LifetimeScope`验证受影响的候选来源登记和有效配置，完成readiness/health后原子发布并关闭旧scope；没有可执行贡献注册表、plugin manifest或通用发布策略。 |
| `.../observation/` | provider monitor订阅、规范化watch key、内部引用计数、防抖及dirty/gap/overflow事件；每个consumer取得可释放handle，最后一个释放才停止底层watch。同资源可供UI和多个业务consumer共享，但不同filter/correlation语义不得误共用。只标脏，不读正文或推进revision。 |
| `.../sources/` | 实际owner私有source descriptor/read handle映射、`SourceReconciler`和不可变`ObservedSourceRevision`；file稳定双读及根边界在这里执行，Gateway内部snapshot与权威memory状态用各自一致性token。locator永不出现在模型、普通history或sealed context。 |
| `.../derivation/` | `ResourceDerivationGraph`解析依赖DAG、调度typed loader、校验多输入版本向量、执行语义diff/CAS；这里只放通用求值框架，AGENTS/Skill/config/team的领域规则由其owner在代码中实现。 |
| `.../registry/` | `ResourceRegistry`发布/查询不可变语义revision、readiness和generation；不拥有provider读取、目录扫描、Session SQLite写入或tool结果。旧valid与当前unavailable必须可区分。 |
| `.../virtual_resources/` | `boxteam://` parser、scope/operation/capability校验与typed resolver；URI仅作安全展示与解析入口，不是稳定identity、文件挂载、授权凭据或历史重读入口。 |
| `.../adapters/` | 文件、Gateway全局Skill受认证内部snapshot和权威内存状态的内置适配；各owner只在本进程持有locator/credential，Workspace不能猜测Gateway物理路径。生产装配在代码中显式维护，测试可注入替身；新增外部服务能力优先走MCP，不借此目录动态安装资源插件。 |
| `app/services/infrastructure/config/` | 现有config owner仍负责JSONC schema、layer merge、candidate validation与runtime reload。Gateway与Workspace配置域分别合成有效配置资源；资源平台负责观察来源/调度派生，不复制配置merge或把`workspace_dev.jsonc`变成隐式运行层。 |
| `app/services/business/resource_derivations/`、`app/agents/tools/` | 前者实现MCP工具指引等业务语义派生与source owner根资格声明，不持有MCP连接或CSM writer；后者以固定`invoke_extension_tool`工具适配封存的ExtensionCatalogBindingRef并执行权限校验，旧`custom_invocation`命名在实施时直接替换，不保留工具别名或双schema。 |
| `app/services/business/session_resource_providers.py` | 会话资源列表/控制适配委托terminal/browser/后台任务等实际owner核实资源状态、决定业务关闭/删除及副作用；既有`SessionResourceProviderRegistry`只路由，不与外部lease账本并行维护第二套资源状态。工具端通过typed resource-use声明占用，不能在通用层解析`terminal_id`/`pageId`等参数名并自动伪造资源登记。 |
| `app/services/orchestration/thread_residency.py` | itemized change拥有的SessionThread runtime准入、generation/lease fence、idle时钟与cold投影owner；它决定何时关闭该generation的`LifetimeScope`，但不能另实现dispose、共享watch、持久外部资源停止或ContextStore第二writer。 |
| `app/services/infrastructure/rollout_context/` | 既有SessionThread唯一I/O owner；仅消费冻结的资源结果，不直接监视或读取当前源。其未列出的`execution/operations/provider/migration`等既有子目录职责维持`add-itemized-rollout-context`的拆分合同。 |
| `.../rollout_context/runtime/`、`.../runtime/context_sources/` | `runtime/`保存线程运行期协调；`context_sources/`独占CSM registration、latest-visible-committed基准、delta合并、rewind/compaction恢复决策；只能提交给唯一owner，不能拥有watcher或第二ContextStore。 |
| `.../rollout_context/assembly/`、`.../checkpoint/`、`.../storage/` | `assembly/`封存selection/activation provenance和稳定wire帧；`checkpoint/`是唯一ContextStore/Saver提交入口；`storage/`保管thread-local事实与受保护detail。三者都不能从当前资源URI/路径补造旧assembly。 |
| `app/services/business/`、`.../resource_derivations/` | `business/`承载业务规则；`resource_derivations/`贡献AGENTS合成、Skill metadata/activation与catalog、team ledger投影的loader/reaction，负责业务语义与优先级，不实现watcher或私自发布第二registry。team ledger仍是权威状态；业务reaction不直接写thread context。 |
| `app/services/orchestration/`、`.../resource_activation/` | `orchestration/`拥有流程时机；`resource_activation/`在active slot冻结policy/Turn snapshot，在安全model-call边界只读取Registry内存快照，检查readiness并向CSM/唯一Saver提交typed decision。它不持久化、重新读源或中途改写已seal前缀。 |
| `app/services/mapping/`、`.../itemized/` | 只把已提交sealed selection/provenance映射为history、LangChain或Provider DTO，不读取当前Registry、provider locator或ContextStore底层表。 |
| `app/agents/`、`.../tools/` | Agent层只提供模型调用面；`tools/`里的`skill_load`只接受name/mode并调用当前冻结catalog+CSM端口，不能自行拼路径、读`SKILL.md`或建立第二追踪状态。 |

作用域固定为：Gateway进程拥有全局Skill source/catalog及其monitor；Workspace进程拥有本地source/provider、Registry和共享watch；Session只拥有目录/协作控制；SessionThread拥有CSM registration、ContextStore及sealed assembly；active execution仅持有冻结activation snapshot。`LifetimeScope`的所有权只描述进程内对象，不重定义这些durable owner。child idle卸载只释放其线程运行期对象与订阅引用，不删除进程级watch、语义revision或已提交上下文。来源变化可异步发布，但只能在配置的`turn|model_call`边界变为新的尾部item；只有首次组装、实际compaction、rewind重建、ToolSet hard rebase能改变prefix epoch，普通revision/监视事件不能重写旧wire bytes。

外部terminal/browser/MCP/dev-server/Node调试进程等业务资源不是`LifetimeScope`所拥有的进程内句柄。现有`ResourceManager`在工具参数名中猜`resource_id`、将`cleanup_policy`混入通用账本，并以无法跨进程恢复的stopper执行stop；重启后stopper缺失却仍可能持久化`stopped`。实施时删除该通用控制入口，保留真正需要的跨Turn持久operation lease语义于唯一`ExternalResourceLeaseLedger`；工具/provider以typed resource-use和实际owner验证resource identity/归属后才能登记占用。debug owner提供精确`(workspace_id, session_id, thread_id, debug process identity)`的typed`node_debug_process`占用/恢复引用，核实`starting|running|paused|stopping`及`reconcile_required`阻断后向residency owner提供状态，不把Node停止策略放进通用账本；独立Web/API调试mutation在同一Session gate建立`debug_control`准入lease，Agent内部已有execution lease可覆盖，避免删除与启动交错。用户取消Turn时domain owner先决定停止还是仅释放该operation lease；若其它lease仍有效，不得凭一个holder的结束停止共享外部资源。实际停止、删除、恢复和状态核实必须经终端/浏览器/MCP/调试等原owner的typed operation，成功证据提交后才更新lease/资源投影；缺少owner/连接或核实失败时返回明确错误或`reconcile_required`，不能宣称已停止。`SessionResourceProviderRegistry`只路由可见列表/控制，其响应必须来自实际owner，不持有另一份独立status。持久`SessionOperationLease`保护Session删除/创建等准入，resident runtime generation lease保护卸载，monitor引用保护共享watch，它们虽都叫lease，却分别具有不同的恢复和所有权语义，不能合并为一个万能计数器。

Node的跨Turn资源占用由debug owner按每次启动唯一process_instance_id持有，并在spawn前持久登记`launch_pending` claim，绑定精确thread、lifecycle generation、launch preimage与nonce；短期`execution|debug_control` lease只覆盖该次操作准入，结束不释放process claim。spawn后debug owner以nonce、OS进程起始身份与Inspector握手核验后才把PID/端口登记为实例属性。崩溃恢复核查同一实例：可证明未spawn/已退出则结清，可证明仍运行则接管或按owner策略收敛，不可证明则保留`reconcile_required`及idle/delete blocker并给出诊断，不凭PID/端口复用误杀无关进程。重启/替换旧实例必须先核实并结清其claim，再创建新的process_instance_id；通用账本不查询OS、不持有业务stopper、不决定哪个Node进程可终止。

### 6. `skill_load` 使用一个工具和三个明确 mode

工具 schema 固定为：

```text
skill_load(
  name: string,
  mode: "snapshot" | "tracked" | "untrack" = "snapshot"
)
```

不存在其它即时移除工具。工具结果只返回名称、实际 mode、revision/hash 的安全标识和是否追加 source item，不返回内部路径或完整 Skill 正文。

#### snapshot

CSM 通过当前activation snapshot的SkillCatalog绑定解析名称，取得ResourceRegistry已稳定发布的 `SKILL.md` revision，追加一个完整、不可变、ambient 的 activation item。由于调用发生在用户消息之后，该 item 投影为 `wire_role=user`。工具调用不读文件、网络或provider；随后不检查变化、不维护 active tracking registration，也不检查 rewind 是否移除了该 item，它只依靠 active context 自然保留且不自动到期。

存储仍保留 captured revision/hash、catalog entry identity、item identity 和受保护正文，以便 replay/audit；“不追踪”不等于丢弃已提交 provenance。

#### tracked

CSM 保存 resource/source identity、catalog/provider binding revision、当前 observed/applied语义revision/hash、tracking enabled 状态，以及 checkpoint-versioned registration；provider-owned内部 read handle/locator只存在于来源registry/provider私有状态。每个配置的`turn|model_call` activation boundary对active tracked source消费一次语义ResourceRegistry published snapshot：

- 文件未变化且最新 committed revision 仍在 active view：不追加 item；
- 文件变化：从 active view 中最新已提交可见 revision 计算到当前 revision 的一个 delta，并以 `wire_role=user` 追加；
- registration 在 rewind 目标 checkpoint 中仍为 tracked，但最新已注入 revision 不在重建后的 active view：从冻结activation snapshot取得并持久化published完整revision恢复事实；实际新epoch中只把`root_eligible`状态合并新root，`tail_only`仍用独立`wire_role=user` item；
- 同一文件在两个 model request 之间多次变化：只提交从最新已提交可见 revision 到最终稳定 revision 的一个 delta。

revision 只有在对应 item 进入 sealed assembly 后才算 committed/injected。仅观察到变化或生成临时候选不能推进 diff 基准。seal 前失败保留 pending candidate；retry 必须复用同一 candidate identity/bytes。

#### untrack

`skill_load(name, mode="untrack")`先按当前thread和受校验逻辑name查找唯一active tracked registration，不按当前SkillCatalog优先级重新解析entry，也不读取source；因此同名高优先级Skill后来出现时，仍能停止原registration。没有active registration返回`not_tracked`，检测到多个active registration则返回`tracking-state-conflict`并fail closed。命中时只把当前checkpoint中该registration切换为untracked/frozen：

- 该registration不再请求或消费新revision；共享monitor/Registry可为其它consumer继续观察同一resource；
- 不再因为 rewind 缺失而自动恢复；
- 不追加 source 正文、撤销说明或覆盖指令；
- 不删除、重写、重排已经提交的 activation/base/delta；
- 已有内容继续按 active context、rewind 和 compaction 的普通规则自然保留或移出。

对 snapshot 或不存在的 tracked registration 调用 `untrack` 返回确定性的 `not_tracked` 结果，不伪造成功状态。之后再次调用 `mode="tracked"` 可以从当前 active view 的最新可见 revision 恢复追踪；若无可见 revision，则从冻结activation snapshot追加published完整 revision。

tracked source的Registry snapshot不可用会在activation boundary返回`tracked-source-unavailable`并阻止dispatch，因此不能把模型再次调用工具当作唯一恢复路径。受信Session控制API必须允许用户以精确`session_id + thread_id + normalized skill name + mode=untrack`调用同一个CSM mutation，并返回与工具一致的安全结果；API不得接受路径、不得读取source、不得删除既有context item，也不得实现第二套tracking状态或“立即卸载Skill”语义。该入口只解决模型无法运行时的人工控制面可达性，权限校验、幂等、checkpoint-versioned untrack和`tracking-state-conflict`合同与模型工具完全相同。Web若暴露操作，只能消费该API返回的完整状态，不得本地伪造untracked。

CSM tracking state 与 checkpoint 一起版本化。rewind 到 `untrack` 之前的 checkpoint 会恢复当时的 tracked 状态；rewind 到其后的 checkpoint 保持 untracked。这避免控制状态脱离产生它的上下文因果点。

### 7. diff 基准是最新已提交且仍可见的 revision

CSM 区分：

```text
observed_revision
pending_revision
committed_revision
latest_visible_committed_revision
```

生成 delta 时只使用 `latest_visible_committed_revision` 作为 from revision。多个尚未进入 sealed assembly 的 observed/pending 变化可以在内部合并，但不能先写成 canonical item 再删除；最终只把一个 delta item 提交给 Saver。delta manifest 保存 from/to revision、base/target hash、deterministic diff hash、source identity 和被合并的内部 observation references。

同一个 tool call、model attempt、prepare 或 seal 重试通过稳定 idempotency key 复用结果。若同一 identity 对应不同正文/hash，则报告 conflict，不能按文本相似度去重。

### 8. rewind 与 compaction 建立新 epoch，并按 owner 根指令资格物化

在建立 view 或 pending epoch transition 前，resolver必须验证结果 active view 的tool protocol closure：assistant tool-call group 与全部匹配terminal results必须整体位于边界同侧。若compaction、rewind/replay或fork anchor切入该group，操作返回`tool-protocol-boundary-conflict`、保持旧view且不创建transition/target，并只给出不含正文的最近安全anchor；不得自动偏移边界、合成result或借source item掩盖未闭合协议。只读history可展示明确标记的partial group，但不能进入sealed assembly或Provider dispatch。

rewind先从目标checkpoint恢复checkpoint-versioned CSM registration，重建active view并提交`PendingPrefixEpochTransition(reason=rewind)`：

- snapshot item 在 cutoff 内则自然保留，在 cutoff 外则消失，CSM 不恢复；
- tracked registration 若仍有效且当前 revision 不在 active view，CSM 从冻结的Registry activation snapshot追加published完整 revision；
- untracked registration 不检查、不恢复。

rewind可以在不继续执行时只提交view和pending transition；下一次真正model-call preparation从activation coordinator冻结的ResourceRegistry内存snapshot取得tracked published revision，并在同一transaction提交恢复事实、seal首个`rewind` epoch assembly及消费transition。snapshot不可用或seal失败时保留rewind view与pending transition，但不追加半成品source、不应用新epoch、不dispatch；该路径不得读文件、网络或其它provider。合法新epoch中，只有owner声明`root_eligible`且仍在目标active view有效的source完整状态可合并进新root；`tail_only`的tracked完整恢复作为独立`wire_role=user` item。恢复事实/lineage必须持久化且旧sealed assembly不变，不能把旧delta再次选入新plan造成重复。

compaction提交summary/active view时登记`PendingPrefixEpochTransition(reason=compaction)`。下一次model-call preparation可以从activation coordinator冻结的ResourceRegistry内存snapshot取得tracked published revision，把原base+delta链物化为该完整revision，并在同一transaction推进source overlay epoch、seal首个compaction epoch assembly和消费transition。snapshot不可用或seal失败不回滚已提交的compaction view，但不产生半应用epoch或Provider request，也不回退读源。实际新epoch只将`root_eligible`的完整有效状态合并到唯一顶层system root；`tail_only`仍位于用户消息之后并使用独立`wire_role=user` item。新plan只选择物化后的有效状态一次，旧base/delta/detail保持不可变，直到retention/GC确认没有sealed assembly依赖。

snapshot/untracked 内容不允许重新读取源文件。压缩器只能根据 active view 和已提交受保护 detail 决定是否携带；CSM 不执行自动恢复。若压缩没有实际重建上下文，则不得借 compaction 名义改变旧前缀。

### 9. 当前生产上下文来源清单是迁移闭包

下表是本 change 编写时对生产代码的逐项审计结果。表中 ID 是迁移与测试追踪号，不是持久化 `source_identity`。ContextStore mutation owner统一所有变更的提交、排序和 seal；CSM只统一“指令、文件与运行时控制 source”的生命周期，不吞并真实用户输入、Provider 工具协议、模型输出、ToolSet或 compaction summary 的既有 domain owner。

| ID | 当前生产来源与入口 | 当前触发、落位与生命周期 | 当前问题 | 本 change 的详细目标 |
|---|---|---|---|---|
| R01 | Agent 基础说明：`ConfigService.get_agent_runtime_config()` 读取 agent `instructions.system_prompt`，`agent_factory.create_my_deep_agent()` 传给 `create_agent(system_prompt=...)` | Agent 构建时形成基础 `SystemMessage`，之后作为每次请求的 system prompt 起点 | 只有最终拼接文本，没有独立 source revision/facet；运行中配置变化无法只追加 | 注册 `agent-instructions` root source；首次组装封入唯一 root system item并记录配置 revision/hash；首个 user item 后发生变化只能追加 user-role full/delta，不能重建 root |
| R02 | 运行时身份与路径：`agent_factory._runtime_identity_system_prompt()` 注入 workspace 绝对根、首选/fallback provider/model 和路径规则 | 每次 Agent runtime 构建时追加到基础 system prompt | 与 R01 原地拼接，provider/config 重建可能改变旧前缀；字段 provenance 不可独立校验 | 注册 `runtime-identity` root source；稳定规范化 workspace/provider/model snapshot；首次进入 root，后续变更按独立 user-role source item追加；不得借 Agent 重建改写旧 assembly |
| R03 | Session内部团队静态规则：启用 `create_team` 时 `_team_aware_system_prompt()` 追加 `TEAM_COORDINATION_SYSTEM_PROMPT` | 按最终可见工具集条件，在 Agent 构建时拼入 system prompt；当前team member仍由delegated Session表达 | 工具策略与 prompt 拼接隐式耦合，缺少“为何启用”的 source 事实；跨Session成员模型与新的SessionThread边界冲突 | 注册条件化 `team-coordination-policy` root source并绑定精确ToolSet revision；运行中启停走C02 hard rebase，在新epoch重编译条件化root。policy明确team只管理当前Session的child thread，跨Session只允许send/read/wait，不向旧epoch追加软policy通知；动态团队事实另走E04 |
| R04 | Todo 静态规则：`TodoListMiddleware(system_prompt=TODO_SYSTEM_PROMPT)` | middleware 在 model request 前追加 system block；`TODO_TOOL_DESCRIPTION` 同时改变 tool schema | 每请求重新拼接，靠后置捕获猜测边界；prompt 与 tool schema owner 混在 middleware 行为里 | `TODO_SYSTEM_PROMPT` 注册为绑定 ToolSet policy的条件化 root source；`TODO_TOOL_DESCRIPTION` 归 C02 ToolSetSnapshot；启停通过同一 desired revision在 safe boundary hard rebase并决定新 epoch是否 included |
| R05 | Skill metadata/index：`WorkspaceSkillsMiddleware`/upstream `SkillsMiddleware` 发现 bundled 与 workspace Skill，向 system prompt写入 locations、name、description、路径和 `read_file` 指令 | metadata 在 `before_agent` 读取一次，格式化后的 catalog 在每次 model request 前追加；当前未发现 `${BOXTEAM_HOME}/skills` | 路径泄露给模型；metadata 与 activation 混合；通过通用 read 工具激活；catalog revision、覆盖关系和运行中变化没有统一事实 | Resource platform持续维护三层SkillCatalog snapshot与`skill:*:metadata` facet，只暴露name/description及安全display URI；activation boundary消费冻结snapshot，首次metadata进入root，后续catalog diff使用user role；activation只能由`skill_load`建立。删除旧middleware位置提示、`.boxteam` Skill mount暴露和通用read白名单。现有`allowed_tools`若仍需用于事件归因，迁到独立工具归属配置 |
| R06 | 文件系统静态规则：`FilesystemMiddleware(system_prompt=FILESYSTEM_SYSTEM_PROMPT, custom_tool_descriptions=...)` | 每次请求前追加 filesystem/environment system block，并注册/改写文件工具说明 | system block 依赖请求期拼接；其中 Skill 路径例外与旧 read 激活方案绑定；工具说明不是独立 ToolSet 事实 | 文件系统行为规则注册为绑定 ToolSet policy的 root source并删除 Skill 读取例外；所有 tool descriptions 归 C02；工具 policy变化触发 hard rebase并重编译新 epoch，不能重写旧 context item |
| R07 | 手动压缩工具静态规则：`CachePreservingSummarizationToolMiddleware(... COMPACT_CONVERSATION_SYSTEM_PROMPT)` | 启用 `compact_conversation` 时，每次 model request 前追加 system block并提供工具 | 与压缩派生请求、压缩结果的生命周期混在一起 | 静态使用规则注册为绑定 ToolSet policy的条件化 root source；工具 schema与启停归 C02并以 hard rebase生效；真正的派生摘要请求与结果归 D01，不把它们伪装成同一 CSM source |
| R08 | 工作区 `AGENTS.md`：`WorkspaceAgentsMiddleware` 初次读取完整内容，`before_model` 检测变化后追加 `workspace_agents_change` `HumanMessage`，发现 compaction marker 时又把当前完整内容拼回 system prompt | 每次 model request 前读单文件；变化时生成 unified diff；middleware 内存保存 applied/observed 内容 | 直接改 LangChain state/system prompt；控制状态不随 checkpoint 版本化；重启/rewind 容易重复或错基准 | 注册`workspace-agents:content` resource/source，由实际owner声明`root_placement`；共享file monitor标脏，Reconciler稳定读取并发布snapshot，普通激活边界按latest-visible-committed追加user-role delta；实际rewind/compaction新epoch仅在资格为`root_eligible`时将完整有效状态合并新root，否则恢复为独立user item。状态由Saver/ContextStore持久化，删除旧middleware直接读盘/拼接和消息注入，不运行`rg`扫盘 |
| R09 | Agent memory：`StructuredMemoryMiddleware` 可读取配置的 memory sources，并用 `MEMORY_SYSTEM_PROMPT` 包裹为 system block | 仅 `create_my_deep_agent(memory=...)` 非空时启用；当前默认生产 runtime 未传入，属于已实现但未接线能力 | 一旦启用仍会每请求拼接，正文与 source revision 无统一 owner；当前配置状态容易被误报为已生效 | 保持默认未启用；任何生产接线前必须把 memory descriptor/content 注册为独立 source：首次可进 root，后续变化按 user-role source item；memory 是不可信 reference，不能借 CSM 提升优先级 |
| E01 | main-thread Goal生命周期：`goal_continuation`、`goal_objective_updated`、`goal_budget_limited` | `GoalRuntimeService` 构造`PreparedInternalMessage`，再用`create_and_run_internal()`创建`MessageRole.user`消息和Job；当前只以Session定位 | 内部控制状态成为普通user message/Turn root，注入与execution唤醒绑死；若按thread机械复制会错误地让child拥有Goal | Goal capability只允许Session main thread；作为`goal:<goal_id>:state` ambient/pending event source提交，保留事件revision与用户目标边界并由独立wakeup启动execution，不创建真实user Turn。child thread收到Goal操作必须明确拒绝，不能fallback到main |
| E02 | Session内委派与跨Session生成结果：`delegated_task`、`generated_session_result` | `SessionSubagentService`或session-generation reporting通过internal message准备/派发Job；新生成Session的seed prompt另有`prepare_user_message()`路径 | durable委派当前创建child Session且回报只按Session定位；系统委派/回报可伪装user root | durable委派改为同一Session的child thread，任务seed进入精确child，完成汇报按持久parent thread地址注册幂等ambient event；不复制child history。生成Session result回到发起thread；seed由producer显式声明`user_derived_root`或`internal_source`，禁止根据文本/role猜测 |
| E03 | 跨Session主thread协作：`session_message`、`read_context`、`monitor_session_agent_end`；本地或Gateway远程路由 | 当前`send_message_to_session`向模型暴露`simulate_user`；`read_context`返回JSON工具结果；`monitor_session_agent_end`轮询并订阅当前及未来Job的`AGENT_END`，目标都只到Session | Agent可伪造真实user ingress；send→monitor存在queued/accepted竞态和无界未来订阅；read结果与目标canonical复制边界未明确 | 模型侧send删除`simulate_user`并返回resolved target、`communication_id`及job/turn binding；read返回有界projection并只作为调用方canonical tool result；以接受`communication_id | job_id | turn_id`的有界`wait_for_session`替换旧monitor，communication selector可等待execution binding。三者只解析目标main，read/wait不修改或materialize目标runtime，跨Session协议拒绝team/task/role/Goal状态 |
| E04 | Session内部团队动态事实：`team_membership`、`team_task_assignment`、`team_task_update` | Team service在持久化board/task后通过internal message启动目标delegated Session Job | 动态团队状态和R03静态规则没有facet分离，以Session ID表示成员，并生成普通user Turn | team ledger只属于一个Session，使用稳定member identity映射child thread并保存coordinator/assignee thread；分别注册membership/task source identity与revision并向精确thread提交ambient/pending user-role item。删除跨Sessionattach/state同步，不轮询重发，不创建user root |
| E05 | 后台终端完成：`terminal_execution_completed` | `TerminalSteeringService`在资源完成后通过internal message唤醒owner Session | 完成事实成为普通user message；delivery与terminal resource identity没有统一source幂等；cold thread callback可能误投main | 注册terminal execution event source，绑定resource/execution/terminal outcome、精确callback thread和delivery idempotency；只追加一次user-role runtime item并独立唤醒。thread已idle unload时按持久binding重新取得当前owner，不能写旧runtime或改投main |
| E06 | 同一执行内 retry/recovery：`missing_custom_tool_retry`、`delegated_report_retry`、`session_question_reply_retry`、`empty_response_retry` | execution step 直接构造 `HumanMessage` 作为下一次模型输入；`tool_test_retry` 只存在测试 harness | 绕过 Saver source 登记；重试次数、message id 与 sealed attempt 可能形成第二事实源 | 每种 retry 使用稳定 source kind、attempt/idempotency key注册 request-bound 或 ambient control item；仍投影 user role但不是 user Turn；重复 attempt复用 sealed bytes。`tool_test_retry` 明确排除生产迁移闭包 |
| E07 | checkpoint/runtime reminder：`checkpoint_reminder`，覆盖 interrupt、startup/turn/scope/tool timeout、execution lost/error、resource cancel 等原因 | `append_system_reminder_checkpoint()` 直接读取并改写 LangGraph checkpoint messages，追加 `HumanMessage` | 绕过 canonical item、ContextStore transaction和 source owner，是最明显的第二 writer | 删除直接 checkpoint message mutation；按 `checkpoint:<reason>:<execution/resource>` identity 经 Saver/CSM 原子提交 pending source/control outcome；恢复只读已提交事实，不从当前 runtime补造 |
| D01 | 压缩派生上下文：`compaction_summary_instruction`、`compaction_retry_marker`、派生请求 fallback `SystemMessage("Summarize...")`，以及返回主上下文的 `compaction_summary` | summarization middleware建立独立模型请求；instruction/retry 是 `HumanMessage`；结果替换 active history 中段 | 静态压缩工具规则、派生请求控制项、最终 summary 和 source materialization 容易混为同一注入来源 | 派生摘要请求使用独立sealed derived assembly和自己的root system item；instruction/retry是该derived assembly的request-only control；结果由compaction owner持久化为canonical `compaction_summary`并登记pending prefix transition，不是CSM source；CSM只在下一次model-call preparation内按规则物化各tracked source一次并与首个新epoch assembly原子seal |
| C01 | 真实用户输入与附件：可信UI/API ingress、replay-as-new-turn，以及producer显式标记的user-derived generated seed | `UserContentBuilder`组装文本/多模态blocks，runner创建`HumanMessage`并持久化user root；当前模型侧session消息工具也可用`simulate_user`进入该路径 | 当前`MessageRole.user`同时被内部消息复用，且模型工具可伪造可信用户来源；role无法证明真实用户来源 | 不进入CSM；只有可信UI/API ingress owner可构造`AppendCanonicalItemIntent`，经统一ContextStore owner创建唯一`semantic_kind=user_input` root。模型工具和内部source禁止调用此路径；模型侧`send_message_to_session`不暴露`simulate_user` |
| C02 | 模型可见工具定义与内层扩展目录：当前直接built-in、Todo/filesystem/compact、自定义、MCP、Session内team tools及visibility/policy过滤 | Agent构建和request middleware确定实际tools/tool config；Itemized middleware快照为`ToolSetSnapshot/ToolSetRef`；当前执行step只在Turn/Job开始时读取一次selection，运行中Job保留旧Agent引用 | descriptions与静态prompt散落；Provider可见工具和内部target目录混合，变化会扰动工具schema与前缀；运行中selection无model-call安全边界 | 拆成Provider可见直接工具+固定`invoke_extension_tool`信封的ToolSetRef，以及独立ExtensionToolCatalog binding。只有前者有效变化经`SwitchToolSetIntent`在安全边界hard rebase；内层MCP/自定义/内置target变化只在资源激活边界更新binding与对应CSM指引，执行期重新鉴权且旧tool call真实配对收敛。main/child capability profile仍决定直接工具集合，Skill`allowed_tools`不改变此权威 |
| M01 | MCP工具目录：当前`McpRuntimeManager`启动时加载MCP工具适配器；变化通知尚无完整刷新闭环 | 运行中server目录变化不稳定地反映到Agent/信封绑定，目录与模型指引没有统一生效快照 | 将raw目录直接塞进Provider工具说明会破坏缓存；只刷新dispatcher又使指引和真实目标脱节 | `McpCatalogOwner`完整relist/验证/发布稳定目录；`McpToolGuidanceProducer`只从validated目录派生`tail_only` CSM source，增删改以user-role delta提示；同一`turn|model_call`激活边界封存目录与指引。原始prompts/resources/instructions不自动注册；target执行按旧binding和最新权限。 |
| C03 | 模型与工具协议事实：assistant/reasoning、Provider tool call/result，以及 invalid args、timeout、confirmation、patch repair、compact tool生成的 synthetic `ToolMessage` | 模型流或 middleware在执行内追加 `AIMessage`/`ToolMessage`，随后进入 canonical item/checkpoint | 它们是会话事实或协议配对，不是 instruction source；误纳入 CSM 会破坏 tool_call_id 和 Provider role合同 | 不进入 CSM；由 canonical item、tool execution与terminal convergence owner构造 `AppendCanonicalItemIntent`并经统一 ContextStore owner提交，保持原生 role/配对、item identity和 plan ordinal；只能参与稳定前缀选择，不能按文本去重 |
| P01 | 请求捕获与投影管线：`PromptReplayCaptureMiddleware`、`ItemizedContextProjectionMiddleware._prompt_contributions()`、`project_context_plan()` | 逐 middleware 比较 system blocks，推断 append/replace并全部记为 request-only contribution；LangChain projector把连续贡献合成 `request-only-system-prompt` | 这是从最终请求反推 provenance，不是 producer 权威；replace/合并会丢 item边界并破坏稳定前缀；无捕获时 synthetic fallback掩盖漏接来源 | 删除 PromptReplay、捕获标签和 instrument 链；废弃 `ItemizedContextProjectionMiddleware` 的 prompt/tool/context反向捕获。每个 R/E producer在 seal 前显式登记；若框架必须保留 middleware hook，只实现无状态 sealed-assembly dispatch bridge，按 Saver-issued reference转发精确 messages/tools/frames，缺失或不匹配即 fail closed |

迁移闭包之外，`app/tool_testing` 的 `tool_test_retry` 只是模型测试 harness；当前未接线的 knowledge/safety 配置也不是生产上下文来源，不得为了“完整”预先实现。R09 memory 只有在显式传入 sources 时才算活跃，测试存在不等于默认生产已启用。

### 10. 删除反向捕获，middleware 最多只是无状态 dispatch bridge

`PromptReplayCaptureMiddleware`、`_PROMPT_REPLAY_LABELS`、`_instrument_prompt_replay()` 及其 request block diff/replace 推断链全部删除，不保留“仅用于诊断”的运行模式。诊断只读取 sealed assembly manifest、source refs、ToolSetRef、frame hash和最终 dispatch outcome；它不能再次观察 framework request并生成另一份 provenance。

现有 `ItemizedContextProjectionMiddleware` 的以下职责全部废弃：

- 从 `request.system_message` 或 `request.messages` 反向生成 `ContextContribution`；
- 从 request-time tools反向创建权威 `ToolSetSnapshot`；
- 在 middleware 内调用 prepare/reconcile/seal，或维护 source/session/request状态；
- 在缺少 capture 时生成 assembled-system-prompt fallback；
- 合并相邻 system/user blocks、重新排序或重新格式化 sealed item。

Context compiler、CSM、ToolSet registry和 Saver owner必须在进入 framework model-call hook前完成 canonical append、tool protocol convergence、source reconciliation、ToolSet hard rebase、plan创建和 seal，并向执行层交付不可变 `SealedAssemblyDispatchRef`。该 reference至少绑定 Session、execution/model-call attempt、assembly、prefix epoch/reason、Provider profile、messages/frame hash和精确 ToolSetRef/policy hash，不能只传可被复用到其它请求的 assembly id。

若 LangChain 等框架必须通过 middleware 才能替换最终 model request，则只保留一个 `SealedAssemblyDispatchBridge` 式无状态适配器：

1. 从本次 execution context取得 `SealedAssemblyDispatchRef`；
2. 通过只读 Saver dispatch port解析已经 sealed 的 messages/tools/Provider frames；
3. 校验 Session、model-call attempt、Provider profile、prefix/frame hash和 ToolSet binding；
4. 把精确 sealed payload交给下一层，既不读取旧 request内容推断来源，也不创建或修改任何持久状态；
5. 在 Provider 调用前再次验证最终 bytes/hash，发现 bridge 之后仍有 middleware改写请求时 fail closed。

bridge必须是最后一个可影响 model request的 framework hook；其后只允许不修改 payload的 telemetry。相同 sealed assembly retry重复解析同一 reference并得到相同 bytes，不使用进程缓存、middleware state或“上一次请求”作为基准。若框架允许 execution/provider adapter直接消费 sealed payload，则删除 `ItemizedContextProjectionMiddleware`，不为了保留类名而创建空兼容层。

考虑过保留 PromptReplay做运行时对账，但它仍会形成第二 provenance来源，且无法证明 middleware diff与 canonical item边界一致，因此拒绝。对账改为“producer registration + sealed manifest + dispatch byte hash”的正向验证。

### 11. 所有 producer 使用统一 mutation 边界和各自 domain owner

- **初始 root producer**：R01–R07 以及显式启用的 R09 在首次组装时提供独立 provenance，由 compiler 只在该合法边界编译成唯一 root system item。
- **文件 producer**：R08 与 Skill activation 使用 CSM tracked/snapshot 规则；activation 只能由 `skill_load` 建立，通用 read 工具不承担 Skill 激活语义。
- **事件 producer**：E01–E07 先提交 ambient/pending source item，再通过独立 execution wakeup intent消费；不得调用普通 user acceptance 创建 Turn root。
- **canonical append producer**：C01/C03 保持 acceptance/Turn、model stream和 tool execution的 domain owner，通过 `AppendCanonicalItemIntent`进入同一 ContextStore transaction，不建立 CSM tracking。
- **ToolSet producer**：C02 由 ToolSet registry产生 `SwitchToolSetIntent`；所有有效变化在下一个 model-call safe boundary hard rebase，不追加软上下文通知。
- **派生 owner**：D01 保持 compaction owner并通过 `RebuildContextEpochIntent`显式重建 active view；“统一 mutation owner”不等于把所有事实改造成 CSM source。
- **投影管线**：P01 的反向捕获全部删除；可选 bridge只验证和转发已登记、已 sealed 的事实，不根据 LangChain 最终消息形状反推 lifecycle。

display/history projector 可以显示安全摘要和 provenance，但不得把这些 item 投影成普通用户对话，也不得把展示内容反写成 source 正文。

### 12. 失败默认阻断，不读取当前文件修补历史

required source 的 detail、hash、revision、owner、stable prefix 或 catalog identity 校验失败时，assembly seal/dispatch 必须失败。optional source 只能通过 included=false、availability、omission/loss 明确记录。

恢复旧 assembly 时只能读取已提交 item/detail/manifest，不得重新读取当前 AGENTS、Skill 或团队状态拼出历史请求。Provider adapter 若合并相邻 message、移动 system item、改变已提交 frame序列化，或在新 ToolSet下无法表达 active history中的旧 tool call/result，也必须明确拒绝；后者只能通过显式 compaction或终止 execution处理，不能静默改写协议事实。

### 13. SessionThread 是 CSM、stable prefix 与 ToolSet applied state 的唯一 owner key

CSM 的所有 source registration、tracked revision、latest-visible-committed 基准、untrack 状态与 source detail 都必须绑定 `(session_id, thread_id, source_identity)`。同一个 workspace file/Skill/team source 可以被多个 thread 各自追踪；其文件 revision 可以相同，但每条 thread 的 active view、已提交 base/delta、rewind 结果和下一次注入决定必须独立，不能因 source hash 相同而跨 thread 复用 CSM control state 或 context item。

`RolloutCheckpointRuntime` 仍是 workspace singleton 的组件装配容器，不成为任何 Session 或 thread 的第二 writer。它通过显式 `ThreadRuntimeBinding(session_id, thread_id, graph_binding, execution_id, model_call_id)` 取得唯一 Saver/ContextStore port。`SealedAssemblyDispatchRef`、CSM observation、ToolSet switch 和 epoch rebuild 都必须携带同一 binding；缺失、owner 不一致、或尝试以 session root/`checkpoint_ns` 替代 thread 时 fail closed。

产品层未指定 thread 的普通 Session 聊天/历史/Goal API 只从权威 catalog 解析 `main_thread_id`，并把实际 ID 返回给调用方；跨Session send/read/wait的目标同样只解析目标main thread。当前Session右侧侧边栏对子thread的直接history/message、内部producer、subagent、retry、rewind、compaction和dispatch必须显式给定thread。delegated child thread 的结果如需进入 main thread，使用带 source-thread provenance 的 ambient/runtime item 由main-thread owner追加，不能共享source state、复制child item为user root，或将两条thread的stable prefix合并。

GraphBinding 的持久化与 CSM owner 对齐：graph factory/blueprint 可以跨 thread 缓存，已编译 graph、工具和 middleware 不得捕获 SessionThread；每次 invocation 注入 binding。无法解析精确 graph revision 的恢复必须停止在 dispatch 前，不能通过创建新 context epoch 或改写旧 prefix 修复。

Session物理locator由workspace `.boxteam/navigation/session-catalog.sqlite`按不可变UTC创建日期精确解析为`sessions/YYYY/MM/DD/{session_id}`；导航Session/Folder父子关系只在该SQLite中，不再由物理`children/`或`session.json`保存。thread物理locator仍属于owner binding：main位于已解析Session节点的`threads/{main_thread_id}`，其它durable thread位于`threads/YYYY/MM/DD/{thread_id}`；两者都由Session thread catalog/resolver受检取得。workspace catalog拥有唯一不可变main pointer，session-control只有与之匹配的唯一main thread row、thread catalog与Session内部collaboration ledger，不建立第二main pointer/ContextStore writer。CSM、Saver和dispatch bridge不得按日期、父节点或`checkpoint_ns`自行拼路径；导航移动不改变源registrations、stable prefix或ThreadRuntimeBinding。

会话目录乐观更新只属于Web展示层，完整命令/状态/事件协议由`add-itemized-rollout-context`的workspace导航catalog拥有。仅`NavigationMutationRecord(state=queued|running)`或浏览器pending overlay，不改变已提交Session/Folder父链、Session物理locator、ThreadRuntimeBinding、GraphBinding、CSM source registration、ToolSet binding或stable prefix；导航pending事件不得进入canonical item、ambient notice、ResourceRegistry或模型请求。入队短事务不建立Session lifecycle lease，也不占用`NavigationTopologyGate`做实际目录预检；worker在执行时才按topology/Session gate和catalog active状态处理。排队中的删除在整树catalog deleting提交前不关闭新业务准入：模型/通信/attachment依然依据已提交catalog/fence运行；提交后即使Web仍有旧pending投影也必须拒绝新副作用。已提交的纯导航move/rename只改变breadcrumb/父节点投影，不能触发CSM reconcile、prefix epoch变化、owner rehydrate或被当作source diff。客户端收到乱序导航事件只可更新展示层，不可给后端执行传入“乐观父链”替代fresh catalog检查。

所有会在既有Session产生新持久副作用的入口先取workspace跨进程`NavigationTopologyGate` shared短锁，再取目标`SessionLifecycleGate`，fresh读取SQLite catalog active/locator与本地fence generation，并durably建立lease/等价record后释放；同一execution的后续callback由有效lease覆盖。导航move/create/delete取topology exclusive，纯parent/name更新只改SQLite，不改Session目录、context或fork/delegation lineage。最终可见性publication重取topology shared→Session gate，fresh验证catalog active和旧lease/fence token；catalog已deleting时取消新可见性发布，只能收敛已准入operation。锁顺序固定topology→至多一个Session gate→至多一个SQLite写事务，不能反向获取或把事件/无锁active查询当原子准入。

通用lease至少持久化`lease_id`、`operation_kind=thread_creation|board_migration|collaboration_fanout|runtime_owner|execution|context_control|communication_source|communication_target|federated_call|remote_observation|attachment|fork_retention|session_catalog_mutation`、稳定operation identity/preimage hash、captured lifecycle generation、holder generation/fencing token、`active|settling|completed|cancelled|failed`状态、revision和可选recovery ref，并对同generation/operation identity唯一、非终态可索引；专用record以显式lease state或版本化全映射归一自己的preparing/routing/published/aborted等状态，不能按名称猜测。lease没有墙钟自动到期；恢复owner验证旧holder失效并CAS新token后才可继续或settle。fence deleting后不建新lease，旧lease只完成/取消原operation而不派生新root/wakeup/child；跨库主体先durable commit再terminal lease，中间崩溃按稳定identity/ref核对。删除请求settling后仍等待原writer确认或幂等恢复核对，terminal后旧token callback失败。同一Session catalog目标条目的locator/lifecycle/归档mutation也必须以`session_catalog_mutation`竞争gate，旁路写入fail closed。

per-Session gate identity仍固定为`(workspace_id, canonical_session_id)`，其OS shared/exclusive锁存于不随日期节点变化的`.boxteam/navigation/session-lifecycle-gates/{session_id}.lock`；另有workspace navigation根的跨进程`NavigationTopologyGate`。cold业务读在topology shared下取得fresh locator和Session shared guard，释放topology后保留Session guard到全部handle关闭并复核catalog/fence；已开始读可以完成，整树catalog deleting后新读返回`session_deletion_pending`。删除逐Session关闭local fence前须等待原guard，不能持锁等待网络/worker或允许lock文件inode重建。

Context lifecycle不得承担Session/thread创建的补洞逻辑。Session+main按workspace creation journal发布；普通单child则必须在生命周期gate内、创建staging前先把冻结ID、最终/内部staging locator、preimage、GraphBinding/capability/seed/admission、artifact manifest/hash及Session lifecycle generation/catalog/collaboration precondition revision的`ThreadCreationRecord(state=preparing)`提交到Session `session-control.sqlite`并让其承担operation lease，再按记录staging/flush/rename，最后重取topology shared/Session gate，由thread catalog、需要时的collaboration ledger与该record的单一SQLite事务CAS验证workspace catalog active及fence仍为捕获的active generation且delegation/parent/member状态未漂移后发布并标记`published`。CAS失败只定点清理并标记`aborted`，不能重基当前状态。只有catalog可见、`state=active`且ContextStore/GraphBinding/capability均完整的thread才能取得owner binding。rename后、发布前恢复只可枚举预存record定点校验，不能扫盘或吸收无record目录。delegated child的task seed和持久admission intent必须在发布前准备，发布后由creation/delegation identity幂等且重新通过生命周期准入的worker启动；CSM、history、before-model或用户打开右侧侧边栏都不得把半成品/空child修补为可运行owner。board migration批量child是显式例外：每个target只由在gate内先行建立的`BoardMigrationRecord`内`MigrationChildCreationEntry`承载等价lease/generation/creation manifest，最终事务验证相同active generation；不得再建立可被普通ThreadCreationWorker认领的独立`ThreadCreationRecord`。

Session及其全部逻辑后代、recursive Folder删除统一由workspace `navigation/session-catalog.sqlite`中的`NavigationSubtreeDeleteRecord`负责：topology exclusive下按递归CTE冻结精确node/Session ID、日期locator与revision并逐Session短时预检retention blocker，然后同一SQLite事务create-or-get batch且一次将整棵子树标为deleting。该catalog commit关闭新准入与逻辑可见性，先关本地fence再改JSON索引和独立delete journal的旧方案废弃。若任一retention claim先行则整个batch不提交；batch先行则后续claim/新context mutation全被catalog拒绝。

删除owner按已提交batch固定清单逐Session取得一个本地gate，CAS local fence到deleting并收敛旧runtime generation、lease、child/board、copy attachment、communication与pin；旧writer只可按冻结preimage和batch许可完成/取消。每个Session完成后其日期目录定点隔离到`.boxteam/sessions/.deleting/<operation_id>/<session_id>`并记录进度；全部完成后workspace SQLite一次提交全树不可复用tombstone。中途崩溃所有目标仍逻辑deleting，CSM不得重开、扫目录恢复或因部分目录已隔离把其它节点重新显示active；GC保留尚在retention/communication窗口内的已隔离数据。

新execution先通过Session生命周期准入并以queue/admission record持有捕获generation的operation lease，再进入itemized change定义的thread-local durable FIFO queue；真实用户acceptance可立即持久化queued Turn/root，但当前active execution的`ExecutionContextFence`必须按`causal_admission_ordinal`排除所有更晚entry的root、seed/notice和execution，即使它们的物理item sequence早于当前execution稍后产生的tool/final item。fence不屏蔽与未来entry无关的ambient文件/Skill/team revision或ToolSet desired state；queue entry取得active slot时冻结默认`TurnResourceSnapshot`，后续tool-loop默认复用它，只有配置为`model_call`的resource kind才在安全preparation重新冻结Registry内存revision，并按stable-prefix合同append/rebase。pending entry不提前生成activation snapshot、检查tracked source或创建assembly。tool/provider callback绑定原execution，不作为新entry，且只能由有效lease/generation提交；Session fence关闭后的旧callback仅可按删除收敛协议写terminal control outcome，跨thread没有共享active slot。

附件正文是workspace级外部blob，不属于CSM source detail或任一thread rollout正文。固定物理根为`.boxteam/attachments/YYYY/MM/DD/{blob-id}`，`blob-id`使用精确正文SHA-256形成的68-byte ASCII `blb_[0-9a-f]{64}`，正文直接位于日期目录且不得增加digest shard；由`.boxteam/attachments/catalog.sqlite`解析逻辑attachment/variant、digest、locator、session/thread/item owner refs、protection、retention和tombstone。写入遵守itemized change的`Session AttachmentOperationPin → AttachmentIngestRecord → BlobCommitClaim → rename → active/pin复核 → catalog availability/owner-ref publish`跨库saga；pin准入、最终owner-ref提交和Session进入`deleting`必须竞争同一短时`SessionLifecycleGate`。任何workspace ingest/staging前先有可恢复Session-local pin，canonical append不得引用未发布blob；删除先取得gate时拒绝新pin/未提交owner ref，owner-ref先提交时删除必须按仍存在的pin阻断canonical使用、释放reference并收敛后再隔离Session。该gate不跨正文写入或模型执行持有，也不替代持久record。CSM/ContextStore canonical append只保存稳定attachment reference与provenance；assembly seal、Provider projection和工具读取必须通过capability检查owner/view membership、hash和length。相同blob可跨Session物理去重，但权限、引用释放和历史可见性不能共享；GC不得因一个Session删除而回收仍被其它owner引用的blob。

### 14. Session内部状态、跨Session操作和runtime residency必须分开

Session main thread承担长期用户任务和Goal；durable child thread承担同一Session内的委派、specialist或service工作。二者复用相同ContextStore/CSM实现，但通过capability profile决定可用source和ToolSet：当前main可拥有Goal，child固定`goal_enabled=false`。用户直接向child发送的消息仍由C01创建child-local真实user Turn；禁用Goal不表示child不可对话、不可持久化或只能作为隐藏subgraph。

team board、member、task和coordinator是Session级协作ledger，不属于任一thread ContextStore，也不能跨Session复制。ledger以稳定member identity指向当前Session的child thread；每次状态变化由E04按目标thread生成各自source observation/revision，不能让多个thread共享CSM control state。另一个Session不能attach为team member；跨Session只通过E03 send/read/wait交流，通信ledger只保存幂等、correlation、resolved target和access audit。

ledger mutation先通过Session生命周期准入，并在同一`session-control.sqlite`事务内以`CollaborationFanoutRecord`冻结捕获的fence generation、`ledger_revision`、事件hash、recipient `(session_id, thread_id)`集合和每个recipient的delivery state；该record在全部recipient terminal前承担operation lease，不得在提交后按“当前成员”重新计算收件人。独立fanout worker按`(ledger_revision, recipient_thread_id)`幂等地向各thread ContextStore提交pending source observation，并在每次提交时验证原fanout lease/generation；不跨thread事务，也不唤醒cold runtime。recipient在配置的resource activation boundary冻结`source_reconciliation_snapshot`并从fanout状态索引拉取/reconcile全部required revision；默认turn snapshot的上界不得早于取得active slot时的ledger revision，不能使用queue acceptance时刻，同一Turn后续tool-loop保持该上界；只有team resource显式配置为`model_call`时才重新捕获Registry内存snapshot。连续未seal revision可按CSM规则合并，已提交revision不可重写。进程崩溃、child cold/删除或单recipient失败不得让其它recipient重复注入；fence关闭后未提交recipient以明确terminal delivery outcome收敛，不能产生晚到source append，snapshot内required team state未追平时该thread不得dispatch模型请求。

跨Session目标接受裸`session_id`、`boxteam://session/{session_id}`、`boxteam://workspace/{workspace_id}/session/{session_id}`或`boxteam://gateway/{gateway_id}/workspace/{workspace_id}/session/{session_id}`；这些引用只是locator而不是bearer capability。联邦拓扑固定为一个中心Gateway hub和其直接spoke：hub保留现有SSH `-L`作为可达、加密和主机认证层，并通过本机loopback转发地址连接spoke的`ws://.../api/gateway/federation/channel`，建立长期全双工WebSocket对等RPC channel。SSH/WebSocket均由hub主动建立，但channel内hub与spoke都能发起带request ID的request/response/event；`B → A → C`最多经过一个hub transit，spoke不得继续转发到第四个Gateway。workspace-qualified link使用当前Gateway catalog identity，federated link以持久`gateway_id + workspace_id`定位拓扑中的Gateway；来源thread按真实invocation保存，即使来自child或经hub中继也不能伪造成source main/hub，目标始终由target workspace解析main thread。

现有`connection_id`保持Gateway本地、持久的连接配置身份，不改作socket实例ID。每次channel实例另有瞬时`channel_instance_id`、`connection_epoch`、`connected_at/last_seen`和epoch内`seq/ack`；单次route lease保存稳定origin/target gateway、可选唯一transit gateway、所用channel epoch、remote workspace route、route revision和expiry。Session link、`ResolvedSessionMainTarget`、`GlobalThreadAddress`、outbox/inbox preimage及业务幂等键不得包含`connection_id`、channel实例、epoch、seq/ack或remote route locator。WebSocket断开、SSH重连或hub进程重启只刷新这些瞬时状态，不能改变业务identity。

source outbox首次提交后冻结稳定target GlobalThreadAddress。重试、source恢复、WebSocket/SSH重连或hub重启只可为该地址刷新临时route lease，不得重新执行裸ID discovery后改投同名Session，也不得因connection/channel/route revision变化改变communication preimage；稳定target不可达时显式保留原target并返回可重试路由失败。read/wait snapshot、selector和response envelope也只绑定稳定地址，临时route不持久化为业务事实。hub只在内存维护当前in-flight relay correlation；丢失该映射时由source outbox以原operation/communication重试，由target inbox/observation dedupe返回原结果，不得要求hub持久化消息正文或替代双端业务账本。

裸ID使用有界exact-ID discovery：source先查询自己的本地registered workspace；若source是spoke，再通过唯一hub查询hub本地workspace和其它active spoke，若source是hub则直接查询自己的active spoke。这里`max_transit_gateways=1`、`max_gateway_hops=2`，request携带完整`visited_gateway_ids`和总deadline；hub只能fan-out到目标spoke的本地workspace，不得要求spoke继续递归。lookup只读cold Session catalog，不加载runtime。只有按每个实际检查点的最新policy获得允许的候选参与解析；未授权存在与不存在统一为`target_not_resolvable`，多个已授权候选只返回不含locator的`target_ambiguous`/count并要求qualified URI。带签名、catalog revision和短TTL的route hint只可优化路由，目标workspace每次仍重新验证Session与main pointer，Gateway不得把hint变成业务Session catalog或授权事实。

裸ID discovery尚不知道`workspace_id`，因此单独使用`FederatedDiscoveryGrant`：source到hub的origin envelope绑定真实source gateway/thread、不可逆principal ref、预期后续operation、精确canonical session ID、request/nonce、visited/path/deadline和短期有效期；hub先按channel binding确认origin不能伪造，再为每个目标spoke签发只允许该spoke本地cold catalog exact lookup的`HubTransitDiscoveryGrant`。target spoke只信任已登记hub的签发身份，并再次按自己的最新policy过滤；grant不得执行operation、读取history、解析child或继续递归。受认证response只返回零、单个已授权qualified route hint或不含locator的ambiguity count。source/hub聚合出唯一target后才进入完整operation授权；两类grant、response和hint都不暴露给模型。

远程operation使用channel-bound origin envelope和内部`HubTransitOperationGrant`传递授权上下文。spoke B在hub-dialed全双工channel上发起请求时，hub A从channel registration取得真实`origin_gateway_id=B`，拒绝payload冒充其它origin；A按自己的最新transit policy允许后，向target C签发绑定`issuer=A`、`origin=B`、`transit_path=[B,A,C]`、audience C、不可逆principal ref、单一`send|read|wait`operation、规范化target、可选source `GlobalThreadAddress`、稳定operation invocation、request、nonce和短期有效期的grant。C不需要与B直接配对，而是验证已登记hub A及其grant完整性/audience/path/期限/replay，然后按C的最新local policy授权“来自B、经A”的principal；C看到的业务source仍是B。response由C在A-C channel上认证，A验证后以绑定原origin request、target response hash和route path的relay envelope返回B。grant/response不进入模型、link、canonical item、communication正文或普通日志；send acceptance receipt还绑定communication、payload hash和acceptance ref。每次网络attempt换request/nonce/grant但复用逻辑operation，send重试保持communication ID；既有inbox dedupe为当前grant重新认证原acceptance，不重复注入。

hub和target必须在任何lookup/forward之前，分别把自己接收的origin/transit discovery/operation envelope以`(issuer, origin, audience, grant_kind, nonce)`在Gateway control-plane first-use replay registry原子登记并绑定grant/request/path hash；同nonce不同preimage或重复first-use拒绝。网络结果未知时source使用新request/nonce/grant，send仍复用原communication。record保留至expiry、clock skew和transport replay margin全部越过，Gateway重启不得丢失有效窗口；registry或credential/key验证不可用时fail closed且不触碰workspace业务状态。hub除peer/connection registry、policy snapshot、replay registry、channel/route运行态与脱敏audit外不保存communication业务状态。

Gateway配置域新增`permissions.federation.default_effect="allow"`、`permissions.federation.rules=[]`和`permissions.federation.hardening_enabled=false`的内置默认值：对已认证、已登记在同一hub拓扑中的主体，默认开放核心discovery/send/read/wait/reply/transit，不要求用户先写allowlist。限制规则和额外hardening为显式opt-in，并通过版本化`GatewayFederationPolicySnapshot(policy_revision, normalized_rules, content_hash)`校验后原子热发布；无效candidate明确报错且不产生半生效状态，已有WebSocket channel无需重启。policy revision不进入模型、canonical/context item、ToolSet或sealed assembly；工具保持可见，调用被最新策略拒绝时返回明确authorization错误，从而不破坏已提交wire prefix。

身份认证、channel identity binding、grant/response完整性、audience/transit path、first-use防重放、Session target解析和业务幂等属于协议正确性，不受`hardening_enabled`或default allow关闭。每次discovery fan-out、hub transit、target operation admission都读取当时最新policy；远端wait的grant在target准入时至少覆盖`effective_timeout + bounded_clock_skew`。read每页、wait每次状态/terminal披露、send网络重试和hub返回relay response前再次检查相关Gateway的最新revision；撤权立即阻止尚未durable acceptance的send和后续read/wait披露，并返回`authorization_revoked`。send一经target durable acceptance不因随后撤权或旧grant过期回滚，但之后的read/wait仍需新授权；重新放开权限后下一次实际调用立即按新revision允许，不追溯改写旧结果。

`send_message_to_session(target, content, kind="result", reply_to_communication_id?, delivery_policy="after_turn")`在目标main注册ambient/pending source并可触发wakeup，但不创建真实user root；`target`接受上述ID/URI，`content`必须非空，`kind=question|reply|progress|result`，`delivery_policy=after_turn|after_tool_result|after_interrupt`。输入不暴露`communication_id`、`send_operation_id`或`simulate_user`；source outbox按软件持久化的稳定operation identity唯一分配communication，可信UI/API重试同样复用软件idempotency key。真实用户语义只允许可信UI/API ingress。成功结果至少返回resolved target、`communication_id`、durable delivery state、target acceptance ref及已知的`job_id`/`turn_id` binding。`wait_for_session(target, communication_id?: string, job_id?: string, turn_id?: string, until: "terminal" | "state_change" = "terminal", timeout_seconds: integer = 60)`替换`monitor_session_agent_end`：selector至多传一个，timeout范围1–300秒；当communication已accepted但execution尚未绑定时先等待binding再等待对应execution，不能把短暂无active Job误报为idle。无selector时冻结准入快照中已经active/runnable/pending的identity集合，不订阅未来任意Job。结果状态闭集为`idle|pending|running|completed|failed|cancelled|timed_out`；timeout返回观察identity/current state与可复用selector，未知selector报`selector_not_found`而非idle。

`WaitForSessionResult`返回resolved target、status、`observed[{selector_kind, selector_id, state, revision}]`、baseline和最新target revision。显式selector只含该对象及communication→job/turn binding；无selector空集合返回idle。非空集合的统一聚合优先级固定为`failed > cancelled > running > pending > completed`；`until=terminal`等待全部冻结对象终态后返回failed/cancelled/completed，`until=state_change`在任一对象revision变化后按同一优先级返回当时状态和完整observed。timeout只把顶层状态设为timed_out，observed保留真实状态，调用方不得猜测未返回对象。

wait由可注入单调`Clock/DeadlineTimer`和目标状态订阅驱动，生产不轮询数据库；测试可以推进虚拟时间验证默认60秒和最大300秒而不缩短产品合同。fake timer只控制等待预算，不修改communication、Job、Turn、residency或canonical时间戳。deadline与terminal/state-change并发时，以target owner的已提交revision裁决：条件已满足则返回该状态，否则返回`timed_out`、revision和可复用selector。

可恢复wait的`RemoteObservationRecord`必须保存版本化`DurableDeadline{timeout_seconds, admitted_at_utc, deadline_at_utc, monotonic_origin_id, monotonic_deadline}`，不能持久化无origin的monotonic数值。相同host boot/clock origin内的进程重启继续原monotonic deadline；origin变化时只能用可信UTC计算不超过原deadline/timeout的剩余预算，无法证明仍有正剩余、检测到回拨/超界或clock不可用时返回`timed_out`/`deadline-clock-unavailable`，不得重置为完整timeout。测试fake clock提供稳定origin+UTC映射，并与residency clock隔离。

跨进程/服务器send不能假设分布式事务，也不能依赖内存future。source thread node持久化`CommunicationOutboxRecord`，target main-thread node持久化`CommunicationInboxRecord`；Gateway只做逐跳授权、路由和Gateway级访问审计，不成为communication业务状态owner。两端记录都绑定source/target `GlobalThreadAddress=(gateway_id, workspace_id, session_id, thread_id)`、`communication_id`、immutable resolved target、payload hash、delivery policy和本端状态；outbox另绑定模型不可见的`send_operation_id`并保存最新受认证target receipt，inbox保存target acceptance ref、ambient item/wakeup幂等键、`admission_id`、job/turn binding和terminal outcome。source outbox建立与target inbox acceptance分别取得各自Session gate并在各自`session-control.sqlite`建立覆盖该communication side的轻量`SessionOperationLease`；两个gate绝不同时持有，网络和双端提交只组成可恢复saga。outbox只可`accepted → routing → target_accepted → execution_bound → terminal`，inbox只可`target_accepted → execution_bound → terminal`，任一侧可进入带原因的`failed|cancelled`；source只能按匹配地址、communication、payload hash和acceptance ref的受认证receipt推进远端状态。

每次逻辑send先由软件提供稳定、模型不可见的`send_operation_id`：模型工具调用绑定source execution/tool invocation，受信UI/API调用绑定其持久idempotency key。source在首次route前先通过lifecycle gate确认active generation并建立communication lease，再以`(source_global_thread_address, send_operation_id)`在该lease覆盖的本地事务中create-or-get outbox、communication ID和完整request preimage；同operation不同preimage冲突，lease/outbox中间崩溃按稳定identity定点继续或终结，outbox未提交不得发网络请求。source outbox和target inbox再分别以`(source_global_thread_address, communication_id)`dedupe；同key不同payload/target报冲突。这样target接受后source在receipt前崩溃时，恢复同一invocation仍使用原communication。

target先在自己的lifecycle gate内验证catalog/main pointer和active fence generation并建立target communication lease，再在该lease覆盖的同一ContextStore owner事务中提交inbox acceptance、唯一ambient item、wakeup幂等键和稳定`admission_id`；删除先关闭fence则不得建立lease/inbox，acceptance先行则删除必须发现并收敛该lease。JobService按`admission_id + preimage hash`create-or-get，并由仍有效的target communication lease覆盖或重新通过生命周期准入；Job创建后、binding提交前的崩溃只能补写原job/turn binding，不能在deleting generation晚到绑定。send只在收到target durable acceptance receipt后返回成功；网络结果未知时返回可重试unknown outcome并要求复用原ID或原send operation。target在acceptance与execution binding之间重启后从持久inbox/lease继续，source wait通过target查询/受认证receipt沿同一communication恢复。禁止跨workspace共享SQLite、两阶段提交或Gateway业务账本。

pending/running communication以及reply/wait/audit需要的因果字段完整保留；terminal正文可按明确retention回收，但拥有者Session删除前保留最小dedupe tombstone，至少覆盖双端地址、send operation/communication/acceptance/admission identity、payload/preimage hash、correlation、终态和receipt验证字段。Session删除先把本地未终态记录收敛为带`source_deleted|target_deleted`原因的failed/cancelled并提交catalog deletion tombstone；canonical ID不复用，迟到route不得投递到其它Session。

target workspace的单一`InboxAdmissionWorker`在acceptance提交事件和backend startup时，从持久状态索引恢复`target_accepted`但未`execution_bound`的inbox；它按精确main-thread address、wakeup key和`admission_id`取得/rehydrate owner并幂等create-or-get admission，成功后提交原job/turn binding，永久失败则提交failed及认证receipt。worker不扫目录、不依赖内存future、不成为第二ContextStore writer，并用claim/lease或等价约束阻止并发重复admission。wait/read只观察已提交状态，不负责启动worker或唤醒目标。

`kind`闭集为`question|reply|progress|result`。`kind=reply`必须带`reply_to_communication_id`，且target inbox能证明被回复communication的source/target与本次方向相反；其它kind必须拒绝该字段。reply只建立消息因果关系，不形成跨Session team/task状态。

`read_context`只读取授权的有界history/summary projection并返回revision/cursor与source refs；它作为模型工具执行时只在调用方追加一个普通canonical `tool_result`，绝不把目标Session的canonical item、Goal、CSM registration/control state或stable prefix复制进调用方。read/wait receipt可以成为调用方工具结果和审计记录，但不写目标context、不唤醒目标execution，也不materialize目标runtime。

read首请求必须由target分配稳定`observation_id/read_series_id`，并签发版本化AEAD保护、opaque且可自验证的不可变`ReadContextSnapshot`/cursor envelope；密文内绑定该identity、resolved thread、active view revision、item/Turn上界、projection/visibility policy hash、总预算、offset与到期时间。模型或客户端不能读取/篡改内部字段，解密或认证失败统一返回不含细节的`read-snapshot-invalid`。token不写ContextStore/canonical history，target只可写独立访问/operation control记录。后续页是新的source tool/API调用与新的`operation_invocation_id`，但必须由cursor恢复同一observation/read series并始终读取该snapshot，即使目标新增消息、rewind或切换view也不得换到最新；每页重新授权，cursor不是capability，target/principal/policy/projection/limit变更返回`read-snapshot-mismatch`。retention、认证key rotation或view/detail不可恢复返回`read-snapshot-expired`/明确loss，不静默新建snapshot；旧cursor重放只能重读固定范围，周报Session只能组合该一致切片。

每个read/wait source tool/API调用使用其tool invocation或受信API idempotency key生成稳定`operation_invocation_id`；它是本次`source_call_id`，不能兼作跨多个分页调用的snapshot identity。source由现有execution lease覆盖或先在自己的Session gate建立`federated_call` lease，再在网络前create-or-get`FederatedCallRecord`并冻结operation、稳定target和参数hash；target先在自己的Session gate验证active fence并建立`remote_observation` lease，才可在ContextStore/canonical之外的Session-local有界operation store建立记录。read首调用以`(source GlobalThreadAddress, first_source_call_id)`唯一create-or-get并分配`observation_id/read_series_id`，同call重试找回同一identity/snapshot；`RemoteObservationRecord`以observation identity保存冻结上界。后续每页的新source call通过opaque cursor恢复该record，再以`(observation_id, page_ordinal)`唯一create-or-get绑定本次operation invocation与cursor hash的`RemoteObservationPageRecord`，并约束一个source call只映射一个page、同ordinal不同preimage冲突。wait observation以`(source GlobalThreadAddress, source_call_id)`唯一建立并冻结selector集合、baseline、deadline和subscription identity；grant/response同时绑定source call及适用的observation/page identity。两端gate不同时持有；目标删除先行时不创建snapshot/baseline/subscription，observation先行时删除把仍依赖node的调用收敛为明确target-deleted或原冻结结果并等待lease terminal。网络retry使用新grant但同一次source call复用其identity；source在target冻结后、保存response/tool result前退出时必须恢复同一page或wait terminal envelope，每个source call最多提交一个tool result。timeout后继续等待使用带原selector的新operation identity与新observation；record过期返回`operation-retry-expired`而不静默重开。该记录不是team/task/Goal状态，不注入目标context或唤醒runtime。

Session逻辑删除不得破坏已接受observation的重试合同。catalog发布不可复用ID tombstone前必须terminalize全部非终态observation lease；隔离节点及其中只读operation replay记录继续保留到全部未过期observation/communication恢复窗口结束，只允许匹配source GlobalThreadAddress、source call、observation/page preimage且通过新授权的定点恢复路径访问，普通Session/history/runtime resolver不得打开。窗口结束后才可物理清理；之后原调用返回`operation-retry-expired`，不得借tombstone重建snapshot、baseline或当前Session视图。

旧跨Sessionteam/member/task/coordinator记录必须通过显式、可审计且整块可见的迁移选择处理：要么从已终态或显式quiesce的legacy member main-thread checkpoint/view，调用migration-only `materialize_thread_copy`复用copy mapping/校验引擎，只在原coordinator Session的`BoardMigrationRecord`冻结staging中生成goal-disabled、target-local child并保存legacy Session→child lineage/mapping；要么冻结并detach旧membership，让原Session继续作为独立Session。该内部原语不创建新Session/main、不写catalog/ledger、不自行发布或取得可执行owner；公开`full_rollout_copy`仍只创建独立target Session及main thread。active execution、runtime、lease或未收敛mutation不得复制；无法quiesce时不能创建缺失上下文的空child。

所有跨Session copy都必须先在source Session的shared `SessionReadGuard`下冻结不可变source snapshot manifest与已校验staging bytes，再在不持source guard时写入/发布target。guard覆盖source catalog解析、node/SQLite handle和view/checkpoint/artifact/detail capability读取，直到manifest durable；删除先关闭source fence则返回`source_session_deletion_pending|source_session_deleted`且不发布target，guard先取得则删除等待capture，guard释放后可继续删除而copy不得再次打开source。`full_rollout_copy`的target creation/publication与source guard分离；board migration先短持coordinator gate建立record/lease并释放，再按冻结顺序逐个source取得且一次最多持有一个guard，最后另行短持coordinator gate做publication CAS。禁止同时持有coordinator gate、source guard、另一个Session gate或两个数据库写事务；source删除在guard前胜出时整批migration定点aborted，guard先胜出时只阻塞该source capture，不阻塞后续staging/publication。

source capture还必须使用ContextStore一致性快照，而非只靠lifecycle guard。公开copy journal或board `MigrationChildCreationEntry`在capture前预登记唯一`source_snapshot_id`和不可被正常resolver发现的内部locator；在source SQLite固定read snapshot内同时冻结active view/checkpoint/control rows、`storage_commits`和每个JSONL committed end offset，通过可验证SQLite snapshot/online backup复制数据库，只读取offset以内JSONL及manifest点名且hash/length/capability校验通过的thread-local immutable detail。并发append/view变化在冻结revision之后时不得混入。capture先在该locator完成带operation/preimage、lifecycle generation、database hash、逐文件offset/length/hash、view/checkpoint revision、detail manifest hash和attachment claim manifest hash的`SourceCopySnapshot(state=captured)`及目录durability barrier，释放source guard后才CAS operation record到`source_captured`。恢复只定点接受完整且hash一致的captured snapshot；partial/prepared必须abort/清理，不得扫盘、从当前source补齐或在同一operation换用更新revision。

workspace attachment blob不得进入Session copy staging。source SQLite snapshot只记录logical attachment/variant identity、digest/length/availability、source owner/item ref与attachment catalog revision，并以journal绑定的IdentifierFactory预分配target-local item/attachment identity与mapping到source snapshot staging manifest。关闭source SQLite read transaction后、仍持source guard时，以一个workspace attachment catalog事务按`(copy_operation_id,source_attachment_ref,target_owner_ref)` create-or-get绑定`source_snapshot_id`、target Session/thread/item、digest/length和preimage的`ForkAttachmentClaim(preparing)`，原子验证source owner、blob availability/digest/length并阻止GC；全部成功后才把有序claim ID清单/hash写入captured marker。capture中途崩溃按精确copy operation ID枚举并释放本operation claim，不按digest或locator扫描。target/child可见性发布前，worker先把全部required claim推进`owner_reserved`并建立target owner refs；resolver仍要求target Session/thread catalog active，故预留ref不提前授权。有required claim的公开copy target在staging control DB预置绑定copy/preimage、lifecycle generation、publication preimage及claim ID/hash的`CopyAttachmentSettlementRecord(state=preparing)`，board由`BoardMigrationRecord`保存等价字段；publication把对应record推进为非终态`published_pending_attachment_commit`。无required claim的公开copy不创建settlement record，board publication直接进入终态`published`。发布后finalizer必须单独取得target/coordinator gate并复核active generation/preimage，先在已关闭session-control事务的情况下把attachment claims提交为`committed`，再以独立session-control事务把copy record推进`committed`或把board record推进`published`，禁止同时打开两个写事务。target删除竞争同一gate：finalizer先行则完成claim与成功record终态后，删除按普通owner ref释放；删除先关闭fence则排空流程按record中的精确claim ID释放`owner_reserved`的claim/ref，或验证已`committed` claim属于record冻结的target owner后按普通删除协议幂等、持久释放该owner ref，再把copy/board record分别推进终态`target_deleted|coordinator_deleted`，恢复不得重建或保留已删除target的owner ref或误报成功。未发布失败按record claim ID释放并进入`aborted`。`CopyAttachmentSettlementRecord`状态闭集为非终态`preparing|published_pending_attachment_commit`和终态`committed|aborted|target_deleted`；`BoardMigrationRecord`状态闭集为非终态`preparing|published_pending_attachment_commit`和终态`published|aborted|coordinator_deleted`。source已unavailable的非required历史ref可显式映射unavailable，required正文claim失败则copy失败。禁止复制blob bytes、扫描digest补claim、在claim与publication之间留下GC窗口、使用无类型terminal标志猜测结果或在settlement record非终态时隔离Session节点。

`pinned` fork还必须在capture前、且不持target gate/事务时，先取得workspace topology shared gate、再取得source exclusive gate并在source `session-control.sqlite`建立绑定fork、两侧GlobalThreadAddress、target creation operation/preimage、source generation及view/detail范围的`ForkRetentionClaim(state=preparing)`和retention占位。source删除先提交catalog deleting则fork不能建claim；claim先提交则删除必须在catalog deleting前以`source_retention_operation_pending|source_retained_by_fork`拒绝。target `target_committed`后只可另取source gate激活同一claim，不能首次补写。abort或target删除先在target持久化release intent并释放其gate，再单独取得source gate释放claim，最后确认target terminal；preparing claim不按墙钟过期，关联record缺失/冲突fail closed。fork/copy仅限同一workspace，federated grant不授权远端fork或retention mutation。

`ForkRetentionClaim`自身以`operation_kind=fork_retention`承担完整Session operation lease并与retention占位原子提交，禁止再建平行lease。

整块可见不依赖跨thread/workspace分布式事务。migration必须在创建任何child staging前，先于coordinator Session `session-control.sqlite` create-or-get不改变thread catalog/collaboration ledger的`BoardMigrationRecord(state=preparing)`，冻结operation/preimage、coordinator lifecycle、旧board/catalog revision，并为每个target内嵌唯一`MigrationChildCreationEntry`，包含child ID、最终/内部staging locator、GraphBinding/capability、source checkpoint/view、lineage/mapping与预期artifact manifest/hash；同operation不同preimage冲突。该entry是batch child唯一creation journal，普通ThreadCreationWorker不得枚举、发布或启动它，且不得为同一target建立独立`ThreadCreationRecord`。随后才按单source guard规则冻结source snapshot，把记录中的所有child artifact、mapping、权限和hash写入正常catalog不可见的staging区并durably flush，再把全部child原子rename到记录冻结且尚未被catalog引用的最终locator；每次恢复只枚举该record中的有限target。全部rename成功后，以同一`session-control.sqlite`事务CAS验证coordinator仍active、旧board/catalog revision及member/task preimage未漂移，再发布thread catalog、collaboration ledger全部locator/member/task mapping；无required attachment claim时record直接进入终态`published`，否则进入非终态`published_pending_attachment_commit`并在claim结算后转为`published`。该事务是唯一可见性提交点，后续结算不得改变board成员集合。CAS失败不得覆盖并发变更、重基或部分发布，只定点清理后标记`aborted`。rename失败同样不得发布；rename后/发布前崩溃时preparing record已经存在，故可定点继续或清理，禁止扫盘或吸收无record目录；发布后/terminal response前按catalog/ledger与record恢复原结果。发布前正常reader只见旧board，发布后只见完整新board；源Session保持只读和独立。不得只发布部分member/task、把一个member同时保留为跨Session与child，或在正常运行时解释staging/旧跨Sessionteam状态。

resident ThreadRuntime是durable owner的可回收执行缓存。统一residency manager为每个thread维护当前generation、lease、last activity和profile；child默认idle threshold为30分钟。没有active/runnable/pending execution、未收敛model/tool/mutation或lease，且debug owner已核实该thread没有`starting|running|paused|stopping`进程/`reconcile_required`阻断时，residency manager才决定关闭该thread generation的`LifetimeScope`，由scope释放compiled invocation、model/tool clients、in-memory CSM/ContextStore cache和stream execution fanout；scope不得自行计算idle、修改持久state、停止共享monitor、判断Node进程业务状态或产生source item。活动调试期间不累计idle时长，核实终态/lease结清后重新起算30分钟；backend重启时必须先恢复调试owner持久占用并核实进程，无法确认不得宣称cold。durable callback/queue只保存精确thread address；迟到callback先获取当前generation owner再提交，不能继续写已关闭实例。

产品提供只读`ThreadResidencySnapshot`观察面，至少包含`session_id`、`thread_id`、`residency=cold|loading|resident|unloading`、execution状态、`last_activity_at`、`idle_deadline_at`和脱敏`blocking_reasons[]`。它是runtime观测，不是canonical item、CSM source、team state或模型上下文；generation/lease细节只进入受保护trace和测试诊断。residency manager通过可注入单调`Clock`计算deadline：生产使用真实时钟，测试使用fake clock精确验证29:59仍resident、30:00转cold及lease阻断，不允许真实等待30分钟或为测试缩短产品阈值。

rehydrate只发生在execution admission或确实需要可写runtime的operation入口：从thread catalog、GraphBinding、checkpoint、CSM registration/revision、ToolSet applied binding和active view重建同一owner。history/detail/list和右侧侧边栏浏览保持cold path。unload/rehydrate自身不产生`ApplySourceLifecycleDecision`、不建立新prefix epoch、不读取当前文件重写旧source；tracked source只在恢复后的resource activation boundary消费ResourceRegistry已发布snapshot。

跨change的统一Web验收由`add-itemized-rollout-context`的`session-turn-history`规格和`tests/e2e/clients/web/test_basic_chat_tool_loop.py`唯一拥有；本change只向同一Python测试模块贡献CSM/Skill、main/child capability、fake-clock residency、send/read/wait及stable-prefix断言，不建立第二套平行E2E。允许该模块调用共享helper，但pytest collection、场景编排和PASS/FAIL gate必须落在该Python文件，不能把唯一验收藏在未被pytest收集的Node脚本中。

测试composition注入确定性但仍满足UUIDv4 bit/profile的`IdentifierFactory`，生产composition只使用随机UUIDv4 factory；Session/child仍经正常Gateway/Web API创建，禁止预写业务库。ModelStream fixture按这些已知SessionThread/model-call identity精确匹配并生成含裸ID/link的tool call，不得共享顺序cursor。该模块属于独占serial E2E组，以输出目录内带PID/start-time验证的`E2EProcessLease`租用避开8010–8016的整套loopback端口和进程owner manifest；有效lease不得抢占，stale lease只有确认owner消失后恢复，teardown只停止本次进程并验证端口释放，不能杀未知监听者或共享开发Gateway home。

## Risks / Trade-offs

- **[post-user source 的role与新epoch根资格]** → 同epoch一律独立user-role，避免暗中提高优先级；只有实际新epoch才按source owner typed`root_placement`决定受信完整状态是否进入唯一system root，默认外部MCP指引维持`tail_only`。Anthropic中途system能力只留TODO。
- **[untrack 不会立即消除 Skill 影响]** → 工具名称和结果明确描述“停止追踪”；既有上下文继续存在，不承诺即时移除。
- **[Provider adapter 自动合并连续 user message]** → 对 projector 输出做 frame-level golden test 和 dispatch 前 stable-prefix 校验；无法关闭合并的 Provider profile 显式 reject。
- **[bridge 之后仍有 middleware 修改 request]** → bridge必须位于最后一个 request-mutating hook，Provider preflight比较 sealed frame bytes/hash；任何差异阻断 dispatch，telemetry只能旁路观察。
- **[Gateway 全局与 workspace Skill 同名]** → 使用固定优先级和内部 catalog entry identity；模型只看到唯一有效名称，诊断保留 origin 但不泄露路径。
- **[文件读取期间变化导致错误 delta]** → 使用 stat/signature/hash 的稳定读取循环；无法获得一致快照时阻止本次 dispatch。
- **[watcher丢事件、重复事件或overflow]** → event只标dirty/gap，SourceReconciler按source descriptor有界重读，ResourceDerivationGraph按依赖DAG parse/diff/CAS；required资源未恢复readiness时阻断新dispatch，不把event sequence当来源或语义revision。
- **[共享watch task或已知来源订阅失效影响多个consumer]** → 每个channel独立背压，文件监视器暴露共享watch/consumer健康状态；config owner对受影响的候选来源订阅用独立scope完成shadow start、initial reconcile、原子切换和旧scope drain，失败继续使用上一份valid snapshot并显式标记stale/unavailable；释放机制不作reload决策。
- **[默认turn边界让文件变化到下一Turn才生效]** → 这是保证同一Turn可重复和上下文稳定的默认语义；确需更快生效的resource kind可显式选择`model_call`，但仍只消费Registry已发布内存snapshot。
- **[虚拟URI被误当作权限或真实路径]** → resolver只返回typed capability handle，operation执行点重新校验principal/scope/snapshot；URI不含credential/revision且从不直接join文件系统，storage以resource_id和snapshot_ref为权威。
- **[旧会话没有 source manifest]** → 返回明确 migration/source-mismatch，不从当前文件静默回填。
- **[多个 producer 并发提交同一 source]** → 在单一 owner transaction 中使用 source/revision/diff 幂等键和 conflict 检查。
- **[用户快速连续切换 ToolSet]** → safe boundary前只把最终 desired revision用于下一次 seal，但保留每次控制面revision及因果审计；已经 applied/sealed 的 ToolSet不可覆盖。
- **[切换时存在 outstanding tool call]** → 先按旧 assembly和原 `tool_call_id`产生真实 completion/failure/policy-denied terminal outcome，再 hard rebase；绝不制造未配对取消或虚假成功。
- **[Provider无法在新 ToolSet下投影旧工具历史]** → 阻止 rebase并返回具体兼容性错误；只有显式 compaction可建立另一 active view，不能由 adapter静默删除历史。
- **[30分钟idle回收与迟到callback竞争]** → active execution/mutation和runtime lease阻止卸载；generation fence使callback只能重新取得当前owner，旧实例无写权限，idle起点在每次有效活动后重新计算。
- **[用户把跨Session通信误认为共享team状态]** → tool/API结果明确返回resolved target main和单次operation状态；跨Sessionschema拒绝member/task/role/Goal字段，team状态只能由Session内部ledger产生E04 source。
- **[send后立即wait看到短暂无active Job]** → send返回持久`communication_id`和target acceptance ref，wait先跟踪该communication到execution binding再等待终态；禁止把accepted/queued误报为idle完成，target重启后从inbox恢复。
- **[模型通过simulate_user伪造真实用户Turn]** → 从模型工具schema删除该参数，可信user ingress只由UI/API acceptance owner建立并带可验证来源。
- **[旧跨Sessionteam只迁移部分成员]** → 从固定source checkpoint/view准备不可见target-local child staging，全部验证后由coordinator catalog/board单一可见性事务发布并保存journal；检测到active runtime、空child、部分board或混合成员时阻断运行。
- **[E2E通过缩短阈值或只看DOM产生假阳性]** → 使用注入Clock保持30分钟产品值，并联合校验DOM、API/通信账本、thread history/SSE与runtime trace；单一观察面不能单独判PASS。

## Migration Plan

1. 先按 itemized 新字段合同替换实验 v2 的自由 metadata 决策路径，并建立以 `(session_id, thread_id)` 为 key 的统一 `ContextMutationIntent` 端口、单一 thread owner transaction与后续受支持版本的显式schema migration，使 canonical append、source lifecycle、ToolSet switch和 epoch rebuild都不再旁路 ContextStore。旧实验 v2 字段形态不做就地升级或运行时兼容读取，遇到它保留原件并明确报schema-incompatible；对已经按新合同提交的 JSONL item，仍绝不原地改字节。独立 v1 一次性导入边界保持原规划。
2. 增加 stable-prefix epoch/reason/manifest、ToolSet compatibility key、desired/applied revision和 CSM source/tracking contract。
3. 先提取通用`EventChannelService`、进程内`LifetimeScope`，使既有TurnExecutionScope、watcher、MCP/client及runtime stop/close复用单一异步释放合同；内置file monitor按完整watch选项共享subscription并返回释放handle；Gateway内部snapshot和权威memory状态各按自身版本合同接入。再建立SourceReconciler、ResourceDerivationGraph、语义ResourceRegistry、initial reconcile/readiness；config owner而非scope负责候选配置与来源登记的shadow健康/原子发布，旧scope随后排空。把Job event bus迁为独立channel adapter，再迁移config、workspace file event、AGENTS和Skill consumer，迁完即删除各自旧watch loop与请求期reader/enumerator。旧ResourceManager在外部资源provider完成typed身份/状态核实后迁为只处理跨Turn持久operation lease的账本，删除工具参数猜测、cleanup policy及内存stopper控制。
4. 建立VRN grammar/resolver、三层SkillCatalog及metadata/activation facet；实现默认turn、可选model_call的ResourceActivationCoordinator和持久activation provenance，验证URI/path/credential不泄露、旧assembly不按当前URI重解。
5. 实现`skill_load`的snapshot/tracked/untrack及checkpoint-versioned control state，删除通用read激活、模型可见`/.boxteam/...`Skill路径/挂载、旧Skills/AGENTS request-time middleware和任何即时移除规划/入口。
6. 建立logical thread owner与resident runtime的generation/lease/idle-unload/lazy-rehydrate边界；验证child默认30分钟cold切换不改变CSM state、ToolSet binding、stable prefix或历史，cold read不创建runtime。
7. 先把Goal限定到main thread，并以migration-only `materialize_thread_copy`读取source checkpoint/view、写不可见staging，再由coordinator单一可见性事务将旧team board迁为Session内部child-thread ledger，或freeze/detach；不调用公开`full_rollout_copy`创建新Session，不复制active runtime、不假设跨库事务。再按R01–R09、E01–E07迁移初始instruction、文件和事件producer，删除直接内部`HumanMessage`、checkpoint message mutation和模型工具伪造user Turn的入口。
8. 建立中心Gateway经SSH `-L`到spoke的长期全双工WebSocket对等RPC channel，以稳定`gateway_id`路由显式URI，并让裸ID只经local+唯一hub的有界fan-out解析target main。拆分持久connection config ID与瞬时channel/route lease；实现origin-preserving、最多一次`B → A → C`的transit grant、默认允许核心操作且可原子热发布的最新policy检查，以及source outbox/target inbox恢复。接入跨workspace/server send/read/wait，以默认60秒、最大300秒且可恢复selector的`wait_for_session`替换旧monitor；hub不保存业务通信状态，权限变化不修改ToolSet、上下文或stable prefix。
9. 让ToolSelectionStore/ToolService、execution step、ToolSet registry与assembly compiler在每次model call safe boundary执行C02 hard rebase，覆盖outstanding call convergence、同Turn多epoch和Provider历史兼容性失败。
10. 分离D01/C01/C03 owner，删除PromptReplay与P01的所有来源反推、ToolSet反向快照和merged system fallback；框架确需hook时把`ItemizedContextProjectionMiddleware`替换为无状态sealed-assembly dispatch bridge，否则直接删除，并接入rewind、compaction、restart和retry。
11. 在`tests/e2e/clients/web/test_basic_chat_tool_loop.py`完成由itemized规格统一拥有的多Session/main-child/cold-runtime/跨workspace协作场景；cold/restart前后逐字段核对持久GraphBinding四元组及实际factory revision，新runtime generation不得改用latest graph。三个隔离Gateway进程组成真实loopback `B ⇄ A ⇄ C`，验证A主动建channel但B能反向发起、响应原路返回、source保持B、默认核心权限可用及连接中撤权/恢复按下一实际使用点生效。模型回放按SessionThread/model-call identity匹配。pytest外置`E2ETestControlHarness`分别持有residency/wait单调时间域，以及按operation identity/phase寻址的`CommunicationAdmissionBarrier`、`SessionLifecyclePhaseBarrier`和`CopyCaptureBarrier`；它们覆盖communication acceptance/binding、deletion journal/fence/final rename、source revision freeze、attachment claim/captured marker/owner_reserved、target/board publication及attachment finalization。test backend只通过fixture composition的内部port注入可暂停并ack的phase observer，pytest收到ack后从进程外终止/重启；生产composition只绑定真实clock与立即返回的no-op observer/barrier，不注册test client、HTTP/model入口、热开关或backend自杀hook。barrier不得修改业务状态、identity、时间或结果，harness状态不得写入业务库/checkpoint/canonical时间。联合Provider frame、真实请求日志、checkpoint manifest、history/SSE/runtime trace和架构审计后再启用新路径。

回滚只能撤销尚未提交的 CSM/assembly transaction。已经提交的 source item、旧 assembly 和历史不得删除或原地修改；新 projector 无法恢复 required source 或稳定前缀时，保留旧事实并阻止新 dispatch。
