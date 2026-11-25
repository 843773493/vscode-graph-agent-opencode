# checkpoint-history-loading Specification

2026-11-05 发布边界：本轮只发布已验收的通用上下文／存储、精确 Thread、来源激活、fork 和恢复合同。团队／成员／委派及跨 Session 协作的新设计和专项场景仅作延期参考，不构成本轮已实现能力；已有协作行为不在本次同步中删除或改变。

## Purpose
为精确 SessionThread 的时间线、诊断和模型恢复提供统一有界读取，使用格式3 canonical Item、SQLite context view 与冻结 selection 支持完整 Turn、独立内部 execution 和不透明游标。
## Requirements
### Requirement: Web 历史以 rollout JSONL 和 SQLite 为唯一来源

系统 SHALL 直接从当前格式3会话 `index.sqlite` 和 `rollout.jsonl` 读取 Web 历史摘要、详情、Turn 边界和 cursor 定位。格式3 item-line 是正常生产 history 的唯一事实源；格式1／2的旧记录退出支持，不能进入 `/bootstrap`、`/history` 或正常 checkpoint/provider 路径。Trace、日志和旧 turn projection MUST NOT 作为历史数据源，也不得在 rollout 缺失时静默回退。

#### Scenario: 当前格式fixture从rollout读取

- **WHEN** 测试数据使用当前格式canonical Item 写入 rollout
- **THEN** 历史 API 从当前SQLite view/index 和 JSONL offset 组装完整 Turn，且不读取 Trace、旧 projection 或 v1 artifact 作为事实来源

#### Scenario: 正常 history 拒绝 v1 artifact

- **WHEN** `/bootstrap`、`/history` 或正常 checkpoint/history service 打开 `rollout_format_version=1` 的 session
- **THEN** 系统返回 `unsupported_rollout_format_version`，不调用 v1 reader、不生成旧格式内容投影、不创建当前格式view，也不把该 session 当作可运行的 history/provider/checkpoint 输入

#### Scenario: 旧 Trace 不能伪造历史

- **WHEN** 会话只有旧 Trace 文件而没有有效 rollout
- **THEN** 历史 API 返回可诊断的 rollout 缺失或损坏错误，不返回旧投影中的 Turn

#### Scenario: 当前工具记录从rollout读取

- **WHEN** 测试经 RolloutCheckpointSaver 正常入口提交用户 root、tool_call、tool_result 和最终 assistant_output canonical items
- **THEN** history API 按当前SQLite view/membership 与 JSONL offset 组装所选可见完整 Turn，Trace 文件是否存在不影响结果

#### Scenario: v2 fixture 从 rollout 读取
- **WHEN** 旧格式2 fixture 被交给当前历史入口
- **THEN** 明确拒绝未支持的 canonical 版本并保留原件；当前格式fixture必须通过真实格式3 writer 构造，不只改版本号。

#### Scenario: v2 工具记录从 rollout 读取
- **WHEN** 旧格式2工具记录被交给当前 history/provider 入口
- **THEN** 拒绝旧记录，不沿旧复合数组生成内容；当前工具记录各自引用真实格式3 call/result Item，不依赖 Trace。

### Requirement: 历史加载支持多种方向和不透明 cursor

系统 SHALL 支持从最早位置、从最新位置、cursor 之前、cursor 之后以及以 cursor 为中心加载历史。cursor MUST 绑定 rollout、view、branch、projection epoch 和逻辑位置。

#### Scenario: 从头加载

- **WHEN** 调用方请求从历史起点加载若干 Turn
- **THEN** 系统根据 SQLite view range 返回最早的完整 Turn，并提供继续向后的 cursor 或明确表示没有更多数据

#### Scenario: 从尾加载

- **WHEN** 调用方请求最新若干 Turn
- **THEN** 系统通过 active view 和 turns 索引返回最新 Turn，不读取更早完整 checkpoint

#### Scenario: 从中间向前加载

- **WHEN** 调用方携带历史 cursor 请求 cursor 之前的 Turn
- **THEN** 系统返回 cursor 之前的完整 Turn，不重复、不拆分 anchor Turn

#### Scenario: 中心展开

- **WHEN** 调用方请求以某个历史位置为中心加载前后范围
- **THEN** 系统返回锚点两侧完整 Turn，并分别返回两侧继续加载状态

#### Scenario: 游标前加载一个 Turn

- **WHEN** 调用方请求 cursor 之前的 1 个 Turn
- **THEN** 系统只返回 cursor 前最近的完整 Turn，不返回 cursor 所在 Turn 或更早 Turn，并提供继续向前 cursor

#### Scenario: 游标上下各加载一个 Turn

- **WHEN** 调用方请求以 cursor 为中心向前和向后各加载 1 个 Turn
- **THEN** 系统返回锚点两侧各 1 个完整 Turn，不重复锚点或拆分 Turn，并分别提供两侧状态

#### Scenario: 游标前加载五个 Turn

- **WHEN** 调用方请求 cursor 之前的 5 个 Turn
- **THEN** 系统按历史顺序返回最多 5 个完整 Turn，并在预算耗尽时返回有界结果和继续 cursor

### Requirement: Turn 是默认分页边界

历史加载必须使用统一的 `Turn.status` 闭合集合 `open | active | completed | completed_empty | interrupted | cancelled | failed | unknown`；其中 `completed_empty` 是唯一的无 canonical output 正常终态名称。普通 `cancelled` 及 fork 导入的全部历史 Turn（无论原 status）只能作为不可运行历史读取，不能由历史加载触发 `resume_turn` 或 `dispatch_replay`；对原 Turn 的这两种请求必须由业务层显式返回 `turn_not_resumable`。导入保留原 status、final item 和业务 hash，以目标本地冻结 namespace binding 核验历史归属，不把原历史批量改成 cancelled，不为此取消 source 执行。若需要重新执行，只能由独立的 `replay_as_new_turn` 创建新的 Turn/root/acceptance/initial execution，在 active view 登记新的 view-local `logical_turn_ordinal`，source history 可以作为上下文前缀但 source Turn/root 不能成为新 Turn 的 root，并记录 `replay_of_turn_id`，不能把新 Turn 创建操作混入原 Turn 的错误响应。

系统 SHALL 以完整 Turn 作为默认加载和分页单位。Turn 必须由 `TurnRecord.root_input_item_id` 表示其真实用户输入起点；用户输入、合并 steering Job、工具活动和最终状态 MUST 保持在同一个 Turn 中。历史顺序使用当前 context view 的 `logical_turn_ordinal`，不使用会跨 branch/view 保持不变的全局 `turn_ordinal` 重排；root lookup 必须沿 view fork lineage 解析到同一个 TurnRecord。`pending_next_turn` runtime notice、request-only prompt、缓存保持型 source delta 和其它内部消息不得创建 Turn、占用 root 或把一个 Turn 拆成独立分页项；overlay 正文只通过按需的 assembly/provenance 详情查看；默认历史允许返回不含正文的上下文存在性标记，标记关联实际请求边界且不占用 Turn、用户输入或活动计数。

存在性标记的展示锚点 SHALL 与真实用户 root 分离：首次派发的 System Prompt／初始内部用户角色注入标记位于 transcript 起点、第一条真实用户消息之前；打断提醒实际被后续请求使用后的标记位于后续新用户消息之前。标记属于原历史页的 metadata，不创建更早的伪 Turn，不因提醒产生于旧 execution 就归回旧轮次，也不只挂在 assistant response 内部。实时与纯历史加载遵守同一锚点；未派发使用的 pending 提醒不冒充已使用上下文。

完整 Turn 分页指保留真实 Turn 分组边界，不要求重新展示选定 view 已隐藏的成员。某 Turn 仍有可见 canonical 成员时，root 可仅保留为 hidden backing identity/排序坐标，summary/detail 只返回选中可见正文；不得因 root hidden 遗漏保留的回答，也不得为了分页把 root 加回可见历史或模型输入。普通完整 view 的原读取行为保持不变；具体分组和坐标规则复用 `session-turn-history`，不增加第二条 reader。

默认分页完整性 SHALL 指真实 Turn envelope 与成员归属不被拆散，不代表一页必须交付该 Turn 的所有正文。过程内部的 activity_outline 和选中 Item 正文可以有界续读，响应明确所覆盖成员与未加载范围；不产生伪 Turn、不以详情片段替换整个 Turn，不影响主时间线分页坐标。

#### Scenario: 合并 steering 消息

- **WHEN** 多个 steering 消息被一次 Job 合并执行
- **THEN** 历史返回一个完整执行 Turn，并保留 root input、源消息和合并 Job 的身份信息

#### Scenario: 工具调用中的 Turn

- **WHEN** 一个 Turn 包含多个工具调用和工具结果
- **THEN** 默认 summary 返回一个 Turn，工具数量和状态作为 Turn 内摘要

#### Scenario: pending runtime notice 位于新 Turn 之前

- **WHEN** 被中断 execution 产生 pending runtime notice，随后用户提交普通输入
- **THEN** 历史以新用户输入的 `root_input_item_id` 开始新 Turn；notice 通过 ambient/assembly relation 参与请求，不成为用户消息或 Turn root；实际派发使用后，默认 metadata 返回位于新用户消息上方的轻量标记，右键才读取对应冻结正文

#### Scenario: 缓存保持型 source delta 不污染历史分页

- **WHEN** `AGENTS.md` 在两个普通用户 Turn 之间从 revision A 变为 B，并追加了 A→B 的 ambient `runtime_notice` delta
- **THEN** 默认历史仍只按真实用户 root 返回原有 Turn，不新增一条用户消息或独立 Turn；默认页可返回该 delta 的轻量存在性标记；用户明确选择查看后才通过冻结 assembly/provenance reference 读取正文

#### Scenario: 大型 Turn 的内部详情分页
- **WHEN** 一个 Turn 的工具活动或中间文本超过详情单页预算
- **THEN** 主时间线仍展示同一个 Turn，内部轮廓与正文分片补读且保留既有成员，未加载内容不被当作已删除或完整交付

### Requirement: 加载内容可以按 include 策略选择

系统 SHALL 支持独立选择用户消息、可见 text、工具摘要、工具调用、工具结果、reasoning 摘要/详情、encrypted reasoning 元数据、内部消息和 metadata。`assistant_text` 不作为 canonical 事件类型，只能是 `assistant_output` Item 的统一内容投影；默认工具展示 MUST 只包含工具名称、状态、原结果tool_outcome及最小安全声明／执行关系，完整参数和结果正文必须显式请求。结果marker与可空执行摘要独立，不能因缺时序把completed解释为成功。

#### Scenario: 默认工具摘要

- **WHEN** 调用方未请求工具详情
- **THEN** 系统从 SQLite 安全工具投影返回工具名称、原结果marker、可得声明／执行摘要及有界错误摘要，不读取参数或结果正文；页外关联与view边界遵守session-turn-history的同一有界合同

#### Scenario: 请求完整工具结果

- **WHEN** 调用方明确请求工具结果并且通过服务端大小限制
- **THEN** 系统根据当前格式Item 的 SQLite source offset 读取对应 JSONL，并在预算耗尽时返回 bounded detail cursor；格式1／2的 source 原件保留，当前程序拒绝读取且不提供旧转换入口

#### Scenario: 隐藏内部消息

- **WHEN** 调用方未请求内部消息
- **THEN** 系统只返回公开可见内容，visibility 为 internal 的 runtime notice 不会被误当成用户消息或 Turn root

#### Scenario: 加载工具摘要和模型最终响应

- **WHEN** 选定 view 中 root 可见，且调用方选择用户消息、tool_summary 和模型最终响应
- **THEN** 每个完整 Turn 返回 root user input、工具名称与状态摘要和 `TurnRecord.final_item_id` 对应的最终 assistant projection，不返回工具参数或完整结果

#### Scenario: 加载工具调用、结果和模型最终响应

- **WHEN** 选定 view 中 root 可见，且调用方选择用户消息、tool_call、tool_result 和模型最终响应
- **THEN** 每个完整 Turn 返回用户 root、工具调用、对应工具结果和最终 assistant projection，并遵守详情大小限制

### Requirement: Gateway 默认历史加载使用固定窗口

系统 SHALL 复用 session-turn-history 的默认读取策略：无保存锚点时最新 1 个 Turn，有锚点时 around 前后默认各 4 个 Turn，默认仅 user 与 final_response；后续 before/after 使用所属 Gateway 配置的固定窗口。不得按滚动次数隐式切换 4／16／64 批次，复合 cursor 的推进阶段不改变该窗口合同。

#### Scenario: 首次进入会话

- **WHEN** Web 首次打开会话且没有保存锚点
- **THEN** 返回最新 1 个完整 Turn，工具活动按原详情入口展开

#### Scenario: 恢复锚点并继续双向读取

- **WHEN** Web 恢复保存的 Turn 锚点，随后向上或向下加载
- **THEN** 先按 around 取得前后固定窗口，再沿 before/after 继续，不重复锚点、不按滚动次数改变批次；在途请求不得重复触发

#### Scenario: 当前 Turn 立即加载工具详情

- **WHEN** 用户明确展开某个 Turn 的工具详情
- **THEN** 只补载该 Turn 的受限详情，其他 Turn 和所属 Gateway 的默认读取策略保持

#### Scenario: 配置由会话所属 Gateway 决定

- **WHEN** Gateway A 代理 Gateway B 的历史读取
- **THEN** B 的有效历史配置决定窗口和投影，A 只透传原结果与游标

### Requirement: 所有历史响应有服务端硬上限

系统 SHALL 对 Turn 数、消息数、JSONL record 数、响应字节数、单项字符数和详情批次设置硬上限。客户端传入更大参数不得绕过限制。

#### Scenario: 请求超过响应预算

- **WHEN** 请求范围超过服务端字节或字符预算
- **THEN** 系统返回已完成的有界结果、继续 cursor 和明确 truncated 状态

### Requirement: 历史读取使用单次 snapshot 和批量 I/O

系统 SHALL 为一次历史请求创建单个 read snapshot，执行页面级 SQLite 查询，并按 JSONL offset 分组读取命中的行。系统 MUST NOT 为每个 Turn 重复初始化、查询 offset 或打开文件。

#### Scenario: 128 Turn 页面加载

- **WHEN** 以配置的固定窗口或合法显式数量读取历史 Turn
- **THEN** 请求只校验一次 read 水位，批量获取全部 offset，不重放完整 checkpoint 或扫描未命中的 JSONL 前缀

### Requirement: 所有 rollout 数据访问使用唯一 RolloutCheckpointSaver

系统 SHALL 通过 `RolloutCheckpointSaver` 作为 checkpoint、Web history、fork context、Turn 状态和 `ContextRequestPlan`/`ContextAssemblySnapshot` 的唯一业务层入口。Saver MUST 在正常运行时只使用当前格式3 reader 解析已提交 SQLite view/range，验证 branch/view lineage，并提供 projection、detail、full 三种模式；未发布的格式1／2及其离线转换已退出支持，旧 reader、importer 和专用历史物化入口 MUST 删除；旧数据在写入前明确拒绝并保留原件。业务 service、middleware adapter、LangChain/Provider projector 只能消费 Saver 返回的已提交 plan/snapshot 与显式 runtime contribution，不得直接扫描 `RolloutStorage`、`AppendWriter` 或内部 context reader，也不得自行组合低层 SQLite 和 JSONL primitive。未 sealed 的内存 ledger 不是业务层可消费的恢复事实。

Saver 返回的 `ContextRequestPlan` 结构必须包含 `plan_id`、`plan_state`、可空的 `assembly_id`、`refs[]`（ContextRef registry）、`tool_set_refs[]`（ToolSetRef registry）、`contributions[]` 和唯一有序的 `selection[]`。`create_context_plan` 先在 owner-thread namespace 产生唯一 `plan_id` 和 `plan_state=unsealed`；在 Saver 成功 seal 前 `assembly_id=NULL`、`selection=[]`，未 seal plan 不是可恢复/可 dispatch 的 sealed assembly，且不存在 `ContextSelectionEntry` 或 `plan_ordinal`。ContextRef 在 draft 只按 `(session_id, thread_id)` 加 item/plan registry identity 解析，不携带 assembly binding；最终 request-only `detail_ref` 也不得在 draft 解析物理 assembly path。Saver 只有在 source/manifest 完整校验、必要 detail 已解析/物化后分配 thread-local `assembly_id`、生成 selection/ordinal 和 sealed detail manifest，并提交 `ContextAssemblySnapshot`；seal 成功后 plan 与 snapshot 的 `plan_id`/`assembly_id` 逐字段不可变且一对一。plan 创建幂等和 seal 幂等使用不同的 owner-thread-scoped keys，冲突分别返回 `plan-idempotency-conflict` 和 `assembly-idempotency-conflict`；空 selection 只有在已分配 assembly 的 sealed snapshot 中合法。

每个 `ContextSelectionEntry` 必须带 `assembly_id`、`plan_ordinal`、tagged-union `ref`、`selection_kind`、`included`、omission 时的 `omission_reason`、`loss[]`、visibility/protection/availability、base/delta role 和 overlay epoch。只有 `included=true` 的 entry 才强制 source revision、logical `content_length` 和恰一个 `content_hash`；其中 included=true 的 request-only entry 还必须带解析为 `{session_id,thread_id,assembly_id,detail_id}` 的最终 `detail_ref`，有 contribution-backed source 时还必须带 `contribution_ordinal`。`included=false` 只允许 optional omission，仍保留 tagged source ref、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 source identity；source revision、length、hash token、detail_ref、contribution_ordinal 可以为 null/未分配，已知 metadata 必须逐字段等于对应 manifest。`selection_kind=canonical_history|request_only|overlay_base|overlay_delta` 时 `ref` 恰为一个 `ContextRef{session_id,thread_id,ref_type,ref_id}`；`selection_kind=tool_set` 时 `ref` 恰为一个独立 `ToolSetRef{ref_type=tool_set,ref_id=tool_set_snapshot_id}`，不把 `tool_set` 解释为 ContextRef 类型。ContextRef 的 assembly binding 不在 ref 本体中，而在 sealed entry/manifest 的 `assembly_id` 中；draft request-only ref 不解析 assembly detail，omitted request-only entry 不拥有最终 `detail_ref`。included entry 的 length/hash/source 一律按权威矩阵从对应 manifest 读取，不得从当前 source/registry 回退；被 selection 绑定且 included 的 `ContextContribution.request_only` 必须为 `true`，omitted entry 不分配 `contribution_ordinal`，ToolSetRef 永不绑定 contribution ordinal。selection 中存在的字段必须与对应 registry/detail manifest 完全一致；缺项、重复、union 类型与 selection_kind 不匹配或顺序绑定不一致返回 `plan-order-integrity`，canonical/tool-set source 不一致返回 `source-mismatch`，request-only detail 不可用/不一致返回 `detail-unavailable`。`refs[]`、`tool_set_refs[]` 与 `contributions[]` 只是 registry，selection 才是 history、LangChain 和 native Provider 共同消费的顺序权威；Provider 只把 included ToolSetRef 投影到 tools/tool-config，history 只保留策略允许的 ToolSetRef metadata，不将工具定义拼成 canonical message；optional omitted canonical/request-only/tool_set entry 在 restore/history 中只保留 omission/loss，分别跳过正文、detail 和工具定义，不从当前 source/registry 回退。

矩阵明细见 `itemized-rollout-context` 的 requirement「selection_kind 与 ref_type 必须使用唯一兼容矩阵」；本节不再复述。history/restore 仍 SHALL 在 restore 之前按该权威矩阵校验，矩阵外组合返回 `plan-order-integrity`，不得改变 entry 生命周期。

included/omitted 各 selection 行的字段归属见权威矩阵，本节不复述。history/restore 的 capability 义务是：included 且 contribution-backed 的 request-only/overlay entry 必须以非空 `contribution_id` 在同一 `(session_id, thread_id, plan_id)` 唯一解析到该 entry 的 `contribution_manifest`，再由 manifest 读取正文/detail 并逐字段校验；普通非 contribution-backed 的 included request-only 只按同 assembly 的 `detail_ref`/source manifest 读取和校验，不要求 `contribution_id`/`contribution_ordinal`。restore/history/projector 不得根据 ref_id、detail_ref、hash 或 ordinal 搜索/猜测另一 contribution。

其中 included `canonical_history` ref 的 `content_length` 必须等于 `item_catalog.payload_length`，included request-only/overlay ref 的 `content_length` 必须来自同一 assembly 的 sealed detail/contribution source manifest，included `tool_set` ref 的 `content_length` 必须来自同一 plan/assembly 的 ToolSetSnapshot manifest；三者都不能用 JSONL line offset/length、wire message 长度或当前文件长度替代。included request-only contribution 的 `content_hash` 必须覆盖 `{ "contribution_kind": <kind>, "body": <typed body> }` 的 JCS bytes；敏感正文由同一 detail manifest 保存并校验内部 content_hash；默认外部投影按访问策略隐藏正文与该 hash。optional omitted entry 不要求正文完整性字段，且不得由 history 从旧 message、当前 source 文件或 refs registry 重新拼接；history/restore 只展示或标记 omission/loss。included selection manifest 不完整时返回 `source-mismatch`/`detail-unavailable`，required source 的 omitted/unavailable detail 直接拒绝 seal/dispatch。

history/restore 在消费 selection 前必须校验每个 ToolSetRef 的 `tool_set_snapshot_id`、`source_revision`、`content_length`、`content_hash`、`tool_set_schema`/`tool_set_schema_version`、`tool_policy_version` 与同一 `tool_set_manifest[]` 的 `tool_policy` 逐字段相等，并把这些 manifest identity 纳入 `context-plan-hash:v2`。schema/config、policy 或 source revision 变化不得静默复用旧 plan；应报告 `plan-hash-mismatch`/`source-mismatch`，不可用 manifest 报告 `detail-unavailable`。`ContextContribution.contribution_kind` 不允许 `tool_set`，不保留旧迁移 quarantine 例外；history 不把 contribution 或 ToolSetRef 伪造成 message。

#### Scenario: Saver 提供已提交 selection 给历史读取

- **WHEN** history/restore 请求一个已 sealed 的 ContextAssemblySnapshot
- **THEN** 读取面使用 Saver 返回的同一 `selection[{plan_ordinal, ref}]` 和 manifest，不从低层 storage、当前 source 或 registry 重建顺序

#### Scenario: 复杂 view 仍使用同一入口

- **WHEN** active view 包含多级 rewind、replay、fork 或 compaction lineage 与显式 membership
- **THEN** Saver 仍使用同一当前格式内部reader 和已提交 SQLite context view/selection 解析流程，不切换到旧 Trace、v1 reader 或 rollout 前缀扫描

### Requirement: 历史读取性能可验收

系统 SHALL 提供包含 128 Turn、普通 reasoning、encrypted reasoning、tool_call/tool_result 和 finalization 的确定性 fixture。真实 8011 链路 MUST 测量 bootstrap、4/16/64 向前加载和当前 Turn 详情重载，并记录 p50/p95；热路径 p95 目标为 100ms 内，稳定浏览器 prepend/锚点恢复 p95 目标为 150ms 内，首次冷 prepend 预算为 500ms 内。

#### Scenario: 混合 128 Turn 读路径

- **WHEN** 测试通过 8011 → Gateway → workspace backend 请求 bootstrap、4/16/64 向前加载和当前 Turn 详情
- **THEN** 每个请求使用一个 snapshot 和页面级批量读取，记录 p50/p95，并证明没有 materialize 全量 checkpoint

### Requirement: 前端历史定位不暴露 View 实现

系统 SHALL 让前端通过真实 `turn_id` 或 `item_id` 定位历史；无 Turn 内容保留真实 execution／Item 归属，不伪造 Turn。view、branch、checkpoint 和 message range 不成为客户端阅读锚点；后端提供的读取／操作 binding SHALL 按原合同原样回传和校验，不要求前端解释 view 图。Item 跳转不附加通用 Part ID，fork／rewind／replay 仍由原操作 owner 判断边界能力。

#### Scenario: 同一个 Turn 出现在多个 view
- **WHEN** append、rewind、fork 或 compaction 后打开稳定 Item 引用
- **THEN** 原 owner 核验当前 view 的真实成员、scope 和可见性后定位；客户端不维护 Item→多个 view 的第二事实源，不将不在所选 view 的 Item 冒充存在

#### Scenario: 内容定位不依赖 Turn 存在
- **WHEN** 用户点击 internal execution 中的 Item URI 或搜索结果
- **THEN** 原 Thread 的 around 入口有界加载该 Item，正常进入阅读位置控制器，不改开原文或等待不存在的 turn_id

### Requirement: checkpoint/history loading 必须显式选择 product thread

checkpoint restore、history loading、detail lookup 和 ContextRequestPlan materialization SHALL 以 `(session_id, thread_id)` 解析 owner。未指定 thread 的 Session-facing 产品入口只可从 Session catalog 解析 main thread；内部 API、subagent、retry、rewind 和 compaction 不得依赖该默认。LangGraph `checkpoint_ns` 只在已选定 thread 内继续限定 framework checkpoint，不得改变 owner selection。

#### Scenario: 相同 namespace 的不同 thread 恢复隔离

- **WHEN** 两个 SessionThread 均请求相同 `checkpoint_ns` 的 checkpoint
- **THEN** loader 仅从各自 thread node 读取相应 checkpoint/context view；不得因 namespace 相同返回或合并另一个 thread 的 messages、ToolSet 或 source state

### Requirement: checkpoint/history restore 必须复用 sealed resource activation snapshot

checkpoint restore、history loading、retry、rewind、compaction 和 exact replay SHALL 通过 `RolloutCheckpointSaver` 读取 assembly 已提交的 `ResourceActivationSnapshotRef` 与 `ResourceProvenanceRef`，不能调用 ResourceRegistry、Virtual Resource Resolver、resource loader 或 monitor provider 获取当前内容。`snapshot_kind=execution`时恢复完整原policy的default/全部overrides、校验其revision/hash并复用turn-bound binding，不得从选中binding反推policy或补默认值；`snapshot_kind=model_call`时必须恢复其`parent_execution_snapshot_id`、逐字节相同的turn-bound binding和该call自己的model-call-bound binding。loader不能把最新Registry generation或配置policy批量覆盖到历史assembly，也不能用一个boundary单值重建混合binding。

loader MUST 校验 resource ref 的 owner session/thread、assembly、activation ordinal、resource identity、revision、availability、length/hash marker 和显式 detail 读取权限（权限/归属合同，不要求加密）。`display_uri` 只用于安全展示和经授权的显式新解析，不是 restore lookup key；历史正文必须按 sealed `snapshot_ref|detail_ref` 定点读取。ref 缺失、owner 冲突、hash 不匹配或正文已被 retention 清理时返回 `source-mismatch`、`detail-unavailable` 或明确 loss，禁止回退文件系统、网络、内存源、当前 Registry 或旧 path 字段。

#### Scenario: rewind 后恢复旧 Turn 的资源版本

- **WHEN** rewind 将 active view 移到仍引用 Skill revision R1 的已提交 Turn，而 Registry 当前 revision 为 R3
- **THEN** history/restore 继续从 sealed ref 读取 R1；CSM 是否为后续新请求重新激活 R3 属于新的 activation/assembly，不改写 R1、不把旧 ref 指向当前 URI

#### Scenario: 当前 locator 不可用

- **WHEN** 资源的绝对路径、网络 endpoint 或内存 key 已不存在，但 sealed body 仍在 retention 范围内
- **THEN** loader 不解析当前 locator，直接从受保护 snapshot/detail 恢复；若 sealed body 也不可用则显式返回 loss/error，而不是读取同名新资源或空内容

#### Scenario: request path 不做资源 I/O

- **WHEN** Saver 为 retry、resume 或后续 tool-loop model call materialize 已提交 plan
- **THEN** 资源选择只来自内存中已冻结 activation snapshot或持久 sealed refs，trace 证明没有额外 `stat`、scan、read、HTTP fetch 或 provider source lookup

### Requirement: selection_kind 与 ref_type 的兼容矩阵必须唯一登记且被历史读取消费

本 requirement MUST NOT 登记第二份 `selection_kind` 与 tagged-union `ref` 兼容矩阵；唯一权威登记是 `itemized-rollout-context` 的主规范 的 requirement「selection_kind 与 ref_type 必须使用唯一兼容矩阵」。history/restore SHALL 在 source lookup、detail 解析和 message/tool projection 之前按该权威矩阵校验 `ContextSelectionEntry.selection_kind` 与 tagged-union `ref`；included 与 omitted entry 都必须满足同一 tag/type 关系。矩阵外组合必须返回 `plan-order-integrity`，不得按历史 role、`ref_id` 或当前 registry 改派。

`ContextRef.ref_id` 仍是 canonical `item_id` 或 request-only 的稳定逻辑 alias（普通 request-only 通常等于其 `contribution_id`，亦可为 producer 声明 source alias；overlay 为 overlay alias）；普通 included request-only 若非 contribution-backed，只通过同 assembly 的 `detail_ref`/source manifest 读取正文；included 且 contribution-backed 的 request-only/overlay 才必须通过非空 `contribution_id` + `contribution_ordinal` 唯一读取同一 `(session_id, thread_id, plan_id)` 的 contribution manifest/body，并校验 detail、source revision、logical length、hash 和 ordinal，overlay 本身必须 contribution-backed。canonical/tool_set 的 contribution_id/ordinal/detail_ref 必须 NULL。omitted entry 仍必须保留矩阵规定的 tag/type，正文和 contribution/detail binding 可 NULL/未分配，已知 identity metadata 必须一致；required omission/detail failure 不得恢复为部分成功。history/restore 与 LangChain/native Provider projector 对 omitted entry 只保留 omission/loss，并分别跳过 canonical message、request-only detail/body、overlay 应用和 tool definition。

#### Scenario: history 在 source lookup 前拒绝 union mismatch

- **WHEN** `canonical_history` 使用 request-only、`request_only|overlay_base|overlay_delta` 使用 canonical_item，或 `tool_set` 使用非 ToolSetRef
- **THEN** history/restore 返回 `plan-order-integrity`，不读取 item/contribution/detail/tool registry，不生成替代 message/tool projection

#### Scenario: history/restore 保留 omitted entry 的矩阵 tag

- **WHEN** optional source 在 sealed selection 中为 `included=false`
- **THEN** 保留对应 tag/type、assembly/plan ordinal、omission/loss/availability 和可得 identity；omitted canonical_history/tool_set 的 `contribution_id`、`contribution_ordinal`、`detail_ref` 必须为 NULL；omitted request-only/overlay 可保留已有且与同一 manifest 一致的 `contribution_id`，但不得新分配 contribution_id/ordinal 或读取正文/detail，没有既有映射则为 NULL；其它正文 length/hash、detail_ref、contribution_ordinal 可不分配
- **AND** history/restore 只展示 omission/loss metadata，不从当前 source 或 registry 回退，也不生成空 message、detail 或 tools

#### Scenario: 默认 Web projection

- **WHEN** Web 请求最新 Turn 或向前加载历史摘要
- **THEN** RolloutCheckpointSaver 使用内部 reader 的 projection 模式，不调用 full materialize，不构造完整 checkpoint messages 列表

#### Scenario: LangGraph 恢复 full

- **WHEN** LangGraph saver 或 context fork 需要可执行消息列表
- **THEN** Saver 从 active canonical view 构造 ContextRequestPlan，再返回按目标能力投影的 BaseMessage，不把 request-only prompt 或 pending notice 伪造成历史 user message

#### Scenario: history 与 request projector 共享 selection/order

- **WHEN** history projection、LangChain restore 和 native Provider request 读取同一 active view 或同一 source overlay epoch
- **THEN** 三者都消费 Saver 已提交 snapshot 的 `selection[{plan_ordinal, ref}]`；request-only contribution 使用持久 `contribution_ordinal` 与 canonical/base→delta 顺序，ToolSetRef 使用同一 `plan_ordinal` 进入 Provider tools/tool-config，history 仅保留受策略控制的 metadata；不按 `created_at`、`contribution_id`、物理邻接或 projector 本地 prepend 规则重排
- **AND** included canonical ref/contribution 正文必须与 sealed `source_revision`、逻辑 `content_length` 和 `content_hash` 匹配；optional omitted entry 只校验其 tagged ref、`plan_ordinal`、omission/loss/availability 及可得 identity metadata，不读取正文或 detail；缺失、覆盖或错误 source 返回 `source-mismatch`/`detail-unavailable`，不得返回看似完整的部分 request

#### Scenario: history/restore 保留 optional omission

- **WHEN** sealed snapshot 中存在 `included=false` 的 optional canonical、request-only 或 tool-set entry
- **THEN** history 与 restore 保留该 entry 的 tagged ref、`plan_ordinal`、`omission_reason`、`loss`、`availability` 和可得 source identity，但不生成 canonical message、request-only message/detail 或 Provider tool definition
- **AND** projector 不从当前 registry/source 回退、不创建空值；如果同一 source 在该 assembly 中是 required，则 seal/restore 返回 `source-mismatch` 或 `detail-unavailable`，不得将 omission 视为成功

#### Scenario: 非法 view 所有模式统一失败

- **WHEN** SQLite view range、item membership 或 Turn root 缺失、越界或成环
- **THEN** projection、detail 和 full 都返回明确 context view 错误，不返回部分结果或按最大 sequence 猜测边界

#### Scenario: 旁路读取被禁止

- **WHEN** 业务 service 或 projector 试图直接读取 RolloutStorage、AppendWriter 或内部 context reader 以构造 ContextRequestPlan
- **THEN** 系统拒绝该旁路访问；只有 RolloutCheckpointSaver 返回的已提交 view/plan/snapshot 可以进入编译与请求投影

### Requirement: 内部 execution history 由持久 admission 独立加载

历史 loader MUST 从目标 `(session_id, thread_id)` 的持久 `ExecutionAdmission`、execution owner state 与 committed canonical items 加载内部 execution history。无 cursor 时，有效 include 由请求中明确提供的集合决定；未提供时，tail 使用配置 `initial_include`，before/after/around 使用 `anchor_include`。两套内置默认集合各加入 `internal`，并保留原消息/summary 选择；显式配置覆盖按实际集合执行，不再新增 `default_include`。continuation 未提供 include 时沿用 token 冻结的有效集合，显式提供时必须相同；不得重新按方向或当前配置计算默认。只有有效 include 包含 `internal` 时，响应才填充顶层 `internal_executions[]`。Web 的默认集合保留原 messages/summary 并包含 `internal`；明确排除时不得查询后再合并、不得把 internal execution 塞进普通 Turn summary/detail，也不得从 source completed Turn 或邻接项推断归属。

`internal_executions[]` MUST 按 execution owner 身份返回 standalone history envelope；每项不得要求 `TurnDetailDTO`、`turn_id`、root 或 final pointer。`RuntimeNoticePayload` 的 history projection 只读取 `display_part`，Provider/checkpoint 消费的 `prompt` 不得出现在 Web history。内部输出仍按该 `execution_id` 的 committed item identity/order 投影，不挂到 source Turn；普通 Turn 的分页、`item_count`、elapsed 和 `final_response` 不因内部 execution 改变。

internal admission MUST 在同一权威事务冻结真实 `source_branch_id` 与 `source_view_id`；幂等重入复用原绑定，不能改绑当前分支。其 notice 和同 execution 的 response items MUST 在既有 `context_view_items` 中明确登记为 `source_kind=display_only`；该 membership 是当前视图中可展示成员的唯一事实。checkpoint 后继、rewind 和 fork 必须按选定源成员显式继承或裁切，fork 保留历史local ID并核目标本地snapshot归属binding；不得只凭 admission 的源 view、物理 sequence 范围或时间猜测当前成员，也不得新增第二 context tree。Provider 的 canonical history selection 只消费 `source_kind=canonical`，不从全局 catalog 补入 pending notice；当前 internal execution 的私有 prompt 及其自身已提交输出仍通过 admission 绑定的执行请求恢复路径消费，工具调用与结果保持协议闭合，不因展示成员隔离而丢失。

#### Scenario: 明确排除内部 execution

- **WHEN** history request 明确提供不包含 `internal` 的 include 集合
- **THEN** 响应不填充 `internal_executions[]`，`pending_next_turn` report-back 不成为独立 Turn 或普通 Turn 的 response part

#### Scenario: 显式加载内部 execution history

- **WHEN** include 显式请求 `internal` 且目标 thread 存在已持久 internal admission
- **THEN** loader 从该 admission 绑定的 execution 与 committed item 坐标返回独立 `internal_executions[]`；runtime notice 的 history projection 只使用安全 `display_part`，不得返回私有 prompt 或 branch result；默认 Web 只展示安全固定文案和标签，不渲染来源 ID。模型 prompt、控制 ID 和 source branch 正文不作为展示内容
- **AND** 输出不会被并入 source completed Turn，也不会增加任何 Turn 的 item count 或 elapsed 统计

#### Scenario: 分支切换不吸收其它分支的内部记录

- **WHEN** 两个分支分别有 internal execution，随后读取一个选定视图或执行 rewind/fork
- **THEN** 只返回该视图明确登记或继承的 display-only 成员；Provider 不因全局物理序号补入另一分支的 pending prompt，目标 internal dispatch 仍消费自己的 admission 输入

### Requirement: 同一历史游标有界推进 Turn 与内部 execution

系统 SHALL 在现有 history API 使用唯一 v3 opaque cursor，同时保存 Turn stream 的 exclusive `logical_turn_ordinal` 和 internal stream 的 exclusive view membership `logical_item_ordinal`（0 为合法坐标）。internal stream 按当前视图中显式可见、属于具有唯一 display notice 的 internal admission 的 canonical notice/response record 分页，单页最多 256 条 canonical record；初始 seed/reference admission 没有 notice 时不产生展示项。视图成员关系必须由持久 owner 明确登记，不得从物理邻接、时间或 metadata 推断。

v3 token MUST 绑定 `session_id`、`thread_id`、`checkpoint_ns`、`rollout_id`、`projection_epoch`、`view_id`、`history_view_revision`、首次同一 snapshot 的 `logical_item_read_upper_bound` 与 Turn ordinal 读上界、固定 `direction`、递增 `stage`、排序规范化的有效 include 集合、初始查询模式，以及 around 初始 anchor 类型、`anchor_turn_id` 或 `anchor_item_id`、同 snapshot 解析的成员坐标与 before/after window；同时保存可空的 `turn_ordinal`、`logical_item_ordinal`。continuation 延续初始查询模式与窗口，不能替换过滤条件、anchor 或窗口；请求省略这些参数时使用 token 冻结值，显式提供且不一致时返回 invalid cursor。未请求的 lane 不查询且明确由冻结 include 标为禁用；已启用 lane 的 anchor 为 NULL 只表示该方向真实耗尽，后续不得从头再读，也不能改 include 复活。两路必须在同一个 `RolloutReadSnapshot` 和 resolved view 中进行有界 keyset 查询；不得全量读取后截断。旧v1／v2 cursor以 invalid cursor拒绝，失效详情沿原当前格式入口重新获取；不保留兼容解码或将旧Part坐标当Item顺序。owner/direction 不符返回 invalid cursor，epoch/view/history_view_revision 漂移返回 stale cursor。首次 `logical_item_read_upper_bound` 必须取同一 snapshot 中选定 view 全部可见 `context_view_items.logical_item_ordinal` 的最大值，canonical 与 display-only 成员共用这一坐标 fence；只有没有可见 membership 时才为 NULL，没有 eligible internal record 不等于上界为 NULL。internal reader 仍以 admission 与 display-only membership 判定成员，物理 `item_sequence` 只定位 canonical item，不能决定分页顺序；后续各 lane 查询不得超过这些上界，上界只定位不推 membership。不新建第二历史快照表，也不以活动 view ID 未变为由混入后续追加或终态修订。

上述双 lane 合同适用于主时间线分页；独立活动轮廓使用同一 codec 的 query_mode=activity 冻结单个 process_ref 与成员续点，不推进主时间线双 lane 游标。它不是 around/window 或 execution 默认查询的别名，不能互换 token；具体首次 tail、before 续读及 include／选择器约束沿 session-turn-history 的活动轮廓合同。

每页仅投影本页命中的 response records，并从同一 snapshot 批量关联其 execution admission 和唯一安全 display notice；同一 execution 可跨页出现。Web MUST 按 `execution_id` 合并同一 entry，按稳定 canonical item identity 去重，不按正文去重；同一 entry 的 committed outputs 保持其权威成员顺序，standalone notice 与完整 Turn 的合并顺序复用 `session-turn-history`「Internal execution history 使用独立 envelope 和 typed display coordinate」规定的同 view membership ordinal，不使用活动统计范围、物理邻接或时间；重复关联的 display notice 不得重影。Turn 预算与统计保持原语义；`has_more` 由两路实际剩余记录的并集决定，两路耗尽时游标为空。

`around` SHALL 保留现有 `anchor_turn_id` 并在同一入口支持互斥的 `anchor_item_id`。Item anchor 先在同一 snapshot 解析其当前可见 membership 与真实 owner：普通 Turn 成员使用所属 Turn window，internal 成员使用其逻辑坐标附近的有界窗口，并保留窗口两侧复合 continuation；不得从尾页逐页扫描或一次加载整个 execution。storage 从选中窗口的显式成员解析 canonical 边界，只把边界作为查询坐标，不作为 membership 判据。around 的 canonical 窗口边界不能裁切到 internal-only 上界；internal 全在窗口前、窗口后或完全为空时，仍以同 view 坐标 fence 判定两侧续读。窗口内超过 256 条 internal record 时，后续 cursor 继续剩余 record，Turn anchor 保持已返回窗口的边界；空 internal 窗口也必须检查窗口两侧是否尚有可读记录。首尾没有 Turn 的 internal record 和零 Turn view 必须可达。

#### Scenario: 零 Turn 的内部历史仍可续读

- **WHEN** 目标视图没有用户 Turn，但存在超过 256 条可见 internal canonical record
- **THEN** head/tail 返回有界 internal-only 页面和相应 continuation cursor，逐页遍历全部记录而不合成 Turn、不重复耗尽的 Turn stream

#### Scenario: 单次内部 execution 的输出跨页

- **WHEN** 一个 internal execution 的 canonical notice、assistant、reasoning 或 tool records 超过单页预算
- **THEN** 同一 logical_item_ordinal lane 继续其后续 records，每页关联同一 admission/display；前端合并后每个真实 Item 只出现一次，不静默截断输出、不引入第二 API 或 execution 专用分页 phase

#### Scenario: around 窗口的密集内部记录可继续

- **WHEN** anchor Turn 两侧窗口内存在超过 256 条 internal record，或窗口外仍有无 Turn 的 notice
- **THEN** before/after composite cursor 保留真实 continuation；Turn 与 internal anchors 分别推进，不重返已加载 Turn、不把截断窗口标为耗尽

#### Scenario: 游标不能跨 owner 或视图复用

- **WHEN** cursor 被用于其它 thread、namespace、方向，或原视图/epoch 已变化
- **THEN** loader 返回对应 invalid/stale cursor 错误，不混入另一 owner 或新视图的记录

#### Scenario: continuation 保留原 include 与查询窗口

- **WHEN** tail 或 around 返回 cursor 后，调用方省略 include 继续读，或尝试更换 include/around anchor/window
- **THEN** 省略时沿用 token 冻结条件，不因 continuation 方向改变而套用另一套默认；显式不一致时拒绝，不复活已禁用或耗尽 lane

#### Scenario: 活动视图原位修改使旧 cursor 失效

- **WHEN** 首页返回后，在同一 view/epoch 追加 item、更新 execution 终态或进行会改变 history_view_revision 的控制操作
- **THEN** continuation 以 stale cursor 明确拒绝，不能把新 revision 与原页混合；未变化时从首次 snapshot 冻结上界内续读

#### Scenario: Item around continuation 固定真实锚点

- **WHEN** 以无 Turn 的 Item 发起 around 后取得 continuation，随后请求省略锚点或改用其它 Item／Turn
- **THEN** 省略时延续原冻结成员坐标和窗口，显式更换时明确拒绝；两路保持原上界、耗尽语义及当前快照版本校验

### Requirement: 正常历史 reader 只服务当前语义 Item 格式

正常history、checkpoint、Provider与fork入口 SHALL 同时核验原metadata和JSONL envelope版本，只接受已完整提交的格式3语义Item及其目标SQLite结构。旧格式、未知版本、版本不一致或转换未完成 SHALL 按原错误出口明确拒绝，保留原件，不开启旧reader、Part兼容解析、Trace回退或即时转换。

按用户确认退出未发布的v1→v2转换支持，删除旧转换及专用入口／测试；保留旧数据原件，不在普通查询中补链。其它有序SQLite结构迁移保持原owner和既有合同，本次退出不授权改写真实Home。

#### Scenario: 当前格式文本历史
- **WHEN** history读取格式3的assistant_output Item
- **THEN** 按Item身份和view成员关系返回安全文本投影，缺正文只沿原locator补读，不通过Part reader解释

#### Scenario: 旧格式或未完成转换
- **WHEN** 元数据／JSONL为旧版、不一致，或目标转换尚未发布
- **THEN** 明确报告格式／结构不匹配或转换未完成，原文件保留，不创建可运行的半成品view，不自动重写数据

#### Scenario: 旧转换命令不再作为修复建议
- **WHEN** 当前程序拒绝旧格式来源
- **THEN** 明确说明不支持转换并保留原件，不提示运行已删除的命令，不启动旧 reader 或 staging

### Requirement: 历史与实时共享 Item 内容投影

系统 SHALL 为历史 summary、detail 和实时收敛提供同一 Item 内容投影。已提交内容 MUST 由真实 `item_id` 唯一定位，顺序使用所选 view 的逻辑 Item membership，保留原 owner、Turn／execution、ModelCall、状态、业务时间、工具调用关系及可用性；不再通过 `part_id`／`part_ordinal` 二次标识或排序。投影的摘要、正文和受保护存在性 SHALL 保持同一 Item 身份，缺失详情明确为未加载，不把未加载当作空正文。

公开内容数组 SHALL 统一使用 Item 名称和字段，旧 Part 内容数组及同义属性 MUST 在消费者迁齐后删除。非内容的过程标题、上下文标记等仍使用其原来源，不为满足 Item 投影伪造 canonical 内容。

工具的声明分组、实际执行状态和可比较的attempt起止证据 SHALL 在实时与历史使用同一语义，具体持久与摘要合同见 session-turn-history「工具历史保留声明关系和可持久执行摘要」。刷新不得回读Trace／终态实时snapshot来恢复关系；ToolCall声明生成事件不得冒充ToolExecution的运行事件。普通单项elapsed和Turn耗时继续沿原合同，不作为工具运行时长或执行重叠证据。

原消息组的typed来源 SHALL 贯通writer、公共DTO／protobuf、实时提交收敛与历史摘要。draft阶段只使用已明确的原调用来源和顺序，消息组尚未冻结时不猜size／ordinal；提交时沿真实来源关联接入已冻结的成员信息。重连沿原snapshot／补读合同取得同一声明关系，不要求客户端保存原tool_calls数组，不另开连接或建立批次缓存。

公开Item来源 SHALL 以可空的source.message_group传递原message_group_id、ordinal和size；无显式组时不构造对象。该显式组与完整owner、ModelCall共同构成同批声明证据，不证明实际并行。未提交调用可沿事件信封model_call_id和可得的provider_call_index恢复原来源顺序，快照保留相同信息；仅有这些未提交来源字段不足以确认同批，不能代替已提交Item身份，也不能证明执行关系。

#### Scenario: 相同内容与不同 include
- **WHEN** 同一 reasoning 或工具 Item 先返回摘要，后返回授权详情
- **THEN** 两次使用相同 item_id、顺序和来源；include 只改变可见载荷，不生成第二个业务身份，不暴露密文或未请求的参数／结果正文

#### Scenario: 历史不依赖实时事件
- **WHEN** 刷新后只有 canonical 历史，没有任何实时 delta
- **THEN** 原 Item 投影足以显示有序内容和未加载状态，不回退到 assistant_text／thinking_blocks／Trace 拼接另一份时间线

#### Scenario: 非文本输出摘要与定点详情
- **WHEN** 最终回答或中间过程包含独立图片、音视频或允许公开的未知结构块
- **THEN** 默认历史以同一Item身份的content投影交付类型及未加载状态，不因没有text而丢弃；SQLite的output_block_type仅镜像类型，不保存正文或媒体引用
- **AND** 显式补读沿原item_ids、view／read fence及预算取得单块原content，保留final角色和顺序；JSON、URL和base64不可截断为损坏内容，超限返回明确truncated及原身份，不启动无界补读或新增媒体存储
- **AND** 同binding迟到摘要不清空已加载content，媒体展示失败不改变Item存在性、Provider原载荷或已知结果

#### Scenario: 未提交实时工具事件
- **WHEN** 实时事件尚无持久 Item 或完整 Provider 调用 ID
- **THEN** 只用真实流来源的临时 draft 身份保位和关联；有明确关联的结果才能合并，无法关联时保留待关联状态，不伪造 tool_call_id／item_id 或将临时 ID 写入 canonical

#### Scenario: 真实提交替换 draft
- **WHEN** writer 提交语义 Item 并提供实际来源关系
- **THEN** 前端一次收敛到真实 Item 身份和后端统计；不按文本相等去重，不保留 Part→Item 运行时别名库，不凭猜测吞掉独立输出

#### Scenario: 工具声明分组跨实时提交和重连
- **WHEN** 同一ModelCall的两条原消息分别声明G1[A,B]和G2[C]，调用从draft收敛为已提交Item，客户端断连后通过原恢复入口取得快照及重放事件
- **THEN** 真实来源接续到各自调用Item、ModelCall和原消息组；仍只有A、B同批，不能以相同ModelCall将C并入G1。声明顺序不变，重复通知不增加调用行，执行摘要后到也不丢失分组；不依赖客户端先前保留的数组或从工具名称猜关系
- **AND** 即使B、C的真实执行区间重叠，恢复后也只按可比较的执行证据解释重叠，不合并原消息组、重排调用行或将Provider重建改为单个[A,B,C]数组

#### Scenario: 执行证据先于完整声明组
- **WHEN** 原调度器已启动一个具有明确来源的调用，前端收到其执行状态时，声明组尚未完成canonical发布
- **THEN** 沿原draft来源显示该调用的已知执行状态，不为等待消息组信息扣留更新或伪造size／ordinal；提交后接续到真实Item及显式组，原恢复入口保持相同来源关系，不以执行先后改变声明顺序

#### Scenario: 最终 carrier 引用已有思考
- **WHEN** 最终 checkpoint 的 typed 来源明确引用已经提交的 reasoning Item
- **THEN** 沿真实 Provider／ModelCall／block 关系复用原 Item，不再持久化或计数一次；文本相同但来源不同的思考仍分别保留

#### Scenario: queued root 与物理交错
- **WHEN** 下一 Turn 的 root 在前一 Turn 的工具结果完成前提交
- **THEN** 两轮按各自显式 membership 展示、计数和计时；物理 sequence 范围不代替逻辑顺序或归属

#### Scenario: 详情释放与再次补读
- **WHEN** 终态详情因正文／条数预算被淘汰或会话变为非活动
- **THEN** 使用同 revision、相同 Item 对应关系的后端权威 summary，不能在前端重新摘要；再次展开只沿 canonical 历史定点补读，不读取终态实时 snapshot

#### Scenario: 历史与实时驻留有界
- **WHEN** 乱序事件、长正文、跨会话详情或终态流缓存持续增长
- **THEN** 保持原驻留上限和截断／snapshot 恢复合同；不在启动时 materialize 全部终态流，不无界保留 draft 或详情

### Requirement: 活动清单和分页直接消费 Item 索引

历史 SHALL 沿原 Item 索引、snapshot 和有界 JSONL locator 提供轻量活动清单。原 `activity_outline` include 继续表达轻量读取意图，MUST NOT 要求复合 Item 的持久 Part 数组、`response_outline_json` 或第二份展示索引。主历史仍按原 Turn／internal execution 合同分页，活动详情以完整语义 Item 为最小成员，不携带页内 Part continuation。

服务 SHALL 保持原字节上限、最多256条 canonical record、快照 read fence 和 owner／revision／方向校验；单个 Item 的正文超预算时返回真实截断及已有详情入口，不拆成多个伪 Item 或自动读取完整大正文。组边界使用真实组首 Item；本页从组中部开始时只能沿原 owner 有界解析边界，不扫描全部前序历史。

#### Scenario: 无工具的混合消息跨页
- **WHEN** thinking、text、image 已分别成为 Item，活动页从中间 Item 开始
- **THEN** 页内 Item 只返回一次，组首引用保持真实来源，下一页按原 cursor 连续读取，不依赖 Part ordinal、不跳项或重复生成组

#### Scenario: 大型工具结果
- **WHEN** 一条工具结果超过正文硬预算
- **THEN** 摘要仍快速返回真实 Item 元数据，显式正文读取按原限制截断；不在默认摘要中解析整个结果或增加 Part 级无限续读

#### Scenario: 完整链路暖读
- **WHEN** 通过实际 Gateway 对固定128 Turn fixture 读取20次暖历史并进行浏览器展开
- **THEN** history p95≤200ms、网络展开p95≤500ms且单次≤1500ms、已有内容折叠／保位单次≤200ms；从发起操作计到完整响应解析／内容可读，保留未筛选原样本和 request_id，不能靠占位符或重试取绿
