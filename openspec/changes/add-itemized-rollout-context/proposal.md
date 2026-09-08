## Why

当前 `rollout.jsonl` 以完整 LangChain message 作为不可变记录，导致 provider 输出中的文本、reasoning、tool call、tool result 只能在一个较粗的 message 边界上持久化和恢复。middleware 又在每次请求中直接编辑 `ModelRequest` 的 messages/system message/tools，使实时上下文来源、持久化事实和最终 Provider 请求之间缺少可追踪的统一边界。

现在引入 item 化边界，可以让 rollout 保留语义事实，让 LangChain message 和各 provider 的请求格式都成为可重建的投影，同时保持实时流增量与终态持久化的职责分离。

## What Changes

- 冻结 `Turn.status` 的闭合集合 `open|active|completed|completed_empty|interrupted|cancelled|failed|unknown` 与逐状态合法转移表；统一用 `completed_empty` 表示无 canonical output 的正常收敛，明确只有 `interrupted` 或 reason=`execution_lost` 的 `unknown` 可经显式 `resume_turn` 回到 active，`cancelled`（包括普通 cancelled 与 full rollout copy 的 cancelled historical）不可恢复。`history_replay` 不创建 execution；绑定原 Turn 的 `dispatch_replay` 对 cancelled 一律返回 `turn_not_resumable`；若保留重新执行能力，只能通过独立的 `replay_as_new_turn` 创建新的 Turn/root/acceptance/initial execution，并记录 `replay_of_turn_id`，不能在同一 API 语义中既返回错误又创建新 Turn。

- 新增面向 rollout 的 `CanonicalItemRecord` 语义模型，以稳定 `item_id`、物理顺序、`semantic_kind`、`payload_kind`、Turn/group 关联、生命周期和 `producer_ref` 表达用户输入、assistant 输出、reasoning、tool call、tool result 及扩展项；v2 明确区分非空必填核心字段与按语义可空的 Turn/group/wire 关联字段，不再使用含义重叠的通用 `kind` 字段。
- 冻结 `CanonicalItemRecord.status` 的完整终态枚举 `completed|partial|incomplete|cancelled|failed|unknown`、ItemDraft 单向终态化和 immutable JSONL append-only 语义；其中 `completed` 是完整语义 item，`partial` 是已持久化但在正常边界前停止的终态快照，两者都不能再原地修改；冻结每个 `semantic_kind` 对应的完整 `payload_kind`/status compatibility table、typed `tool_outcome` marker 与独立 `ControlOutcome`（不得引入带 outcome 前缀的 unknown 状态别名）；冻结 `payload_kind` 的完整枚举 `text|structured_content|tool_call|tool_result|summary|attachment_ref|opaque|extension`、extension schema/version 以及未知值的 recovery/unsupported 处理，不允许静默降级。
- compatibility matrix 的九个 semantic kind 逐项冻结为：`user_input -> text|structured_content + completed`；`assistant_output -> text|structured_content + completed|partial|incomplete|cancelled|failed|unknown`；`reasoning -> text|summary|opaque|extension + 全部六种 item status`；`tool_call -> tool_call|structured_content + 全部六种 item status`；`tool_result -> text|structured_content|tool_result|opaque|extension + 全部六种 item status`；`runtime_notice -> text|structured_content|opaque|extension + completed`；`compaction_summary -> summary|structured_content + completed`；`attachment -> attachment_ref + completed`；`extension -> extension|opaque + 全部六种 item status`。未列组合必须拒绝；`tool_outcome=success|failure|cancelled|unknown` 仅允许在 `tool_result` payload，控制记录统一使用 `outcome` 字段和 `ControlOutcome`，不与 item `status` 混用。
- 为每个用户交互建立显式 `TurnRecord` 和 `root_input_item_id`；Turn 起点由被接受的真实用户输入确定，不由 LangChain/provider 的 message role、首个物理 item 或 `system_reminder` 推断；execution、model call、retry/resume 和 final item identity 分开建模。
- 冻结 `TurnRecord` 的 `accepted_ingress_id`、`acceptance_idempotency_key`、session-global `turn_ordinal`、origin `source_branch_id`、`initial_execution_id`、`final_item_id` 和 status 约束；两类 acceptance identity 各自在 session 内唯一并有明确冲突语义，只有符合 status 转移表的 `resume_turn` 才在同一 Turn 下新增 execution/model-call lineage；`history_replay` 是唯一可以在同一 owner session 的历史 view 中复用 source Turn/root 的回放操作且不创建 execution；`replay_as_new_turn` 必须在 active view 登记新的 target-local Turn、独立 root/acceptance/initial execution 和新的 view-local `logical_turn_ordinal`，source history 只能作为上下文前缀或 lineage，不能成为新 Turn 的 root；不能在 cancelled Turn 上调用 `dispatch_replay` 或恢复原 Turn。
- 新增结构化 `ContextContribution`、独立的 `ToolSetSnapshot`/`ToolSetRef` 和 `ContextAssemblySnapshot`，记录 middleware/runtime 对本次上下文的实时贡献、来源、版本、hash 和父子关系；静态 system prompt、动态 skill/环境/记忆提示默认是 request-only，普通 tool definition 不属于 `ContextContribution`（`contribution_kind=tool_set` 明确非法），其 registry identity 不伪装成 `ContextRef`，而是在 selection 中通过独立 `ToolSetRef` 投影到 Provider 的 tools/tool-config。选中的 `tool_set_refs[]` manifest identity（snapshot id、source revision、content length/hash token、schema/policy version 和绑定 policy）必须进入 provider-neutral `plan_hash`，跨 provider 只允许 request_hash 因 wire 编码不同而不同；这些元数据预留给未来详情视图和扩展，不要求本变更修改前端。
- 将现有“冻结已应用的完整 `AGENTS.md`、后续追加 diff、压缩后重新加载最新版本”的缓存保持行为统一建模为 source-revision overlay：已应用版本是稳定 base，后续版本是有序 delta，`ContextRequestPlan` 同时选择两者；`SKILL.md` 的已应用 metadata/body 也遵循同一规则，只有 source overlay 自身失效、需要压缩或明确刷新时才物化为新的完整版本，不覆盖旧 item。
- 将历史 view 变化与 source overlay 变化分开：rewind/replay/fork/compaction 首先改变 canonical history 的 view revision，并在下一次请求前执行 context reconciliation；只要 source base/delta 仍可用就复用并重新注入，即使对应 delta 已被历史尾部隐藏，也不强制物化 source。只有 source overlay 自身失效或需要压缩时才推进独立的 overlay epoch。
- **BREAKING** 将 v2 `rollout.jsonl` 从“每行一个完整 LangChain message”改为“每行一个不可变语义 item”；v1/v2 通过持久化 format dispatch 显式分流，JSONL 仍是事实日志，SQLite 继续保存 checkpoint、context view 和有界索引投影。
- 冻结 v2 envelope 的 `format_version=2`、`record_type=item`、item identity/payload/provenance 字段和 `content_hash=sha256:jcs:v1:<64位小写hex>` 的完整输入合同，以及 SQLite manifest 的 `rollout_format_version=2`；通过 `storage_commits.commit_kind` 与正交的 `commit_mode` 区分 sealed-before-dispatch 与 terminal convergence，空输出、失败和 execution lost 使用不推进 JSONL offset 的 `terminal_convergence/metadata_only` commit。`database_meta.committed_jsonl_offset` 是单一权威，`storage_commits.jsonl_offset_after` 必须在同一事务中等值校验；恢复冲突时停止而不是择一继续。
- 禁止 middleware 原地修改 canonical item；静态或动态 prompt、技能说明、环境快照和工具集合以 request-only ref/plan contribution 进入本次请求，只有明确需要成为后续上下文事实的内容才追加新的 canonical item；wire role 只是目标 Provider 的编码投影，不改变这些来源边界。
- 将 SQLite item 索引划分为所有已提交 canonical item 都具备的最小 catalog、按需建立的来源/关系与内容 projection、以及仅对可操作边界建立的 item/content-part anchor；request-only reference 不占用 canonical item sequence 或 catalog。
- 将 request-only prompt/middleware 详情放入当前工作区 session 节点下受保护且可 GC 的 `ContextPlanDetailStore`，并由 `RolloutCheckpointSaver` 作为已提交 context view/plan/snapshot 的唯一业务 owner；业务层和 projector 不得旁路扫描底层 storage。
- 将聚合实现按 design 1.1 的单一 owner、依赖方向和删除门槛拆分到 v2 domain、rollout context infrastructure、纯 mapping、provider bridge、business 和 orchestration 边界；这些稳定架构合同不在 proposal 中重复展开。legacy reader/report/quarantine 只属于一次性 `legacy_import_v1_to_v2` migration/import，不能成为正常 runtime fallback、兼容 API 或第二事实源。
- 新增 item writer 和 context compiler：从 active context view 与 request-only refs 形成有序 context plan，按 provider 能力编译为 LangChain `BaseMessage[]` 或 provider 原生 item/request；不把 `list[CanonicalItem]` 当作 LangChain 原生消息存储类型。生产 domain/storage/runtime/projection 只消费 v2；v1 reader/adapter 不属于 context compiler。
- 保留 LangChain `AIMessage`/`ToolMessage` 作为执行适配器和 checkpoint channel 的投影；canonical `assistant_output` item 与 content part 可以重建为一个合法的 `AIMessage`，但 `assistant_text` 只属于历史/live projection，不是 canonical item 枚举；合并后的 wire role 不得反向覆盖来源 identity。若迁移命令需要读取旧 AIMessage，只能在一次性 `legacy_import_v1_to_v2` staging 中读取，不能成为正常 checkpoint/history/provider API。
- 将实时 `block.started`/`block.delta` 等增量流与 rollout 的已提交 item 分开；只有语义 item 完成或按中断规则终态化后才写入不可变 rollout 记录。
- 为旧版 message-line rollout 定义唯一的显式一次性 `legacy_import_v1_to_v2` 读取/导入边界；v1 原始 artifact 仅用于该命令的 migration、报告、quarantine 和回滚审计，正常 history/provider/checkpoint/runtime 不读取 v1。新 writer 永久只写 v2，不维护 dual writer、dual projector、双 schema 或双事实源，也不通过 fallback 掩盖格式不一致。
- v1 root candidate 按 source sequence 中的 user-root window、既有 `turn_id` 和 user message 数量确定；sequence 只提供可审计的窗口边界，不能凭物理邻接猜测归属。无 ID 的单 root window 生成稳定的 `legacy-missing-turn:<legacy_message_hash>` candidate，已有单一 ID 使用 `legacy-turn:<turn_id>` candidate；assistant/tool/function 才能作为 Turn member，system/developer 作为保留的 `legacy_request_context`，有可信 internal/checkpoint metadata 的 `system_reminder` 映射为 Turn 外 runtime_notice，未知或其它 role 标记 `legacy_unsupported_role` 并保留原行后拒绝；缺失/重复/冲突 ID、多 user message、无法确定边界的组明确拒绝或标记为 `legacy_orphan`，不创建伪造 Turn。
- **BREAKING** 调整 checkpoint、历史 reader、compaction、rewind/replay 和 fork 的边界，使它们以 v2 item/view 引用工作，同时继续允许 compaction 在一个 Turn 中间使用精确 item anchor；v1 只由一次性 migration/import 命令读取。
- 冻结跨 session `context_fork`、`history_prefix_fork` 和 `full_rollout_copy` 使用 `(session_id, entity_type, local_id)` 复合引用；target 一律为 v2，并为所有复制的 Turn/item/tool/execution/model-call/assembly/view/branch/anchor/overlay/detail 以及 `accepted_ingress_id`/`acceptance_idempotency_key` 分配新的 target-local identity，保留 source lineage、root/offset 审计坐标和 detached/pinned retention 语义。context/history-prefix fork 遇运行态 Turn 时目标不存在，full copy 则把该运行态作为不可运行的 cancelled historical state 保存；普通或 cancelled historical Turn 的 `resume_turn`/`dispatch_replay` 均拒绝，要求重跑必须明确调用独立的 `replay_as_new_turn` 创建新 target Turn/root/acceptance/initial execution，并在 target active view 登记新的 view-local `logical_turn_ordinal`；source history 可以复制或引用为上下文前缀，但 source Turn/root 只作为前缀或 lineage，不能成为新 Turn 的 root。该操作不是原 Turn 的 dispatch replay，target 的新输入与重放都不回到 source namespace。

## Capabilities

### New Capabilities

- `itemized-rollout-context`: 定义 v2 canonical item 的持久化、上下文视图、投影编译、生命周期、middleware contribution、实时 provenance、assembly snapshot、分层 SQLite 索引、细粒度操作 anchor，以及只在一次性 migration/import 命令中处理的旧格式边界。

### Modified Capabilities

- `session-turn-history`: 历史 reader 从 item 索引和 item payload 生成 Turn、assistant text、thinking、tool call/result 与 final response 投影。
- `checkpoint-context-branching`: checkpoint、context view、compaction、rewind/replay、fork 和 interrupt 的内部操作边界改用 item/content-part anchor，同时保留 Turn 入口和 branch/retention 语义。
- `rollout-checkpoint-storage`: 将 v1 message-line 与 v2 item-line 的存储、dispatch、提交和 offset 定位合同分开，并将 final item identity 作为终态指针。
- `checkpoint-history-loading`: 让正常历史加载从 v2 item 的统一 view/projection 入口读取，并明确 `assistant_text` 只是 projection；v1 message 仅由一次性 `legacy_import_v1_to_v2` migration/import 入口读取。
- `litellm-aimessage-content-adapter`: 将 LiteLLM 的 `AIMessage` 作为兼容投影/执行载体，canonical v2 由 normalized item draft 和 `assistant_output` semantic item 表达。

## Impact

- 主要影响历史聚合入口所覆盖的 rollout storage、checkpoint saver/runtime、历史 reader/projection、`app/agents/providers/` 内容归一化，以及 LangChain/provider request adapter；最终模块归属和删除门槛见 design 1.1。
- 需要新增 JSONL item schema/version、SQLite 分层 item 索引、context compiler、provenance/assembly metadata 和 LangChain projector；实时 message stream 协议继续作为展示增量通道。
- 影响旧 session 的读取、恢复、fork、compaction、历史 API 和测试 fixture；旧数据必须通过显式版本识别后交给一次性的 `legacy_import_v1_to_v2` migration/import 命令处理，正常 runtime 不提供 v1 只读 fallback。
- 不改变 provider SDK 的事实来源，也不把 OpenAI `OutputItem` 直接作为全局领域模型；provider response item、canonical context item 和 LangChain message 是三个不同层次。

## Verification boundary

稳定 proposal 只定义完成边界，不复制某次 checkout 的测试、dirty-tree、行数或实现审查结果。`tasks.md` 的 **Verification ledger** 是本 change 唯一的易变验证记录，统一保存实际命令、退出码、PASS/FAIL/PARTIAL/UNVERIFIED 证据、历史失败和任务计数。

任务只有在与其合同相称的生产实现和验证证据同时闭合时才能勾选。核心 Web/tool-loop E2E、局部 smoke、静态检查或 OpenSpec strict validate 都只能证明各自覆盖范围，不能替代 item storage/recovery、detail security、cross-projector selection/order、fork/replay、provider/native request、compaction 或 legacy migration 的独立门槛。proposal、design 和 specs 不再重复记录这些运行时状态，避免把过期证据误读为稳定设计或 change 完成。
