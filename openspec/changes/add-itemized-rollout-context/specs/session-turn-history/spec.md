## MODIFIED Requirements

### Requirement: 历史投影区分 assistant text、reasoning 和 final response

系统 SHALL 独立支持 `assistant_text`、`thinking`、`tool_summary`、`tool_call`、`tool_result`、`compaction_summary` 和 `final_response`。`assistant_text` 是从 canonical `assistant_output` item/content part 派生的 projection，不是 canonical item kind；history `compaction_summary` part必须直接引用同一Turn中`semantic_kind=compaction_summary`的canonical item identity/view revision，不得从压缩后的普通文本或notice猜测。历史 Turn 的开头 MUST 使用 `TurnRecord.root_input_item_id` 或等价的 root input identity；不得把第一个 wire role 为 user 的 item 当作 Turn 起点。`thinking` 投影由 canonical reasoning item 或受保护 reasoning content part 表达，块类型为可读 `reasoning`、provider 生成的 `summary` 或不携带正文的 `encrypted` 标记。`final_response` MUST 使用 `TurnRecord.final_item_id` 或等价的 final item identity，而不是把最后一个 assistant role 作为唯一依据；`Turn.status=completed` 时 final item 必须存在，`completed_empty`、interrupted、cancelled、failed、unknown 或未 finalization 时 final item 必须为空。若为一次性 `legacy_import_v1_to_v2` staging 的无 finalization 旧 fixture，只能在 migration report 中标记 heuristic 提示；正常 v2 history/provider/checkpoint/runtime 不得启用 heuristic fallback。`cancelled` 仅表示该 Turn 被明确停止且不可作为成功响应，不得被历史投影成 final response。

#### Scenario: 混合 canonical item 的 Turn

- **WHEN** 一个 Turn 依次包含 `assistant_output`、reasoning item 和 tool call
- **THEN** 历史 projection 可以从 assistant output content part 独立返回 `assistant_text`、thinking 和 tool summary，LangChain 恢复只按 message group 生成需要的消息，不因 projection 拆分而伪造多条 assistant message

#### Scenario: 思考 item 来源保持可区分

- **WHEN** Provider 返回可展示 reasoning、provider summary 和/或 encrypted reasoning
- **THEN** Web 分别返回 `reasoning`、`summary` 和无正文的 `encrypted` 块，encrypted payload 只能用于 provider 恢复，不得出现在 API 响应

#### Scenario: 上下文压缩作为独立历史 Item

- **WHEN**一个Turn在工具循环中提交了可见且`turn_scope=turn_member`的canonical `compaction_summary`
- **THEN**history按其原item identity/order返回一个可展开`compaction_summary` response part，既不伪装成thinking/final，也不与压缩前后的reasoning或tool part合并

#### Scenario: runtime notice 不伪装成 Turn root

- **WHEN** 一个隐藏的 `system_reminder` 在下一条普通用户输入之前存在，且它在 LangChain/provider projection 中暂时使用 user role
- **THEN** 历史服务仍以普通用户 input 的 `root_input_item_id` 开始新 Turn；runtime notice 只按其 semantic kind、scope 和 relation 展示或隐藏

### Requirement: Agent state 快照不等同于默认历史 projection

系统 SHALL 将 LangChain checkpoint 的 agent-state 快照、canonical item 和 Web/history projection 视为三个不同层次。`get_agent_state_messages` 在兼容序列化时必须保留 `AIMessage.content` 中经过规范化的有序 reasoning/text/content carrier、tool call 字段和必要的 content-part identity，包括最终 assistant 的 reasoning 与可见文本；它不得因为旧调用方只需要可见 text 而静默删掉 reasoning，也不得把该快照写回为第二份 canonical item。

默认历史与 Web projection SHALL 从 canonical item/index 派生 `assistant_text`、thinking、tool summary、可见`compaction_summary`和 `final_response`；它可以隐藏 reasoning/compaction正文并只返回受权限控制的摘要/引用。Provider request projector SHALL 在 request 边界依据目标能力过滤或编码 reasoning，不得通过改写 agent-state 快照、canonical item 或已提交 checkpoint 来过滤。

#### Scenario: final assistant 的两种读取视图

- **WHEN** 一个已完成 Turn 的 checkpoint 含有按顺序排列的 reasoning 和 text content blocks
- **THEN** agent-state 快照保留两个 blocks 以支持 LangChain/诊断恢复；默认 history 只返回 `assistant_text` 和按策略决定的 thinking projection；两者都引用同一 canonical item，不创建 `assistant_text` canonical 记录

#### Scenario: 旧 text-only consumer

- **WHEN** 旧调用方断言 final agent-state 只有 text
- **THEN** 系统将其标记为 legacy compatibility mismatch；不能为了通过该断言静默删除 canonical checkpoint 中的 reasoning，Provider projector 也不能绕过目标能力策略直接发送该快照

### Requirement: Turn acceptance identity 在历史层保持 thread-local 唯一

历史服务和 Turn resolver SHALL 将 `accepted_ingress_id` 与 `acceptance_idempotency_key` 视为两个不同的 thread-local identity，并分别约束 `(session_id, thread_id, accepted_ingress_id)` 与 `(session_id, thread_id, acceptance_idempotency_key)` 唯一；每个 identity 只能一对一指向一个 accepted Turn。相同 ingress、相同 acceptance key、相同 payload hash 和相同 origin branch 的重复 acceptance 只能返回既有 `turn_id`、root 和 initial execution，不创建第二个历史 Turn；同 ingress 被不同 key 重用、同 key 搭配不同 ingress/payload/branch，或跨 thread 直接复用裸 identity 时，必须返回明确 acceptance idempotency conflict，且不修改原 Turn 或历史顺序。跨 session fork 的 copied acceptance identity 必须先映射为 target-local 值，source identity 只在 lineage/audit 中可见。

#### Scenario: 重复 acceptance 不产生第二个历史 Turn

- **WHEN** 同一 SessionThread 收到相同 `accepted_ingress_id`、`acceptance_idempotency_key` 和 payload hash 的重试
- **THEN** resolver 返回原 Turn 的 root 和 initial execution，历史分页仍只显示一个 Turn

#### Scenario: acceptance identity 冲突不修改历史

- **WHEN** 同一 `accepted_ingress_id` 被不同 acceptance key 使用，或同一 key 的 payload hash/origin branch 不同
- **THEN** 服务返回可诊断的 acceptance idempotency conflict，不创建、重编号或覆盖任何 Turn/item

### Requirement: 历史摘要不 materialize 完整消息

历史投影 SHALL 识别统一的 `Turn.status` 闭合集合 `open | active | completed | completed_empty | interrupted | cancelled | failed | unknown`；`completed_empty` 是唯一的无 canonical output 正常终态名称。`completed` 才能通过 `final_item_id` 返回正常 final response，`completed_empty`、`interrupted`、`cancelled`、`failed` 和 `unknown` 不得伪装成成功响应；普通 `cancelled` 与 `full_rollout_copy` 的 `cancelled` historical 均不可对原 Turn 执行 `resume_turn` 或 `dispatch_replay`，请求必须返回 `turn_not_resumable`；`history_replay` 只能在同一 owner namespace 的 history view 中复用 source Turn/root 且不创建 execution；若需要重新执行，只能由独立的 `replay_as_new_turn` 创建新的 Turn/root/acceptance/initial execution，在 active view 登记新的 `logical_turn_ordinal`，并记录 `replay_of_turn_id`；source history 可以作为上下文前缀，但 source Turn/root 不是新 Turn 的 root；新输入也创建新的 Turn。

#### Scenario: history_replay 与 replay_as_new_turn 分离

- **WHEN** 历史服务从某个 Turn/view/anchor 执行 `history_replay`，或调用方明确选择 `replay_as_new_turn`
- **THEN** `history_replay` 只在同一 owner namespace 的 history view 中登记并复用 source `turn_id`、`root_input_item_id` 和历史 ordinal，不创建 execution；`replay_as_new_turn` 则在 active view 登记新的 target-local Turn、`user_input` root、acceptance、initial execution 和新的 `logical_turn_ordinal`
- **AND** `replay_as_new_turn` 可以复制或引用 source history 作为上下文前缀，但 source Turn/root 只能作为前缀或 lineage，不能成为新 Turn 的 root；对普通或 full-rollout-copy 的 cancelled Turn，原 Turn 的 `dispatch_replay` 仍返回 `turn_not_resumable`，不能由该错误响应隐式创建新 Turn

`tool_summary`、`thinking`、final pointer 和受界限的最终 `visible_text` SHALL 从 SQLite item/index projection 读取。中间完整 `assistant_output` item、tool_call 参数和 tool_result 只在显式 include 时按 item offset 读取目标 `rollout.jsonl` 记录；普通分页不得扫描整个 rollout 文件、恢复完整 canonical item 集合或 materialize 完整 LangChain checkpoint，也不得把 request-only system context、缓存保持型 source delta 或 pending runtime notice 当作历史 Turn message；这些上下文只能通过 assembly/provenance reference 在授权详情视图中展开。`visible_text`、`reasoning` 和 `summary` 均不得包含 encrypted reasoning 或未授权工具 payload。

Turn标题的`item_count` MUST只统计同一history revision、显式属于该Turn且被projection标记为`expandable_activity`的逻辑成员：每个可见reasoning/summary content part计1，每个tool_call参数计1，每个匹配tool_result计1，可见且以`turn_scope=turn_member`归属该Turn的`compaction_summary`计1，其它扩展类型只有显式声明相同activity语义才计数。聚合`tool_summary`行、DOM容器和checkpoint/provider carrier不另计；真实user root、final response、transport chunk、encrypted-only无正文标记、request-only contribution、CSM source/隐藏runtime notice及仅有存储metadata的记录均不计。相同持久logical identity无论被多少carrier引用只计一次，正文相同但identity不同的可展开成员分别计数。每个成员的`elapsed_ms` MUST由后端按logical order计算：首个成员相对Turn acceptance/root时间，后续成员相对前一个计数成员；`duration_ms`从acceptance到Turn terminal时间。时间缺失、倒退或membership revision不一致必须返回显式projection错误，不得由Web钳零、补默认值或使用物理相邻item代替。

#### Scenario: 默认摘要只读取轻量投影

- **WHEN** 前端请求默认的最新 Turn 摘要
- **THEN** 服务从 SQLite projection 和目标 JSONL item offset 读取 user、thinking blocks、tool summary 与 final response，不 materialize 完整 LangChain message list，也不读取 tool_result 正文

#### Scenario: 显式详情读取目标 item

- **WHEN** 用户只为当前 Turn 请求 tool_call 和 tool_result
- **THEN** 服务只定位并读取该 Turn 命中的 item 记录，不扫描整个 rollout 文件或其它 Turn

#### Scenario: source overlay 只在详情视图展开

- **WHEN** `AGENTS.md` 或已使用的 skill source 存在未物化的 base/delta overlay，用户请求默认 Turn history
- **THEN** summary 不把 overlay diff 当作 assistant/user 文本；获得授权的 provenance 详情请求才可按 assembly/source reference 返回其版本、来源和有界 diff 摘要

#### Scenario: 聚合工具行与上下文压缩按逻辑成员计数

- **WHEN**一个Turn含一条可见reasoning、一个tool_call、一个tool_result、一条Turn-member compaction summary和最终response，Web把call/result渲染为一个聚合工具行
- **THEN**后端`item_count`为4，聚合行不产生第五项、final不产生第五项；每项elapsed按logical order给出，刷新后不得按DOM节点数或物理sequence重算

## ADDED Requirements

### Requirement: Turn 和历史投影必须精确归属于 SessionThread

Turn acceptance identity、`turn_ordinal`、root item、history view 和 `final_item_id` SHALL 在 `(session_id, thread_id)` 范围内唯一；不得再把它们解释为仅 session-local。默认 Session history 只解析 main thread，显式 thread history 只读取该 thread 的 catalog/index/rollout。child thread 的 assistant/tool/result 不得混入 main-thread Turn，跨 thread 汇报必须作为带 source-thread provenance 的独立 canonical/runtime item，由目标 thread 的 owner 提交，不得把原 item 复制为普通用户输入。

#### Scenario: delegated child history 不污染主聊天

- **WHEN** delegated child thread 产生 tool result 或最终报告
- **THEN** 主 thread 的默认历史不包含 child 的原始 Turn/item；若系统需要向主 thread 发送汇报，则该汇报保留 child `session_id/thread_id` 和 source item/execution identity，且不是新的真实 user Turn root

### Requirement: 历史详情为 provenance 和扩展视图保留稳定引用

历史服务 SHALL 为 item、source、middleware contribution、独立 `ToolSetRef` 和 ContextAssemblySnapshot 提供稳定且可权限控制的 reference，允许未来的特定历史视图或扩展按 reference 请求来源详情。默认 Turn summary/detail MUST 只返回安全摘要、可用性和引用标识，不自动返回 middleware 内部状态、完整 prompt、tool schema 或受保护 payload；ToolSetRef 只能作为受策略控制的 assembly/provenance metadata 展示，不能成为 canonical message 或 Turn item。其 snapshot id、source revision、content length、hash token、schema/policy version 和绑定 policy 属于 plan_hash manifest identity，历史 reader 不得用当前工具 registry 取代该 manifest；`ContextContribution.contribution_kind=tool_set` 不是合法历史贡献类型，legacy 值只能 quarantine。

#### Scenario: 默认历史不泄露 middleware 细节

- **WHEN** 用户请求默认历史 Turn
- **THEN** 返回 item 内容、状态和可选的 provenance summary/reference，不返回完整 middleware prompt、内部 state 或受保护 tool/reasoning payload

#### Scenario: 扩展按 item 展开来源

- **WHEN** 一个获得授权的扩展根据 item reference 请求 middleware/assembly 详情
- **THEN** 服务只读取该 item 命中的 provenance 和 assembly metadata，按 retention、visibility 和大小预算返回可展开详情，不加载整个 rollout

### Requirement: 历史资源 provenance 必须安全、稳定且不计入 Turn Item

历史内部projector SHALL 从sealed `ContextAssemblySnapshot`读取并校验`ResourceActivationSnapshotRef`和`ResourceProvenanceRef`的稳定resource identity、snapshot kind/parent、policy revision/hash、effective boundary、captured generation、安全`display_uri`、resource kind/scope/facet、revision、length/hash marker、availability、activation ordinal与assembly identity。普通summary/detail响应只返回策略允许的safe display字段和opaque typed provenance ref，raw `resource_id`仅可进入受信诊断，不得作为普通客户端locator暴露。Resource activation snapshot、Skill metadata/activation、AGENTS、配置、团队状态和其它request-only contribution不是canonical Turn member，不得计入用户可见`item_count`、`elapsed_ms`、thinking/tool/compaction展开顺序或final response；只有真实canonical item按权威`turn_item_projection.logical_item_ordinal`参与这些统计。

默认历史不得返回资源正文、内部 `provider_locator`、绝对路径、network credential、memory key 或可反推出它们的调试字段。获得授权的详情请求可以通过 typed resource reference 读取当时 sealed body；即使当前 ResourceRegistry 已发布新 revision、虚拟 URI 改映射或原始文件消失，也只能读取持久化 snapshot/ref，不能查询当前资源来重建历史。live、终态 history、刷新恢复和跨设备 history MUST 使用同一 committed resource ref identity；前端不得从提示文本、`read` 工具调用、URI 字符串或 DOM 节点推断、去重或补造 provenance。

#### Scenario: 零可展开 Item 的 Turn 仍可带资源 provenance

- **WHEN** 一个 Turn 使用了 system/Skill/AGENTS resource snapshot，但没有 reasoning、tool_call、tool_result 或 compaction_summary canonical item
- **THEN** 历史可以在授权详情中返回该 activation snapshot 的安全 provenance，Turn 标题仍显示权威时长与 `Item 0 项`，折叠体显示没有可展开的中间消息；不得把 resource binding 或首条 system/Skill 内容计成 Item

#### Scenario: live 与刷新显示同一资源 revision

- **WHEN** live Turn 已 seal resource revision R1，Turn 完成后 Registry 发布 R2，随后用户刷新页面或从另一客户端读取历史
- **THEN** 所有历史表面都按同一 assembly identity 显示 R1 的安全 `display_uri`/revision/availability，不能把 R2 投影为该 Turn 的来源，也不能因文本相同合并不同 resource identity

#### Scenario: 历史详情隐藏物理来源

- **WHEN** 授权用户展开来自 workspace、Gateway、builtin 或 memory 来源的 resource provenance
- **THEN** 响应只返回对应命名空间的 `boxteam://` display URI 和策略允许的 manifest；绝对路径、provider locator、credential、memory key 及旧 `/.boxteam/...` 路径均不出现在 API、SSE 或 DOM

#### Scenario: 旧 path-based 投影不能恢复

- **WHEN** legacy UI/projector 尝试从 generic read 的 path、Skill 正文或旧 middleware 字段反推来源
- **THEN** 正常 v2 history 明确拒绝该 projection 或标记 migration-only loss，不创建虚拟 resource ref、不回退当前 Registry，也不保留兼容字段

### Requirement: 当前 Session 的 child thread 必须作为右侧侧边栏会话资源可见

主窗口的标准会话区 SHALL 默认展示当前 Session 的 main thread。当前 Session 的 durable child thread SHALL 作为 Session 级资源出现在主窗口右侧侧边栏，并提供独立的状态、历史、未读和直接消息入口；child thread 不得作为新的产品 Session混入左侧 Gateway/Session导航，也不得把自己的原始Turn投影进main-thread时间线。

打开child thread列表、状态或历史 MUST 使用持久metadata/history projection，不得唤醒cold Agent runtime。用户从右侧侧边栏向child发送消息时，客户端 MUST 携带精确`session_id + thread_id`，响应和SSE/history cursor也必须返回并校验同一thread；不得退回裸Session默认main-thread路由。

#### Scenario: 在右侧侧边栏查看 cold child 历史

- **WHEN** 用户在当前Session右侧侧边栏选择一个已经卸载运行资源的child thread
- **THEN** 界面展示该child独立历史、状态和来源关系，目标runtime保持cold，main-thread聊天内容和选择保持不变

#### Scenario: 从右侧侧边栏向 child 发送消息

- **WHEN** 用户在child-thread对话入口提交一条消息
- **THEN** 请求精确定位该child并在其历史中创建真实用户Turn；实时事件、工具项和最终响应只更新对应child-thread视图，不进入main-thread消息流

#### Scenario: child 汇报与原始历史分离

- **WHEN** child thread完成任务并向parent main thread汇报
- **THEN** main历史只显示带child来源引用的汇报item，用户可从该引用进入右侧侧边栏查看child原始历史；系统不得把child的全部Turn或工具结果展开复制到main时间线

### Requirement: 基础 Web tool-loop E2E 必须验收完整 Session 协作场景

仓库 SHALL 在已有 `tests/e2e/clients/web/test_basic_chat_tool_loop.py` 内保留基础两轮聊天/工具循环验收，并在同一 pytest 模块内增加多 Session main thread、Session内durable child、resident runtime回收、跨workspace/server send/read/wait、Session内team状态及右侧侧边栏child对话的分离用例。该模块是这一组跨change业务场景的唯一Web E2E owner；可拆成多个test function并调用共享Python/Node helper，但pytest collection、场景fixture和PASS/FAIL gate必须位于该模块，不得把唯一断言放进未被pytest收集的脚本，也不得在其它E2E中另造一套不同语义的验收流程。进程内`LifetimeScope`的释放合同、共享watch及外部资源lease由`add-context-injection-lifecycle`唯一拥有；本change只验证thread residency owner调用该合同后的可观察结果，不建立第二套dispose/ResourceManager。

验收 MUST 同时使用用户可见DOM、Gateway/workspace网络回执、thread-qualified history/SSE cursor、权威item/time/order投影和只读runtime/trace证据；只检查DOM存在、健康端点或数据库行都不足以通过。基础两轮使用固定golden；每个新增可见Turn也 MUST 从权威history projection取得预期item identity/order、`item_count`与`elapsed_ms`，并与live DOM及刷新后的DOM精确一致：同正文不同identity保留，同identity只出现一次，无可展开item时count为0且标题不得显示首条思考。测试 MUST 使用 `out/tests/e2e/clients/web/test_basic_chat_tool_loop/workspace/` 作为隔离根，将多个workspace分别放入`hub/`、`spoke-b/`、`spoke-c/`等确定性子目录并分配隔离端口，产物放同级`artifacts/`。A/B/C Gateway必须作为三个独立进程，分别使用同名输出根`runtime/gateways/{hub-a,spoke-b,spoke-c}/boxteam-home/`作为`BOXTEAM_HOME`，只在这些目录内建立稳定identity、测试peer credential与policy snapshot；A通过真实loopback SSH转发等价fixture建立到B/C的全双工WebSocket channel，跨spoke场景必须经过`B → A → C`及`C → A → B`响应路径，不得读取或写入用户正常`${BOXTEAM_HOME}`/`~/.boxteams`，也不得以resolver stub、直接请求远端workspace或B/C直连冒充。pytest拥有的外置`E2ETestControlHarness` MUST 分别提供`residency_clock`、`wait_deadline_clock`，以及按operation/phase寻址的`CommunicationAdmissionBarrier`、`SessionLifecyclePhaseBarrier`和`CopyCaptureBarrier`；后两者至少覆盖deletion journal/fence/final rename、source revision freeze、attachment claim、captured marker、owner_reserved、target/board publication及attachment finalization。测试backend只通过fixture composition的内部port连接；生产composition只绑定真实时钟与立即返回的no-op phase observer/barrier，不得注册test client、HTTP路由、模型工具或热开关。barrier只暂停并上报phase，不修改业务状态/identity/时间/结果；pytest收到ack后从进程外终止/重启进程，backend不得由test hook自杀。harness socket/log只写同名输出根`runtime/test-control/`和`artifacts/`，不得进入业务库/checkpoint/canonical时间戳；fixture teardown必须关闭全部进程/端口。测试可推进虚拟时间但不得sleep真实阈值或改短产品合同。所有模型请求使用本模块拥有且按SessionThread/model-call identity匹配的确定性ModelStream fixture，禁止访问真实Provider或由并发thread共享顺序游标。

动态identity MUST 通过pytest fixture composition注入的确定性`IdentifierFactory`生成，且每个值仍满足UUIDv4 bit/profile与canonical ID validator；生产composition只绑定随机UUIDv4 factory。测试仍必须经正常Gateway/Web API创建Session/child，不得预写数据库；ModelStream manifest使用已知identity生成并精确匹配裸ID/link/tool args，不得忽略动态字段。该模块 MUST 标记为独占serial E2E组，并用同名输出目录内带PID/start-time校验的`E2EProcessLease`租用避开8010–8016的整套loopback端口和owner manifest。有效lease不得被抢占或通过杀进程清理；stale lease只有验证owner已不存在后才可恢复。每个case使用独立fixture子目录/identity seed，teardown只停止owner manifest列出的本次进程并断言端口释放。

#### Scenario: 基础两轮 tool loop 仍是前置验收

- **WHEN** 运行 `uv run pytest tests/e2e/clients/web/test_basic_chat_tool_loop.py`
- **THEN** 基础两轮分别按后端权威坐标精确断言时长、item数量、reasoning→tool→reasoning展开顺序和刷新恢复；不存在按文本去重、重影item或“无消息但计数”。任一新场景不得替代这些断言

#### Scenario: 多 Session 与 child 右侧对话在真实页面中隔离

- **WHEN** E2E创建Git/分析/实施/汇总Session，并由分析或实施main创建child，再从右侧侧边栏打开该child、查看历史，并在已有委派执行期间发送用户消息
- **THEN** 左侧Session导航只列出产品Session，main/child的DOM、历史、SSE/cursor和工具item只更新精确thread；新消息先显示queued，第一条execution继续至少一次tool-loop时其Provider request不包含queued root，刷新历史仍按第一Turn完整tool/final→第二Turn排序而不按物理append交错。同一child按FIFO执行而sibling child可并行，child显示Goal disabled，汇报只以一条带source-thread provenance的main item出现

#### Scenario: idle unload 与 cold history 在 Web E2E 中可证明

- **WHEN** fake clock依次推进到29分59秒、30分00秒，并在cold后只打开child历史，最后发送一条新消息
- **THEN** 只读residency与trace证明仅在30分钟且无blocker、该generation的`LifetimeScope`已取消并排空自有task/句柄后转为cold；另以当前child保有`launch_pending`进程claim或正在`starting|running|paused|stopping`的Node调试进程推进同一fake clock，验证仍resident且显示脱敏debug blocker，debug owner核实终态/结清lease后重新起算30分钟。其它consumer仍持有的共享watch和跨Turn terminal/browser/MCP不随child关闭。查看历史不唤醒，新消息才reload；reload产生新的runtime generation，但持久`GraphBinding(graph_id, graph_revision, graph_schema_hash, capability_profile_hash)`逐字段等于卸载前值，Provider/runtime trace证明该次执行使用这一精确binding而非当前latest graph。上述过程不改变已提交item、stable prefix、sealed bytes或历史排序

#### Scenario: Session 内 team 与跨 Session 无状态边界可见

- **WHEN** E2E把同一Session的两个child加入team，在一个child排队期间及其active tool-loop两次model call之间修改role/task/coordinator，再尝试把另一个产品Session attach为member或通过跨Session消息携带team/task/Goal状态
- **THEN** 右侧侧边栏及权威ledger只显示本Session child member；默认`turn`边界在child取得active slot时冻结当时最新resource snapshot，当前tool-loop后续call保持不变，显式`team_state=model_call`时才在后续call激活新revision；queue acceptance不冻结过早上界，cold child不被fanout唤醒，请求路径不读取ledger。跨Session状态请求显式失败且不产生部分ledger/context mutation，正常send/read/wait仍只保留消息幂等和来源审计

#### Scenario: 跨 workspace/server 协作支撑通知和周报

- **WHEN**spoke B上的实施Session向spoke C上的Git Session发送消息并以回执`communication_id`立即等待，且用户在汇总Session的真实消息中提供本地/远端Session ID或link，由模型经中心A的全双工channel读取/等待多个Session并生成周报；期间保持channel连接并依次热发布撤销和恢复read/wait的policy revision
- **THEN**Gateway-local与`boxteam://gateway/{gateway_id}/workspace/...`目标都解析到main thread，跨spoke请求只走`B → A → C`且响应原路返回，C保留B的真实source thread。默认无规则配置允许全部核心操作；撤权后的下一次使用或披露立即返回`authorization_revoked`，恢复后下一次调用立即成功，channel、ToolSet和stable prefix不变。send→wait在accepted/queued时不返回假idle，fake deadline timer验证默认60秒/最大300秒及timeout可恢复结果，报告使用带source refs的调用方tool-result item；读取/等待不唤醒或改写目标，重试不重复投递，跨Session无team/task/Goal共享状态，歧义、未知gateway、错误audience/path、过期/重放grant、未授权operation/target/principal、credential失效和不可达均在workspace前显式失败且无目标副作用

#### Scenario: 周报分页固定远端历史快照

- **WHEN** 汇总Session通过真实Web流程分页读取至少一个远端Session，在第一页后给目标追加Turn并执行独立rewind/view变化，再复用cursor读取后续页和生成周报
- **THEN** 第二页使用新的source call/tool invocation，但opaque cursor恢复首个AEAD `ReadContextSnapshot`绑定的同一`observation_id/read_series_id`；所有tool-result/source refs与最终报告只包含其冻结revision和item/Turn上界。每页重新授权，撤权、expiry、参数冲突或token篡改分别显式失败且不泄露内部字段、不切换到新view、不唤醒或改写目标

#### Scenario: 刷新和后端重启不丢失协作身份

- **WHEN** 测试harness在target进程外持有`CommunicationAdmissionBarrier`协调状态，target经内部test port在一条communication已经target-accepted但尚未execution-bound时暂停；测试重启target workspace backend，新进程重连同一barrier，随后由harness释放，在source尚未调用read/wait时先观察只读communication/trace证据，再调用wait并刷新页面
- **THEN** InboxAdmissionWorker已主动恢复同一job/turn binding，wait只观察该binding并只产生一次目标ambient item；main/child locator、逐字段相同的持久GraphBinding及其精确factory revision、独立历史、communication/wait终态、cold residency、item数量/时序和右侧侧边栏选择均从权威持久事实恢复，不回退到session-as-thread、latest graph或前端缓存

#### Scenario: source 丢失 receipt 后仍只投递一次

- **WHEN**target已经durable acceptance，而source在持久化receipt前由harness终止；测试重启source并恢复同一tool/API invocation
- **THEN**source按原send operation找回同一outbox/communication，target以新grant返回原acceptance receipt；DOM、双端communication/trace和target history证明没有第二个ambient item、Job、Turn或可见消息

#### Scenario: source 丢失 read 或 wait response 后恢复同一 observation

- **WHEN** target已冻结远端read page或无selector wait baseline，而source在保存response/tool result前重启；期间目标新增Turn或Job
- **THEN** source以同一source call/operation invocation和新grant恢复原RemoteObservationRecord/PageRecord，page、selector集合、baseline及terminal envelope不变，该调用方DOM/history只出现一个tool result；read下一页使用新source call但仍属cursor绑定的同一observation/read series，record过期明确失败，不把目标新状态静默混入

#### Scenario: 远端裸 ID discovery 不泄露或递归

- **WHEN**用户消息只包含远端裸Session ID，或该ID有多个已授权候选、只存在未授权候选，并在wait期间撤销权限
- **THEN**真实Gateway trace证明source只查询local workspace并经唯一hub对其它spoke做一次有界fan-out，`max_transit_gateways=1`且`max_gateway_hops=2`；workspace未知时hub transit discovery grant只能执行target spoke本地exact lookup，唯一授权目标确定后才另发绑定完整target、origin和path的operation grant。歧义只返回候选数并要求qualified link，未授权与不存在不可区分；nonce在hub/target重启后重放仍被持久registry拒绝。重连轮换channel epoch/route revision时，已提交outbox仍绑定原GlobalThreadAddress和communication/preimage且不重新裸ID选路。默认无规则时核心能力可用；registry故障、wait grant寿命不足或中途撤权均明确失败，恢复权限后下一次实际调用立即生效；cursor/selector不能绕过最新授权，所有拒绝均无目标context副作用

#### Scenario: 物理交错与真实压缩都按逻辑 Item 投影

- **WHEN**E2E用admission barrier让同一child第二个queued Turn root先于第一Turn后续reasoning/tool/final物理提交，并为专用Session在创建另一Turn前通过正常workspace配置/ToolSet API启用`compact_conversation`，再由确定性模型调用该真实产品工具触发context compaction
- **THEN**history API、live DOM和刷新DOM都按各Turn的`logical_item_ordinal`逐项匹配identity、`item_count`和`elapsed_ms`；另一个Turn的root不进入首Turn，canonical `compaction_summary`除该压缩操作自身可能存在并分别计数的tool_call/tool_result外只额外建立一个ordinal，且不伪装成thinking/final或重复carrier
- **AND**测试不得用生产不可达的test-only压缩开关、绕过正常ToolSet启用流程、直接预写projection或按DOM节点反推预期值

#### Scenario: Session 删除与新副作用在 Web E2E 中线性化

- **WHEN**E2E用外置barrier分别让child发布、team fanout、target inbox/execution binding及独立`skill_load`/ToolSet control与同一Session删除竞争，并在workspace catalog已deleting但部分local fence仍active的故障点重启backend
- **THEN**操作先行时只存在捕获旧generation的可恢复lease且删除等待terminal；删除先行时没有新thread/context/inbox/ambient/wakeup/Job/attachment副作用。已开始且持有shared `SessionReadGuard`的cold history阻塞删除取得topology exclusive并提交catalog deleting，reader完成后删除才线性化；catalog已deleting但部分local fence仍active的崩溃恢复窗口只允许catalog metadata观察，新的thread history/detail和mutation明确`session_deletion_pending`。删除排空期间不持gate，受许可旧lease可短时重取gate完成settlement，随后继续同一删除且不重新active或接受late callback
- **AND**target read/wait在建立`RemoteObservationRecord`前同样竞争gate；删除先行时无snapshot/baseline/subscription，observation先行时删除返回明确target-deleted或完成原冻结结果并等待lease terminal。catalog tombstone后普通resolver不可打开隔离节点，但source丢失response后可在恢复窗口内以同一source call和新授权定点取得相同terminal envelope；窗口结束才物理清理并明确`operation-retry-expired`，不因最长300秒wait遗失恢复事实或静默重开

#### Scenario: Web E2E 验证 copy 的 source snapshot 与删除竞态

- **WHEN**同一pytest模块用barrier分别让本地`full_rollout_copy`与board `materialize_thread_copy`停在source capture前/中间，在冻结revision后追加source Turn/view变化，并与source Session删除跑双顺序
- **THEN**target history只包含`SourceCopySnapshot`冻结的SQLite revision、JSONL committed offsets和detail manifest；删除先行时无可见target/child且board保持完整旧状态，shared guard先行时删除只等capture durable，释放后source可删除而copy不回读隔离节点。partial capture定点abort，captured marker到operation CAS崩溃继续同一snapshot，多member逐source capture且trace证明没有双gate、扫盘补齐、换revision或部分board

#### Scenario: Web E2E 验证 pinned retention 与 federated fork 拒绝

- **WHEN**同一pytest模块创建本地detached/pinned fork并让pinned claim与source删除竞争，随后在target committed前后重启、删除pinned target；另以federated Session link请求fork
- **THEN**pinned claim先于capture且唯一：删除先行使fork零可见副作用失败，claim先行使删除在整树catalog deleting前显示具体blocker，target恢复只激活原claim。target删除通过durable release saga释放source且任一崩溃点不同时持两端gate；federated fork明确拒绝且远端catalog/retention/context零副作用，send/read/wait仍按原合同成功

#### Scenario: Web E2E 验证 copy attachment claim 与 GC

- **WHEN**source Turn引用available workspace attachment，copy/board已提交attachment claim但target/child尚未发布，测试删除source并触发GC，且分别在claim preparing、owner_reserved、target publication后/claim committed前重启
- **THEN**workspace始终只有一个由相同digest/length标识的blob，claim在source owner释放后继续阻止GC；reserved owner在target Session/thread catalog发布前不可访问，发布后DOM/history和授权工具可读同一正文。恢复只按operation中的claim ID继续/释放，不产生悬空ref、重复blob、提前授权或串用其它copy claim；required缺失使整个copy/board不发布，非required unavailable历史ref明确显示不可用

#### Scenario: Web E2E 验证 target 删除与 attachment finalization

- **WHEN**target/board已经发布但claim仍为owner_reserved，同一pytest模块让copy finalizer与target/coordinator Session删除竞争同一gate，并在两个顺序的settlement中途重启
- **THEN**finalizer先行时先完成全部claim commit并把settlement/board record分别推进成功终态`committed|published`，删除再正常释放owner；删除先行时排空流程释放reserved claim/ref，或验证committed claim归属后持久释放其target/child owner ref，再把record分别推进删除终态`target_deleted|coordinator_deleted`，恢复不重建或保留owner、不复活target/child、不隔离含非终态record的节点且不留下GC泄漏
