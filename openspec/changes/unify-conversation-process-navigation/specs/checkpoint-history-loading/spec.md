## RENAMED Requirements

- FROM: `### Requirement: 前端 Turn 定位不暴露 View 实现`
- TO: `### Requirement: 前端历史定位不暴露 View 实现`

## MODIFIED Requirements

### Requirement: 前端历史定位不暴露 View 实现

系统 SHALL 让前端历史时间线和从历史位置发起的 fork/rewind/replay 按操作使用真实 `turn_id`、`item_id` 及必要的 `part_id` 作为稳定定位键；无 Turn 的内部内容使用真实 execution／Item 来源，不伪造 Turn；branch lineage、checkpoint 和 message range 只作为后端内部字段或诊断信息返回。前端不解释 view 图；原 source_view_id／source_view_revision 作为读取与操作 binding 原样保留、回传及按原 owner 版本判断迟到响应，不将它们作为用户阅读锚点或第二份 view 管理状态。

#### Scenario: 同一个 Turn 出现在多个 view

- **WHEN** 一个 Turn 因 append、rewind 或 compaction 出现在多个不可变 view 中
- **THEN** 前端仍只显示一个稳定的 `turn_id`，后端根据当前 active head 和 cursor epoch 解析本次请求实际使用的 view
- **AND** 前端无需维护一个 Turn 到多个 view 的映射

#### Scenario: 内容定位不依赖 Turn 存在

- **WHEN** 前端需要恢复或打开内部 execution 的 Item／Part
- **THEN** 原 history owner 在当前 Thread 与 view 中核验真实来源并返回有界窗口，前端不维护 view 图、不构造虚假 Turn，也不将定位改成 fork／rewind 操作


### Requirement: 同一历史游标有界推进 Turn 与内部 execution

系统 SHALL 在现有 history API 使用唯一 v2 opaque cursor，同时保存 Turn stream 的 exclusive `logical_turn_ordinal` 和 internal stream 的 exclusive view membership `logical_item_ordinal`（0 为合法坐标）。internal stream 按当前视图中显式可见、属于具有唯一 display notice 的 internal admission 的 canonical notice/response record 分页，单页最多 256 条 canonical record；初始 seed/reference admission 没有 notice 时不产生展示项。视图成员关系必须由持久 owner 明确登记，不得从物理邻接、时间或 metadata 推断。

v2 token MUST 绑定 `session_id`、`thread_id`、`checkpoint_ns`、`rollout_id`、`projection_epoch`、`view_id`、`history_view_revision`、首次同一 snapshot 的 `logical_item_read_upper_bound` 与 Turn ordinal 读上界、固定 `direction`、递增 `stage`、排序规范化的有效 include 集合、初始查询模式，以及 around 初始 anchor 类型、`anchor_turn_id` 或 `anchor_item_id`、同 snapshot 解析的成员坐标与 before/after window；同时保存可空的 `turn_ordinal`、`logical_item_ordinal`。continuation 延续初始查询模式与窗口，不能替换过滤条件、anchor 或窗口；请求省略这些参数时使用 token 冻结值，显式提供且不一致时返回 invalid cursor。未请求的 lane 不查询且明确由冻结 include 标为禁用；已启用 lane 的 anchor 为 NULL 只表示该方向真实耗尽，后续不得从头再读，也不能改 include 复活。两路必须在同一个 `RolloutReadSnapshot` 和 resolved view 中进行有界 keyset 查询；不得全量读取后截断。旧开发期 v1 cursor 及使用物理 item sequence 字段的旧 v2 cursor 直接以 invalid cursor 拒绝，不保留兼容解码。owner/direction 不符返回 invalid cursor，epoch/view/history_view_revision 漂移返回 stale cursor。首次 `logical_item_read_upper_bound` 必须取同一 snapshot 中选定 view 全部可见 `context_view_items.logical_item_ordinal` 的最大值，canonical 与 display-only 成员共用这一坐标 fence；只有没有可见 membership 时才为 NULL，没有 eligible internal record 不等于上界为 NULL。internal reader 仍以 admission 与 display-only membership 判定成员，物理 `item_sequence` 只定位 canonical item，不能决定分页顺序；后续各 lane 查询不得超过这些上界，上界只定位不推 membership。不新建第二历史快照表，也不以活动 view ID 未变为由混入后续追加或终态修订。

上述双 lane 合同适用于主时间线分页；独立活动轮廓使用同一 codec 的 query_mode=activity 冻结单个 process_ref 与成员续点，不推进主时间线双 lane 游标。它不是 around/window 或 execution 默认查询的别名，不能互换 token；具体首次 tail、before 续读及 include／选择器约束沿 session-turn-history 的活动轮廓合同。

每页仅投影本页命中的 response records，并从同一 snapshot 批量关联其 execution admission 和唯一安全 display notice；同一 execution 可跨页出现。Web MUST 按 `execution_id` 合并同一 entry，按稳定 canonical item/part identity 去重，不按正文去重；同一 entry 的 committed outputs 保持其权威成员顺序，standalone notice 与完整 Turn 的合并顺序复用 `session-turn-history`「Internal execution history 使用独立 envelope 和 typed display coordinate」规定的同 view membership ordinal，不使用活动统计范围、物理邻接或时间；重复关联的 display notice 不得重影。Turn 预算与统计保持原语义；`has_more` 由两路实际剩余记录的并集决定，两路耗尽时游标为空。

`around` SHALL 保留现有 `anchor_turn_id` 并在同一入口支持互斥的 `anchor_item_id`。Item anchor 先在同一 snapshot 解析其当前可见 membership 与真实 owner：普通 Turn 成员使用所属 Turn window，internal 成员使用其逻辑坐标附近的有界窗口，并保留窗口两侧复合 continuation；不得从尾页逐页扫描或一次加载整个 execution。storage 从选中窗口的显式成员解析 canonical 边界，只把边界作为查询坐标，不作为 membership 判据。around 的 canonical 窗口边界不能裁切到 internal-only 上界；internal 全在窗口前、窗口后或完全为空时，仍以同 view 坐标 fence 判定两侧续读。窗口内超过 256 条 internal record 时，后续 cursor 继续剩余 record，Turn anchor 保持已返回窗口的边界；空 internal 窗口也必须检查窗口两侧是否尚有可读记录。首尾没有 Turn 的 internal record 和零 Turn view 必须可达。

#### Scenario: 零 Turn 的内部历史仍可续读

- **WHEN** 目标视图没有用户 Turn，但存在超过 256 条可见 internal canonical record
- **THEN** head/tail 返回有界 internal-only 页面和相应 continuation cursor，逐页遍历全部记录而不合成 Turn、不重复耗尽的 Turn stream

#### Scenario: 单次内部 execution 的输出跨页

- **WHEN** 一个 internal execution 的 canonical notice、assistant、reasoning 或 tool records 超过单页预算
- **THEN** 同一 logical_item_ordinal lane 继续其后续 records，每页关联同一 admission/display；前端合并后每个真实 item/part 只出现一次，不静默截断输出、不引入第二 API 或 execution 专用分页 phase

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

### Requirement: 历史与 live 使用同一 Turn response part 模型

系统 SHALL 为历史 summary、历史 detail 和 live 流式事件提供同一套有序 response part 语义模型。part 的来源坐标必须能够表达 v1 `AIMessage.content` 块、v2 canonical item/content part、reasoning item、tool call identity/call index、tool result 的 `tool_call_id`、canonical `compaction_summary` identity/view revision和`assistant_output` 的 text projection；不得要求历史事件拥有 live 流式 delta 才能渲染。临时 part_id、持久 item:part:n 与最终 item:final 不承诺相等；沿原 producer／内容部位／canonical source 的确定关系重绑定展示位置，不改写真实 ID、不按正文相同推测关联。live 尚未提交到 rollout 时可以只携带稳定 `part_id`，不得伪造 JSONL item/message sequence 或新增全局 part index。`compaction_summary`是独立response part类型，不能降级为thinking、assistant text或final response。

#### Scenario: 历史 summary 只缺少投影细节

- **WHEN** 首次加载返回用户消息、reasoning summary、tool summary 和最终文本
- **THEN** 前端将其转换为与 live 相同的 response part 类型，缺失的中间文本和工具正文只标记为未加载，不生成伪造的 delta 事件

#### Scenario: summary include 不泄漏未请求的中间部件

- **WHEN** summary 请求只包含 `user`、`reasoning_summary`、`tool_summary` 和 `final_response`
- **THEN** 返回 reasoning summary、工具名称/状态和最终文本；普通 reasoning 正文、encrypted reasoning payload、tool_call 参数和 tool_result 正文均不得被 response-part adapter 直接返回；三层展开需要的普通 reasoning 身份、类型与组边界可以作为不含正文的 summary 部件返回，不将其标为已加载详情

#### Scenario: live 工具缺少真实调用 ID

- **WHEN** live 工具事件只有流式 `part_id` 而没有真实 `tool_call_id`
- **THEN** 前端使用稳定 `part_id` 关联本次调用和结果，但不得把 `part_id` 序列化为 LangChain `tool_call_id`

完整 owner 投影 SHALL 整体替换对应 owner 的投影；有界 activity_outline 页与 item_ids 正文片段 SHALL 由同一个 canonical reducer 按真实 part／source identity 及后端顺序应用于其明确覆盖范围，保留其它已加载成员，不将省略视作删除。同一 source_view_id／source_view_revision 的轻量投影不得降级已加载正文；较新 binding 使不兼容详情失效，旧响应不得回写。覆盖范围消费原请求选择器、响应成员与 continuation／truncation，不新增持久合并账本或按文本去重。

#### Scenario: 历史完整 detail 补齐同一 Turn

- **WHEN** 原 history 返回明确完整覆盖同一 Turn 的详情投影
- **THEN** Saver 通过 SQLite item/message source 坐标和命中的 rollout JSONL 记录返回完整有序 response parts，前端整体替换该 Turn 投影，用户看到的排序与 live 一致

#### Scenario: 历史不回退到旧消息字段渲染

- **WHEN** 历史 Turn 返回 `response_parts`，但没有 live SSE events
- **THEN** 前端只将 `response_parts` 转换为 TimelineItem 并交给统一 ResponsePart renderer，不再从 `assistant_text`、`thinking_blocks`、`tool_summary` 或旧 trace 字段拼出第二套消息
- **AND** summary 缺少的中间内容保持为未加载状态，只有用户请求 detail 后才通过同一 response-part 模型补齐

#### Scenario: tool_call 位于 assistant output 之后

- **WHEN** v2 assistant output/content parts 后存在 tool_call 与匹配的 tool_result
- **THEN** 渲染顺序由 canonical item/content-part identity 和 tool relation 决定，不用新的全局 part index 改写 canonical 顺序

#### Scenario: 后端返回权威逻辑 Item 统计和计时

- **WHEN** history summary 或 detail 读取一个 Turn
- **THEN** SQLite `turn_item_projection`按`logical_item_ordinal`返回每个`expandable_activity`中间part的`item_id`、`item_sequence`、`part_ordinal`、`created_at`和`elapsed_ms`，并从同revision显式membership返回不包含user/final的`item_count`与Turn`duration_ms`；首项elapsed相对Turn acceptance/root，后续项相对前一个计数成员，duration从acceptance到terminal。`first_item_sequence`/`last_item_sequence`仅是诊断坐标，不得以首尾相减、物理范围或邻接推断数量、顺序或时间
- **AND** 可见reasoning/summary part、tool_call参数、tool_result及显式Turn-member compaction summary各按logical identity计1；聚合tool row、final/user、transport chunk、encrypted-only标记、request-only/CSM/隐藏runtime项和重复carrier不计。同一assistant carrier中的多个tool call按各自`part_ordinal`计数，checkpoint/provider重影只按持久producer identity/tool relation解析，禁止按正文相等去重；时间缺失、倒退或revision不一致显式失败

#### Scenario: queued root 不计入前一个 Turn

- **WHEN**busy thread先提交下一Turn的queued root，前一Turn随后才物理追加tool result、reasoning或final item
- **THEN**两个Turn都按各自显式logical membership/order统计与展示；前一Turn的首尾物理sequence之间出现下一Turn root不会增加其`item_count`、改变`elapsed_ms`或打乱刷新后的DOM顺序

#### Scenario: 局部正文回补保留其它成员

- **WHEN** item_ids 只选择一个或最多四个成员，通过索引读取其受限正文
- **THEN** 原 reducer 只更新明确覆盖的 canonical part，保留其它已加载的成员、顺序、统计及主时间线 cursor，不把局部数组整体替换整个 Turn

#### Scenario: 轻量轮廓迟到不降级正文

- **WHEN** 同一 binding 的轮廓页晚于所选 Item 正文返回，或旧 revision 响应晚于新 binding 返回
- **THEN** 前者保留已有正文，后者丢弃旧响应，不清空其它成员或把旧来源绑定写到新投影

#### Scenario: 最终 checkpoint 引用既有 reasoning part

- **WHEN** 最终 assistant checkpoint 的 typed `content_part_refs.id` 指向同一 Turn 已提交 reasoning Item 的 `projection_identity.block_id` 或相同 provider reasoning item identity
- **THEN** history projection 将它视为既有逻辑 Item 的 carrier 引用，只返回原 canonical reasoning 的 identity、顺序和计时，不按最终 assistant item 再追加一次
- **AND** 只有正文相等但持久 part/provider identity 不同的 reasoning 必须继续分别返回，后端不得退回正文去重

#### Scenario: live 统计与终态 history 收敛

- **WHEN** live Turn 尚未持久化完整 history projection
- **THEN** Web 可按 message stream 实体 identity 临时计算时长和逻辑 Item 数，不伪造 canonical item sequence
- **AND** 终态 history 到达后以后端统计和顺序替换 live 值；若 live/history `item_count` 不一致，前端显式记录协议错误而不是静默采用任一猜测值

#### Scenario: 历史页携带可淘汰详情的权威摘要

- **WHEN** history API 返回一页 Turn detail
- **THEN** 同一响应为每个 detail 返回 identity、revision、ordinal 和顺序一一对应的后端权威 summary；summary 强制使用 summary projection，并移除工具参数、工具结果和其它详情正文
- **AND** Web 详情缓存超过条数或正文预算、或会话转为非活动 scope 时，只能恢复这份同 revision summary；不得在前端重新摘要，重新展开时通过 canonical history offset 定点补载

#### Scenario: 历史详情与实时消息流隔离并有界驻留

- **WHEN** 用户展开已终态化 Turn、切换会话、重启后端，或实时 block/tool 正文持续增长
- **THEN** 历史详情只请求 canonical Turn history API，不查询或拼接 `message.v1` availability/snapshot；实时消息流只承载当前未终态 Turn
- **AND** 前后端限制终态 stream cache、乱序事件、block 正文、工具正文、跨会话 timeline 和详情正文的驻留规模；启动恢复不 materialize 全部终态 snapshot，越界时明确截断或要求 snapshot 恢复，不得无界增长、静默丢失或卡死服务
