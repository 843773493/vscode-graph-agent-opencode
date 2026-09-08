## MODIFIED Requirements

### Requirement: 历史投影区分 assistant text、reasoning 和 final response

系统 SHALL 独立支持 `assistant_text`、`thinking`、`tool_summary`、`tool_call`、`tool_result` 和 `final_response`。`assistant_text` 是从 canonical `assistant_output` item/content part 派生的 projection，不是 canonical item kind。历史 Turn 的开头 MUST 使用 `TurnRecord.root_input_item_id` 或等价的 root input identity；不得把第一个 wire role 为 user 的 item 当作 Turn 起点。`thinking` 投影由 canonical reasoning item 或受保护 reasoning content part 表达，块类型为可读 `reasoning`、provider 生成的 `summary` 或不携带正文的 `encrypted` 标记。`final_response` MUST 使用 `TurnRecord.final_item_id` 或等价的 final item identity，而不是把最后一个 assistant role 作为唯一依据；`Turn.status=completed` 时 final item 必须存在，`completed_empty`、interrupted、cancelled、failed、unknown 或未 finalization 时 final item 必须为空。若为一次性 `legacy_import_v1_to_v2` staging 的无 finalization 旧 fixture，只能在 migration report 中标记 heuristic 提示；正常 v2 history/provider/checkpoint/runtime 不得启用 heuristic fallback。`cancelled` 仅表示该 Turn 被明确停止且不可作为成功响应，不得被历史投影成 final response。

#### Scenario: 混合 canonical item 的 Turn

- **WHEN** 一个 Turn 依次包含 `assistant_output`、reasoning item 和 tool call
- **THEN** 历史 projection 可以从 assistant output content part 独立返回 `assistant_text`、thinking 和 tool summary，LangChain 恢复只按 message group 生成需要的消息，不因 projection 拆分而伪造多条 assistant message

#### Scenario: 思考 item 来源保持可区分

- **WHEN** Provider 返回可展示 reasoning、provider summary 和/或 encrypted reasoning
- **THEN** Web 分别返回 `reasoning`、`summary` 和无正文的 `encrypted` 块，encrypted payload 只能用于 provider 恢复，不得出现在 API 响应

#### Scenario: runtime notice 不伪装成 Turn root

- **WHEN** 一个隐藏的 `system_reminder` 在下一条普通用户输入之前存在，且它在 LangChain/provider projection 中暂时使用 user role
- **THEN** 历史服务仍以普通用户 input 的 `root_input_item_id` 开始新 Turn；runtime notice 只按其 semantic kind、scope 和 relation 展示或隐藏

### Requirement: Agent state 快照不等同于默认历史 projection

系统 SHALL 将 LangChain checkpoint 的 agent-state 快照、canonical item 和 Web/history projection 视为三个不同层次。`get_agent_state_messages` 在兼容序列化时必须保留 `AIMessage.content` 中经过规范化的有序 reasoning/text/content carrier、tool call 字段和必要的 content-part identity，包括最终 assistant 的 reasoning 与可见文本；它不得因为旧调用方只需要可见 text 而静默删掉 reasoning，也不得把该快照写回为第二份 canonical item。

默认历史与 Web projection SHALL 从 canonical item/index 派生 `assistant_text`、thinking、tool summary 和 `final_response`；它可以隐藏 reasoning 正文并只返回受权限控制的摘要/引用。Provider request projector SHALL 在 request 边界依据目标能力过滤或编码 reasoning，不得通过改写 agent-state 快照、canonical item 或已提交 checkpoint 来过滤。

#### Scenario: final assistant 的两种读取视图

- **WHEN** 一个已完成 Turn 的 checkpoint 含有按顺序排列的 reasoning 和 text content blocks
- **THEN** agent-state 快照保留两个 blocks 以支持 LangChain/诊断恢复；默认 history 只返回 `assistant_text` 和按策略决定的 thinking projection；两者都引用同一 canonical item，不创建 `assistant_text` canonical 记录

#### Scenario: 旧 text-only consumer

- **WHEN** 旧调用方断言 final agent-state 只有 text
- **THEN** 系统将其标记为 legacy compatibility mismatch；不能为了通过该断言静默删除 canonical checkpoint 中的 reasoning，Provider projector 也不能绕过目标能力策略直接发送该快照

### Requirement: Turn acceptance identity 在历史层保持 session-local 唯一

历史服务和 Turn resolver SHALL 将 `accepted_ingress_id` 与 `acceptance_idempotency_key` 视为两个不同的 session-local identity，并分别约束 `(session_id, accepted_ingress_id)` 与 `(session_id, acceptance_idempotency_key)` 唯一；每个 identity 只能一对一指向一个 accepted Turn。相同 ingress、相同 acceptance key、相同 payload hash 和相同 origin branch 的重复 acceptance 只能返回既有 `turn_id`、root 和 initial execution，不创建第二个历史 Turn；同 ingress 被不同 key 重用、同 key 搭配不同 ingress/payload/branch，或跨 session 直接复用裸 identity 时，必须返回明确 acceptance idempotency conflict，且不修改原 Turn 或历史顺序。跨 session fork 的 copied acceptance identity 必须先映射为 target-local 值，source identity 只在 lineage/audit 中可见。

#### Scenario: 重复 acceptance 不产生第二个历史 Turn

- **WHEN** 同一 session 收到相同 `accepted_ingress_id`、`acceptance_idempotency_key` 和 payload hash 的重试
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

#### Scenario: 默认摘要只读取轻量投影

- **WHEN** 前端请求默认的最新 Turn 摘要
- **THEN** 服务从 SQLite projection 和目标 JSONL item offset 读取 user、thinking blocks、tool summary 与 final response，不 materialize 完整 LangChain message list，也不读取 tool_result 正文

#### Scenario: 显式详情读取目标 item

- **WHEN** 用户只为当前 Turn 请求 tool_call 和 tool_result
- **THEN** 服务只定位并读取该 Turn 命中的 item 记录，不扫描整个 rollout 文件或其它 Turn

#### Scenario: source overlay 只在详情视图展开

- **WHEN** `AGENTS.md` 或已使用的 skill source 存在未物化的 base/delta overlay，用户请求默认 Turn history
- **THEN** summary 不把 overlay diff 当作 assistant/user 文本；获得授权的 provenance 详情请求才可按 assembly/source reference 返回其版本、来源和有界 diff 摘要

## ADDED Requirements

### Requirement: 历史详情为 provenance 和扩展视图保留稳定引用

历史服务 SHALL 为 item、source、middleware contribution、独立 `ToolSetRef` 和 ContextAssemblySnapshot 提供稳定且可权限控制的 reference，允许未来的特定历史视图或扩展按 reference 请求来源详情。默认 Turn summary/detail MUST 只返回安全摘要、可用性和引用标识，不自动返回 middleware 内部状态、完整 prompt、tool schema 或受保护 payload；ToolSetRef 只能作为受策略控制的 assembly/provenance metadata 展示，不能成为 canonical message 或 Turn item。其 snapshot id、source revision、content length、hash token、schema/policy version 和绑定 policy 属于 plan_hash manifest identity，历史 reader 不得用当前工具 registry 取代该 manifest；`ContextContribution.contribution_kind=tool_set` 不是合法历史贡献类型，legacy 值只能 quarantine。

#### Scenario: 默认历史不泄露 middleware 细节

- **WHEN** 用户请求默认历史 Turn
- **THEN** 返回 item 内容、状态和可选的 provenance summary/reference，不返回完整 middleware prompt、内部 state 或受保护 tool/reasoning payload

#### Scenario: 扩展按 item 展开来源

- **WHEN** 一个获得授权的扩展根据 item reference 请求 middleware/assembly 详情
- **THEN** 服务只读取该 item 命中的 provenance 和 assembly metadata，按 retention、visibility 和大小预算返回可展开详情，不加载整个 rollout
