## MODIFIED Requirements

### Requirement: Turn 是默认分页边界

历史加载必须使用统一的 `Turn.status` 闭合集合 `open | active | completed | completed_empty | interrupted | cancelled | failed | unknown`；其中 `completed_empty` 是唯一的无 canonical output 正常终态名称。普通 `cancelled` 及 fork 导入的全部历史 Turn（无论原 status）只能作为不可运行历史读取，不能由历史加载触发 `resume_turn` 或 `dispatch_replay`；对原 Turn 的这两种请求必须由业务层显式返回 `turn_not_resumable`。导入保留原 status、final item 和业务 hash，以目标本地冻结 namespace binding 核验历史归属，不把原历史批量改成 cancelled，不为此取消 source 执行。若需要重新执行，只能由独立的 `replay_as_new_turn` 创建新的 Turn/root/acceptance/initial execution，在 active view 登记新的 view-local `logical_turn_ordinal`，source history 可以作为上下文前缀但 source Turn/root 不能成为新 Turn 的 root，并记录 `replay_of_turn_id`，不能把新 Turn 创建操作混入原 Turn 的错误响应。

系统 SHALL 以完整 Turn 作为默认加载和分页单位。Turn 必须由 `TurnRecord.root_input_item_id` 表示其真实用户输入起点；用户输入、合并 steering Job、工具活动和最终状态 MUST 保持在同一个 Turn 中。历史顺序使用当前 context view 的 `logical_turn_ordinal`，不使用会跨 branch/view 保持不变的全局 `turn_ordinal` 重排；root lookup 必须沿 view fork lineage 解析到同一个 TurnRecord。`pending_next_turn` runtime notice、request-only prompt、缓存保持型 source delta 和其它内部消息不得创建 Turn、占用 root 或把一个 Turn 拆成独立分页项；overlay 正文只通过按需的 assembly/provenance 详情查看；默认历史允许返回不含正文的上下文存在性标记，标记关联实际请求边界且不占用 Turn、用户输入或活动计数。

存在性标记的展示锚点 SHALL 与真实用户 root 分离：首次派发的 System Prompt／初始内部用户角色注入标记位于 transcript 起点、第一条真实用户消息之前；打断提醒实际被后续请求使用后的标记位于后续新用户消息之前。标记属于原历史页的 metadata，不创建更早的伪 Turn，不因提醒产生于旧 execution 就归回旧轮次，也不只挂在 assistant response 内部。实时与纯历史加载遵守同一锚点；未派发使用的 pending 提醒不冒充已使用上下文。

完整 Turn 分页指保留真实 Turn 分组边界，不要求重新展示选定 view 已隐藏的成员。某 Turn 仍有可见 canonical 成员时，root 可仅保留为 hidden backing identity/排序坐标，summary/detail 只返回选中可见正文；不得因 root hidden 遗漏保留的回答，也不得为了分页把 root 加回可见历史或模型输入。普通完整 view 的原读取行为保持不变；具体分组和坐标规则复用 `session-turn-history`，不增加第二条 reader。

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

