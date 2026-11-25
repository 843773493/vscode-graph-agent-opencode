# session-turn-history Specification

2026-11-05 发布边界：本轮只发布已验收的通用上下文／存储、精确 Thread、来源激活、fork 和恢复合同。团队／成员／委派及跨 Session 协作的新设计和专项场景仅作延期参考，不构成本轮已实现能力；已有协作行为不在本次同步中删除或改变。

## Purpose

从精确 SessionThread 的 canonical rollout 和 SQLite view 提供有界历史、逻辑活动统计、稳定定位与恢复投影，保持真实来源身份、顺序和冻结请求事实。
## Requirements
### Requirement: Rollout 是聊天历史的唯一权威来源

系统 SHALL 直接从会话 rollout 的 `index.sqlite` 和 `rollout.jsonl` 读取聊天历史。正常读取只接受 canonical 格式3语义 Item 与原 storage owner 登记的当前 SQLite 结构；本轮集成目标为 schema34，不将早期未发布的 schema31／32／33 当作兼容来源。元数据和 JSONL envelope 必须一致，旧版、未知版本或未完整发布的来源明确拒绝并保留原件，不运行已退出支持的 v1→v2 importer。上下文边界由 SQLite context view 表达，不创建物理 segment。Trace、日志和旧 turn projection MUST NOT 作为 `/bootstrap` 或 `/history` 的数据源，也不得在 rollout 缺失时静默回退。

#### Scenario: 旧 Trace 不能伪造历史

- **WHEN** 会话只有旧 Trace 文件而没有有效 rollout
- **THEN** 历史 API 返回可诊断的 rollout 缺失或损坏错误，不返回旧投影中的 Turn

### Requirement: 历史支持方向和游标

系统 SHALL 支持从头、从尾、游标之前、游标之后和游标中心加载完整 Turn。游标 MUST 是不透明值，并绑定 rollout、branch、projection epoch 和锚点。

#### Scenario: 游标前加载

- **WHEN** 客户端请求游标之前的 5 个 Turn
- **THEN** 返回最多 5 个不重复且完整的 Turn，并提供继续向前的游标

#### Scenario: 游标中心加载

- **WHEN** 客户端请求 `around(anchor)` 且未指定窗口覆盖数
- **THEN** 按所属 Gateway 配置返回锚点前后各 4 个完整 Turn，并同时提供 `before_cursor` 和 `after_cursor`

#### Scenario: 双向游标继续加载

- **WHEN** 用户向上滚动提交 `before_cursor` 或向下滚动提交 `after_cursor`
- **THEN** 服务只读取对应一侧的下一个固定窗口，不重复锚点 Turn，并返回更新后的同方向 cursor

### Requirement: Turn 是默认分页边界

系统 SHALL 将用户消息、合并 steering、工具活动和模型最终状态组织为完整 Turn；内部消息和工具记录不得把 Turn 拆成独立的主时间线分页项。

默认分页完整性 SHALL 指真实 Turn envelope 与成员归属不被拆散，不代表一页必须交付该 Turn 的所有正文。过程内部的 activity_outline 和选中 Item 正文可以有界续读，响应明确所覆盖成员与未加载范围；不产生伪 Turn、不以详情片段替换整个 Turn，不影响主时间线分页坐标。

#### Scenario: 工具活动保持完整

- **WHEN** 一个 Turn 包含 assistant tool_call、多个 tool_result 和最终 assistant 响应
- **THEN** 任意历史方向的结果都不会拆分这个 Turn

#### Scenario: 大型 Turn 的内部详情分页
- **WHEN** 一个 Turn 的工具活动或中间文本超过详情单页预算
- **THEN** 主时间线仍展示同一个 Turn，内部轮廓与正文分片补读且保留既有成员，未加载内容不被当作已删除或完整交付

### Requirement: include 策略控制内容投影

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

#### Scenario: 显式工具详情
- **WHEN** 调用方显式请求选中工具的参数／结果正文
- **THEN** 同一历史 owner 沿精确 Item、view binding 和原预算返回正文；其它工具不详情化，超限保留截断事实，不改变声明／结果身份。

### Requirement: Gateway 应用默认渐进加载

系统 SHALL 按会话所属 Gateway 的嵌套配置执行历史加载。无已保存视图锚点时默认读取最新 1 个 Turn；存在锚点时使用 `around(anchor)` 返回前后固定窗口，默认两侧各 4 个 Turn，默认只包含 `user + final_response`。后续向上使用 `before_cursor`，向下使用 `after_cursor`，不得通过滚动次数隐式切换批次。

#### Scenario: 首次进入会话

- **WHEN** Web 首次打开会话
- **THEN** 只返回最新 1 个 Turn，工具摘要默认折叠

#### Scenario: 恢复已保存锚点

- **WHEN** 用户重新进入一个已保存历史位置的会话
- **THEN** 客户端先请求 `around(anchor)`，恢复锚点前后窗口并保持该 Turn 的视觉位置

#### Scenario: 代理不覆盖所属 Gateway 策略

- **WHEN** Gateway A 代理读取 Gateway B 上的会话
- **THEN** B 的历史配置决定批次和投影，A 只透传结果和游标

### Requirement: 历史 cursor 必须校验精确快照版本

历史 cursor SHALL 复用 checkpoint-history-loading 的唯一 v3 复合游标合同（逻辑 Item 续点，不保留 Part 坐标或旧版本解码），绑定精确 SessionThread、view、history_view_revision、projection epoch、查询选择器与逻辑位置。成员变化、普通追加或终态更新推进该历史版本时旧 cursor MUST 明确 stale；不得把 token 内部推进阶段当成按滚动次数增加页面批次。rewind、branch 切换和删除同样不得静默切到新历史。

#### Scenario: rewind 后使用旧游标

- **WHEN** 客户端在 rewind 后提交旧游标
- **THEN** API 返回可识别的 stale-cursor 错误，不静默返回另一条 branch

#### Scenario: 同 epoch 的历史版本改变

- **WHEN** 同一 view 或 successor 在 projection epoch 不变时因追加或终态更新推进 history_view_revision
- **THEN** 旧 cursor 明确失效，客户端沿原刷新和定位入口重新读取，不混合两个历史快照

### Requirement: 历史读取有服务端硬上限

系统 SHALL 限制 Turn 数、record 数、详情批次、响应字节、总字符和单项字符。客户端传入更大的值不得绕过限制；结果超过预算时返回有界结果、继续游标和明确的截断状态。

#### Scenario: 请求超过响应预算

- **WHEN** 请求范围超过服务端 Turn 或字节预算
- **THEN** 返回已完成的有界结果、继续游标和明确的截断状态，不构造无限大的响应

### Requirement: 历史失败必须透明

系统 SHALL 对 rollout 损坏、索引不一致、恢复失败和无效 cursor 返回包含原因的错误。系统不得以空历史、旧投影或虚假的默认值掩盖失败。

#### Scenario: rollout 索引损坏

- **WHEN** SQLite 索引或已提交 JSONL 记录无法恢复
- **THEN** API 返回包含会话和失败原因的错误，不返回空历史或旧 projection

### Requirement: 历史投影区分 assistant text、reasoning 和 final response

系统 SHALL 独立支持 `user_text`、`user_attachments`、`assistant_text`、`thinking`、`tool_summary`、`tool_call`、`tool_result`、`compaction_summary` 和 `final_response`。`assistant_text` 是从 canonical `assistant_output` Item 派生的 projection，不是 canonical item kind；history `compaction_summary` Item 投影必须直接引用同一Turn中`semantic_kind=compaction_summary`的canonical item identity/view revision，不得从压缩后的普通文本或notice猜测。历史 Turn 的开头 MUST 使用 `TurnRecord.root_input_item_id` 或等价的 root input identity；不得把第一个 wire role 为 user 的 item 当作 Turn 起点。`thinking` 投影由 独立 canonical reasoning Item 的安全投影 表达，块类型为可读 `reasoning`、provider 生成的 `summary` 或不携带正文的 `encrypted` 标记。`final_response` MUST 使用 `TurnRecord.final_item_id` 或等价的 final item identity，而不是把最后一个 assistant role 作为唯一依据；`Turn.status=completed` 时 final item 必须存在，`completed_empty`、interrupted、cancelled、failed、unknown 或未 finalization 时 final item 必须为空。格式1／2转换已退出支持，正常 history/provider/checkpoint/runtime 不启用 heuristic fallback。`cancelled` 仅表示该 Turn 被明确停止且不可作为成功响应，不得被历史投影成 final response。

#### Scenario: 混合 canonical item 的 Turn

- **WHEN** 一个 Turn 依次包含 `assistant_output`、reasoning item 和 tool call
- **THEN** 历史 projection 可以从 独立 assistant output Item 独立返回 `assistant_text`、thinking 和 tool summary，LangChain 恢复只按 message group 生成需要的消息，不因 projection 拆分而伪造多条 assistant message

#### Scenario: 思考 item 来源保持可区分

- **WHEN** Provider 返回可展示 reasoning、provider summary 和/或 encrypted reasoning
- **THEN** Web 分别返回 `reasoning`、`summary` 和无正文的 `encrypted` 块，encrypted payload 只能用于 provider 恢复，不得出现在 API 响应

#### Scenario: 上下文压缩作为独立历史 Item

- **WHEN**一个Turn在工具循环中提交了可见且`turn_scope=turn_member`的canonical `compaction_summary`
- **THEN**history按其原item identity/order返回一个可展开`compaction_summary` response Item，既不伪装成thinking/final，也不与压缩前后的reasoning或tool Item合并

#### Scenario: runtime notice 不伪装成 Turn root

- **WHEN** 一个隐藏的 `system_reminder` 在下一条普通用户输入之前存在，且它在 LangChain/provider projection 中暂时使用 user role
- **THEN** 历史服务仍以普通用户 input 的 `root_input_item_id` 开始新 Turn；runtime notice 只按其 semantic kind、scope 和 relation 展示或隐藏

#### Scenario: 混合用户输入只投影文本与附件元信息

- **WHEN** 冻结的 user_input 同时包含文本、附件 manifest 和模型使用的图片预览
- **THEN** 历史仅将用户文本与附件 metadata 投影为各自 DTO，不把 rich block 或整个 payload 字符串化为正文；完整模型恢复仍按冻结输入取得预览，默认历史不返回预览 data URL

最终回答 SHALL 沿明确 final_item_id 所属原输出组的真实有序输出成员展示；final_item_id 指向该组末输出 Item。多段文本／媒体不因最后一项为非文本而丢失，不另写聚合 final 正文或按最后 assistant 文本猜测。已提交思考复用真实来源，不重复写入或计数。历史与实时投影、同 binding 丰富度合并及受保护内容沿 checkpoint-history-loading「历史与实时共享 Item 内容投影」执行。

#### Scenario: 多 Item 最终回答
- **WHEN** 原最终消息包含多个文本或媒体输出 Item
- **THEN** final pointer 只引用已有真实末输出 Item，最终回答按原组成员显示全部允许公开的输出；不另造聚合消息，不把纯思考当作最终回答

### Requirement: 历史摘要不 materialize 完整消息

历史投影 SHALL 识别统一的 `Turn.status` 闭合集合 `open | active | completed | completed_empty | interrupted | cancelled | failed | unknown`；`completed_empty` 是唯一的无 canonical output 正常终态名称。`completed` 才能通过 `final_item_id` 返回正常 final response，`completed_empty`、`interrupted`、`cancelled`、`failed` 和 `unknown` 不得伪装成成功响应；普通 `cancelled` 及 fork 导入的全部历史 Turn（无论原 status）均不可对原 Turn 执行 `resume_turn` 或 `dispatch_replay`，请求必须返回 `turn_not_resumable`。导入历史保留原 status，不为此取消 source 执行。`history_replay` 只能在同一存储 owner 的 history view 中复用 source Turn/root 且不创建 execution；已导入历史经目标本地索引和冻结 namespace binding 核验原 scope，不回读 source。若需要重新执行，只能由独立的 `replay_as_new_turn` 创建新的 Turn/root/acceptance/initial execution，在 active view 登记新的 `logical_turn_ordinal`，并记录 `replay_of_turn_id`；source history 可以作为上下文前缀，但 source Turn/root 不是新 Turn 的 root；新输入也创建新的 Turn。

#### Scenario: history_replay 与 replay_as_new_turn 分离

- **WHEN** 历史服务从某个 Turn/view/anchor 执行 `history_replay`，或调用方明确选择 `replay_as_new_turn`
- **THEN** `history_replay` 只在同一存储 owner 的不可变 history view 中登记并复用 source `turn_id`、`root_input_item_id` 和历史 ordinal，不创建 execution；它返回 `history_view_id` 并按 checkpoint-context-branching 的只读 selector 合同读取，不替换 execution active branch/head，也不改变已有 pending transition；`replay_as_new_turn` 则在 active view 登记新的 target-local Turn、`user_input` root、acceptance、initial execution 和新的 `logical_turn_ordinal`
- **AND** `replay_as_new_turn` 可以复制或引用 source history 作为上下文前缀，但 source Turn/root 只能作为前缀或 lineage，不能成为新 Turn 的 root；对普通 cancelled 或 fork 导入的任意历史 Turn，原 Turn 的 `dispatch_replay` 仍返回 `turn_not_resumable`，不能由该错误响应隐式创建新 Turn

`tool_summary`、`thinking`、final pointer 和受界限的最终 `visible_text` SHALL 从 SQLite item/index projection 读取。中间完整 `assistant_output` item、tool_call 参数和 tool_result 只在显式 include 时按 item offset 读取目标 `rollout.jsonl` 记录；普通分页不得扫描整个 rollout 文件、恢复完整 canonical item 集合或 materialize 完整 LangChain checkpoint，也不得把 request-only system context、缓存保持型 source delta 或 pending runtime notice 当作历史 Turn message；这些上下文只能通过 assembly/provenance reference 在授权详情视图中展开。`visible_text`、`reasoning` 和 `summary` 均不得包含 encrypted reasoning 或未授权工具 payload。

Turn标题的`item_count` MUST只统计同一history revision、显式属于该Turn且被projection标记为`expandable_activity`的逻辑成员：每个可见 reasoning/summary Item 计1，每个独立 tool_call Item 计1，每个匹配 tool_result Item 计1，非 final 普通文本／媒体等显式可展开过程 Item 各计1，可见且以`turn_scope=turn_member`归属该Turn的`compaction_summary`计1，其它扩展类型只有显式声明相同activity语义才计数。聚合`tool_summary`行、DOM容器和checkpoint/provider carrier不另计；真实user root、final response、transport chunk、encrypted-only无正文标记、request-only contribution、CSM source/隐藏runtime notice及仅有存储metadata的记录均不计。相同持久 item_id无论被多少carrier引用只计一次，正文相同但identity不同的可展开成员分别计数。每个成员的`elapsed_ms` MUST由后端按logical order计算：首个成员相对Turn acceptance/root时间，后续成员相对前一个计数成员；`duration_ms`从acceptance到Turn terminal时间。时间缺失、倒退或membership revision不一致必须返回显式projection错误，不得由Web钳零、补默认值或使用物理相邻item代替。

#### Scenario: 默认摘要只读取轻量投影

- **WHEN** 前端请求默认的最新 Turn 摘要
- **THEN** 服务从 SQLite 轻量 Item projection 和有界目标 JSONL offset 读取 user、thinking 安全摘要、tool summary 与 final response，不 materialize 完整 LangChain message list，也不读取 tool_result 正文

#### Scenario: 显式详情读取目标 item

- **WHEN** 用户只为当前 Turn 请求 tool_call 和 tool_result
- **THEN** 服务只定位并读取该 Turn 命中的 item 记录，不扫描整个 rollout 文件或其它 Turn

#### Scenario: source overlay 只在详情视图展开

- **WHEN** `AGENTS.md` 或已使用的 skill source 存在未物化的 base/delta overlay，用户请求默认 Turn history
- **THEN** summary 不把 overlay diff 当作 assistant/user 文本；获得授权的 provenance 详情请求才可按 assembly/source reference 返回其版本、来源和有界 diff 摘要

#### Scenario: 聚合工具行与上下文压缩按逻辑成员计数

- **WHEN**一个Turn含一条可见reasoning、一个tool_call、一个tool_result、一条Turn-member compaction summary和最终response，Web把call/result渲染为一个聚合工具行
- **THEN**后端`item_count`为4，聚合行不产生第五项、final不产生第五项；每项elapsed按logical order给出，刷新后不得按DOM节点数或物理sequence重算

工具摘要 SHALL 包含本规范「工具历史保留声明关系和可持久执行摘要」允许的轻量事实；默认不为恢复同批或执行关系读取完整参数／结果。final 输出组全部成员不计为过程活动，工具 call/result 合行不改变两项业务计数。

### Requirement: 页面级读取延迟可验收

系统 SHALL 使用资产工作区中预生成的 128 Turn 混合 fixture 验证 bootstrap、around、before/after 双向加载和当前 Turn 详情重载。后端 SQLite/reader 热路径的优化目标为 p95 100ms 内，硬验收上限为 200ms；浏览器 prepend/append 与锚点恢复硬验收上限为 200ms。后端真实模型会话和浏览器确定性大型工具 mock 会话必须使用不同 session ID，测试运行时只读取复制后的工作区，不重新生成或修改资产。测试必须记录 p50/p95 和 snapshot、SQLite query、materialize 计数。

#### Scenario: 128 Turn 锚点双向加载

- **WHEN** 浏览器完成 bootstrap、around(anchor)、before/after 加载并点击一个 Turn 的统计折叠行
- **THEN** 每次请求都使用一个 read snapshot 和页面级批量读取，记录 p50/p95；后端 p95 不超过 200ms 并持续向 100ms 优化，浏览器 prepend/append 与锚点恢复不超过 200ms

### Requirement: Turn 活动统计与按需中间消息

每个历史 Turn SHALL 显示同一历史 revision 的权威 duration_ms 和 item_count。逻辑活动成员、去重与 elapsed_ms 的唯一计量合同沿本规范的历史摘要和 checkpoint-history-loading，不按分组、组件或隐藏 carrier 重新计量。三层过程展开 SHALL 通过原 SQLite-backed history owner 按层加载：第一层取得有界中间文本与活动轮廓，第二层展示组内轻量标题／状态，第三层才读取所选成员的受限思考、参数或结果正文。未展开组不得因第一层打开而全量详情化，其它 Turn 也不得被顺带读取。零成员不制造展开按钮；失败 metadata 和必要操作入口不依赖详情展开。

非 final 的普通文本 SHALL 作为显式过程成员参与相同计量，不因 Turn 失败、中断或没有 final pointer 而丢失。原 `text`／`assistant_text` 正文选择与默认摘要中的 `final_response` 正文覆盖保持可读，但不改变该 Item 的 `text` 身份、不升级成 `final_text`；活动轮廓中的普通文本仍遵守原有界正文预算。计数与诊断范围 SHALL 使用同一成员集合，不能出现计数为零但仍携带成员 sequence 范围的响应。

#### Scenario: 仅含普通文本的中断过程
- **WHEN** 一轮执行只留下非 final 文本后被中断，或失败前已产生普通中间说明
- **THEN** 冷启动首读、活动轮廓及 Item 点读保留相同文本 Item与真实来源，过程计数非零，独立最终回答保持为空

#### Scenario: 统计行默认折叠
- **WHEN** around 或 before/after 返回有可展开活动的已结束 Turn
- **THEN** 显示统计行和右侧折叠箭头，但不读取或渲染中间 assistant、reasoning、tool_call 和 tool_result 全部正文

#### Scenario: 逐层展开统计行
- **WHEN** 用户先展开过程、再打开活动组、最后展开一个工具
- **THEN** 各步只加载对应缺失的受限投影；工具正文只在第三步取得，其它工具和 Turn 不详情化，重复开关复用已经满足的投影

#### Scenario: 展开统计行
- **WHEN** 用户点击某一 Turn 的统计行
- **THEN** 按三层展开只取得本层缺失的有界投影，第三层才补读目标成员正文；其它 Turn 不详情化，开关复用已取得内容。

### Requirement: Turn 身份分组与正文可见性分离

Turn 的真实 root identity 与正文可见性 SHALL 分离。有效前缀只保留某 Turn 的摘要、回答或其它可见 canonical 成员，而其真实 root 仅作 hidden backing 时，唯一普通 Turn history reader 仍须按原 Turn 分组返回这些可见成员；root membership、原 logical 坐标和 TurnRecord 继续校验，但 hidden root 不进入 `user_messages`、response_items 或 Provider selection。只有 hidden backing 的 Turn 不单独列为历史行；不得升 root 为可见、伪造用户输入、改成 internal execution 或新增并行 fragment reader。tail/bootstrap、显式 Turn 选择、窗口和续页必须使用同一分组准入规则。

#### Scenario: 压缩后保留回答但不显示旧提问

- **WHEN** fork 的有效前缀保留摘要和一个普通 Turn 的 final，而该 Turn 的 root 不在有效 selection 中且只作 hidden backing
- **THEN** 目标默认历史和显式详情返回同一真实 Turn 下的选中摘要/回答，`user_messages` 可为空；旧 root 不显示、不成为模型输入，不因 hidden root 静默遗漏 final，后继新聊天仍按目标 owner 执行

### Requirement: Turn 和历史投影必须精确归属于 SessionThread

Turn acceptance identity、`turn_ordinal`、root item、history view 和 `final_item_id` SHALL 在 `(session_id, thread_id)` 范围内唯一；不得再把它们解释为仅 session-local。默认 Session history 只解析 main thread，显式 thread history 只读取该 thread 的 catalog/index/rollout。child thread 的 assistant/tool/result 不得混入 main-thread Turn，跨 thread 汇报必须作为带 source-thread provenance 的独立 canonical/runtime item，由目标 thread 的 owner 提交，不得把原 item 复制为普通用户输入。

#### Scenario: delegated child history 不污染主聊天

- **WHEN** delegated child thread 产生 tool result 或最终报告
- **THEN** 主 thread 的默认历史不包含 child 的原始 Turn/item；若系统需要向主 thread 发送汇报，则该汇报保留 child `session_id/thread_id` 和 source item/execution identity，且不是新的真实 user Turn root

### Requirement: Web 正式入口支持按稳定来源坐标重复定位

Web 主窗口的主聊天区域 SHALL 保留用户可操作的“跳到指定轮次”入口，并将其与 Item 搜索、引用和浏览恢复统一到原历史读取与虚拟列表定位链路。Turn 入口允许输入稳定 turn_id 或选择已加载轮次；内容目标使用真实 Item，支持没有 turn_id 的内部 execution。目标 MUST 固定到完整 Gateway／工作区／Session／Thread，展示序号和虚拟列表索引不得作为身份。

未加载的 Turn 使用原 anchor_turn_id 有界 around；未加载 Item 使用同一 history 入口的 anchor_item_id，在同一历史快照核验其 owner、可见性及真实逻辑位置；内部 execution 目标必须取得包含该成员的受限邻近窗口，不能逐页扫描至命中。定位与保存恢复不再限主 Thread；每个真实 Thread 有独立展示记录，替代旧 `(user_id, workspace_id, session_id)` 与无 Turn 时保存 scrollTop 后忽略的约定。

每次主动提交 SHALL 能重新定位同一目标，加载、挂载和尺寸变化时按稳定来源与展示部位重新解析，不使用旧索引。显式内容跳转只打开必要祖先并定位相应详情，暂停自动跟随；普通位置恢复尊重已有折叠状态。切换 owner 或发起后继跳转时，旧响应、错误和成功提示不得更新新目标。显式目标不存在或加载失败 MUST 明确显示，不以回到最新或相邻对象冒充成功；DOM 缺失不得作为不存在证据，超时及归属／版本错误不得转换为资源删除。新 Session 和 fork 不继承来源位置。

#### Scenario: 跳到尚未挂载的 Turn
- **WHEN** 用户输入当前线程中未加载的有效 turn_id 并提交
- **THEN** 页面通过同一 around 历史入口取得附近内容并定位真实 Turn，保持原分页限制；Turn 入口不无条件展开全部过程

#### Scenario: 同一目标可反复定位且不会被新消息抢走
- **WHEN** 用户定位一个 Turn 或 Item 后浏览其它位置，再次提交相同目标，期间线程出现新消息
- **THEN** 页面重新定位所选目标且不被新消息推向尾部；只有明确跳到最新才恢复跟随

#### Scenario: 切换 owner 丢弃旧跳转响应
- **WHEN** 定位尚未结束，用户切换 Gateway、工作区、Session、Thread 或提交后继目标
- **THEN** 旧结果和旧错误不改变新视图，不把其它 owner 的同名身份当作原目标

#### Scenario: 无效目标明确失败且正式测试复用用户入口
- **WHEN** 用户输入不存在的目标，或 Web E2E 需要定位历史内容
- **THEN** 不存在目标明确报告；E2E 复用正式导航与可访问控件，专门验收滚动的场景仍真实滚动

#### Scenario: fork 创建的新 Session 不继承来源位置
- **WHEN** 从已有浏览位置的 Session 创建 fork 并打开
- **THEN** 新 Session 使用完整的新 owner scope，不读取来源锚点或偏移

#### Scenario: 没有 Turn 的内容恢复与定位
- **WHEN** 用户恢复或显式定位无 turn_id 的内部执行 Item
- **THEN** 沿真实 execution 和 Item 加载相应窗口，恢复遵守原折叠，显式定位展开必要祖先；均不伪造 Turn 或强制回最新

#### Scenario: 无 Turn 锚点时恢复到最新尾部
- **WHEN** 恢复记录没有 Turn 引用
- **THEN** 有有效 Item／execution／上下文来源时沿真实来源恢复；只有明确 follow_latest 或确无可恢复来源时使用最新窗口，不因缺 turn_id 丢弃有效位置。

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

默认历史不得返回受保护资源正文、内部 `provider_locator`、network credential、memory key 或可反推出它们的调试字段；普通正文、展示及诊断中的原生路径按统一 URI owner 的字段职责合同处理。获得授权的详情请求可以通过 typed resource reference 读取当时 sealed body；即使当前 ResourceRegistry 已发布新 revision、虚拟 URI 改映射或原始文件消失，也只能读取持久化 snapshot/ref，不能查询当前资源来重建历史。live、终态 history、刷新恢复和跨设备 history MUST 使用同一 committed resource ref identity；前端不得从提示文本、`read` 工具调用、URI 字符串或 DOM 节点推断、去重或补造 provenance。

#### Scenario: 零可展开 Item 的 Turn 仍可带资源 provenance

- **WHEN** 一个 Turn 使用了 system/Skill/AGENTS resource snapshot，但没有 reasoning、tool_call、tool_result 或 compaction_summary canonical item
- **THEN** 历史可以在授权详情中返回该 activation snapshot 的安全 provenance，Turn 标题仍显示权威时长与 `Item 0 项`，折叠体显示没有可展开的中间消息；不得把 resource binding 或首条 system/Skill 内容计成 Item

#### Scenario: live 与刷新显示同一资源 revision

- **WHEN** live Turn 已 seal resource revision R1，Turn 完成后 Registry 发布 R2，随后用户刷新页面或从另一客户端读取历史
- **THEN** 所有历史表面都按同一 assembly identity 显示 R1 的安全 `display_uri`/revision/availability，不能把 R2 投影为该 Turn 的来源，也不能因文本相同合并不同 resource identity

#### Scenario: 历史详情隐藏物理来源

- **WHEN** 授权用户展开来自 workspace、Gateway 或 inline 来源的 resource provenance（`memory` 已确证不是 URI scope，不作为来源命名空间出现）
- **THEN** 响应只返回对应命名空间的 `boxteam://` display URI 和策略允许的 manifest；资源目标不使用绝对路径、provider locator、credential、memory key 或旧 `/.boxteam/...` 作为备用引用；API、SSE 或 DOM 中的展示、正文及诊断路径不单独构成协议错误

#### Scenario: 旧 path-based 投影不能恢复

- **WHEN** legacy UI/projector 尝试从 generic read 的 path、Skill 正文或旧 middleware 字段反推来源
- **THEN** 正常当前格式 history 明确拒绝该 projection，不创建虚拟 resource ref、不回退当前 Registry，也不保留兼容字段

### Requirement: 当前 Session 的 child thread 必须作为右侧侧边栏会话资源可见

主窗口的标准会话区 SHALL 默认展示当前 Session 的 main thread。当前 Session 的 durable child thread SHALL 作为 Session 级资源出现在主窗口右侧侧边栏，并提供独立的状态、历史、未读和直接消息入口；child thread 不得作为新的产品 Session混入左侧 Gateway/Session导航，也不得把自己的原始Turn投影进main-thread时间线。

打开child thread列表、状态或历史 MUST 使用持久metadata/history projection，不得唤醒cold Agent runtime。用户从右侧侧边栏向child发送消息时，客户端 MUST 携带精确`session_id + thread_id`，响应和SSE/history cursor也必须返回并校验同一thread；不得退回裸Session默认main-thread路由。

公开 HTTP 合同 SHALL 提供 `POST /api/v1/sessions/{session_id}/threads/{thread_id}/messages` 与同一路径下的 `POST .../history`，复用既有消息请求/准入响应和历史请求/分页响应。child 列表 SHALL 返回只读 `runtime` 及 `report_execution_id/report_status`；`completed` 是执行展示状态，不替代 residency 或删除状态。每个 child 的草稿与异步响应 MUST 按精确 thread 隔离；打开报告 MUST 从其来源 thread 的既有 history owner 读取，不能创建新的 Turn 或将来源历史合并到 main。

main-thread history bootstrap、pending 队列和 active Job 展示 SHALL 消费同一 canonical main thread 范围。`GET /api/v1/sessions/{session_id}/main-thread/pending-requests` SHALL 在 Session 读准入内由后端权威 resolver 取得 main thread，并复用现有 pending owner 的精确 thread 读取；返回快照及摘要的 `thread_id` 表示读取范围，Session 聚合快照该值为空。全部主线程展示写入 MUST 校验快照范围及每个请求的 thread 归属；不得从 Session 的首个 active slot 或全 thread 队列推断 main。既有 Session pending 查询、撤回、清空和 policy 操作继续使用 Session 聚合范围；客户端 mutation 后 SHALL 重新读取 main 完整快照更新主展示，不将聚合 mutation 响应写成主线程镜像。热、冷摘要 SHALL 在同一 pending owner 按 thread 计算 count/limit；cold 只读既有控制引用，不唤醒 runtime、不读取 canonical 正文或另建队列事实。

#### Scenario: main 与 child 同时排队时展示归属准确

- **WHEN** 同一 Session 的 main 与 child 各自存在 active Job 或 pending 请求，用户启动主线程历史、刷新页面或完成一次 Session pending mutation
- **THEN** 主展示只接收 canonical main thread 的 active Job 和 pending 请求；child 原始 Turn 不进入主历史，同一持久队列版本的热与冷 pending 计数及分页上限一致，Session 聚合查询及 mutation 的原范围保持不变

#### Scenario: 队列恢复硬错误后重试原 thread 派发

- **WHEN** 产品准入已生成 main 与 child 的持久 pending 请求，恢复在装载后保存、取队首后保存或任务创建阶段发生硬错误，调用方在同进程重试
- **THEN** 原错误明确返回；重试沿用原 Job、execution binding、活动槽和 FIFO，不重装覆盖已加载队列，也不重复启动已经运行的 sibling。恢复完成标记只在落盘及各 thread 派发结算后发布；同一 thread 的并发重试只创建一个任务，原取消收敛和删除 preparation 边界保持有效

#### Scenario: 取消派发失败且没有执行任务的活动 Job

- **WHEN** 恢复取出队首后在保存或创建任务阶段发生硬错误，用户直接取消这个尚无执行任务的活动 Job、运行时排空这些请求，或重试此前落盘失败的取消
- **THEN** 取消必须先由 canonical owner 提交终态，再发布内存终态和一次取消事件、释放原 thread 的活动槽并唤醒原 FIFO 后继；冷 owner 读取仍为 cancelled，残留队列引用不得复活该请求
- **AND** 终态落盘失败时明确返回原错误，保留 cancelling、原 Job 和原活动槽，后继不得提前执行；重试使用同一条收尾链路，不需要重新准入或重新装载队列
- **AND** 排空按原请求逐项提交取消与事件，后续项失败不得丢失先前成功项的事件；排空不启动后继，重试不重复取消已终结项

#### Scenario: 在右侧侧边栏查看 cold child 历史

- **WHEN** 用户在当前Session右侧侧边栏选择一个已经卸载运行资源的child thread
- **THEN** 界面展示该child独立历史、状态和来源关系，目标runtime保持cold，main-thread聊天内容和选择保持不变

#### Scenario: 从右侧侧边栏向 child 发送消息

- **WHEN** 用户在child-thread对话入口提交一条消息
- **THEN** 请求精确定位该child并在其历史中创建真实用户Turn；实时事件、工具项和最终响应只更新对应child-thread视图，不进入main-thread消息流

#### Scenario: child 汇报与原始历史分离

- **WHEN** child thread完成任务并向parent main thread汇报
- **THEN** main历史只显示带child来源引用的汇报item，用户可从该引用进入右侧侧边栏查看child原始历史；系统不得把child的全部Turn或工具结果展开复制到main时间线

### Requirement: Trace 保留 Session 诊断聚合并支持精确 Thread 查询

Session Trace 的历史与 SSE 入口 SHALL 默认保留 Session 诊断聚合范围；可选 `thread_id` SHALL 经权威 resolver 验证并只返回该 Thread 的事件。每个公开事件 MUST 携带真实 `session_id + thread_id`，历史 mapper、SSE codec 和 Web 消费者 MUST 校验归属；主聊天不得仅按 job/message 或单个 pending 槽接收 sibling 的事件。Goal 事件按其既有 main-only 合同定位。

公开 Trace cursor MUST 绑定 Session、可选 Thread、聚合或精确范围及分页或流用途，跨范围复用 SHALL 明确返回 `410 trace_cursor_gone`。分页保留有界物理扫描预算，精确 Thread 过滤后允许空页带有 `next_cursor`；调用方 MUST 按游标继续读取，不能以空 items 判断结束。查询或读取 cold Thread 的 Trace MUST NOT 物化 Agent runtime。cursor 只定义恢复范围，不代替鉴权。

#### Scenario: 同一 Session 的 main 与 cold child 分别读取 Trace

- **WHEN** 产品入口在 main 与 child 生成执行历史，然后分别读取聚合、main 和 child 的分页或 SSE
- **THEN** 每个精确范围只返回聚合历史中归属相同 Thread 的事件，身份与顺序不变且不重复；稀疏页仍推进游标，跨 Thread、聚合范围或游标用途复用均明确失败，cold child 保持未物化

### Requirement: 用户中断必须绑定明确的 SessionThread

`POST /api/v1/sessions/{session_id}/interrupt` SHALL 在省略 `thread_id` 时解析 catalog 权威 main；显式 `thread_id` SHALL 经统一 resolver 校验。服务 MUST 只选择目标 SessionThread 的可暂停 Job，选定后冻结该 Job，并核对其 execution admission、可选 Turn 和 internal execution 身份；绑定已消失时明确失败，不得重新选择后继 Job 或其它 Thread。main 没有活动 Job 而 child 正在执行时，默认入口 SHALL 返回无活动任务，不中断 child。

可打断 phase、文本、并发工具、取消原因及 reminder 标志 MUST 按 `(workspace_id, session_id, thread_id)` 隔离，执行、重试、事件消费、drain blocker 和清理必须显式传入 Thread。公开响应 MUST 包含真实 Thread，Web 主聊天必须传入其权威 main 身份并验证响应归属；缺身份时明确失败，不发送含糊请求。响应 status SHALL 使用实际 Job 控制结果，不伪造固定 cancelling 状态。Session pending 聚合查询及 mutation 的既有范围不受此项改变。

#### Scenario: main 与 child 同时执行时分别中断

- **WHEN** 产品消息入口让同一 Session 的 main 与 child 同时进入工具执行，用户先默认中断再显式中断 child
- **THEN** 第一次仅终结 main 的执行、消息流与临时状态，child 的工具与取消状态保持原值；第二次响应绑定 child 的原 Job，两个流分别终结且无跨 Thread 的 reminder 或清理

### Requirement: 基础 Web tool-loop E2E 必须验收完整 Session 协作场景

本轮必做范围采用当前非协作 checklist；团队／成员／委派与跨 Session send/read/wait、三 Gateway 协作专项仍是延期设计参考，不要求含这些场景的完整 default 作为发布或归档门。已有协作行为不因本次同步改变。

仓库 SHALL 在已有 `tests/integration/clients/web/conversation_lifecycle/test_conversation_lifecycle.py` 内保留基础两轮聊天/工具循环验收，并在同一 pytest 模块内增加多 Session main thread、Session内durable child、resident runtime回收、跨workspace/server send/read/wait、Session内team状态及右侧侧边栏child对话的分离用例。该模块是这一组跨change业务场景的唯一Web Integration owner；可拆成多个test function并调用共享Python/Node helper，但pytest collection、场景fixture和PASS/FAIL gate必须位于该模块，不得把唯一断言放进未被pytest收集的脚本，也不得在其它E2E中另造一套不同语义的验收流程。进程内`LifetimeScope`的释放合同、共享watch及外部资源lease由`add-context-injection-lifecycle`唯一拥有；本change只验证thread residency owner调用该合同后的可观察结果，不建立第二套dispose/ResourceManager。

验收 MUST 同时使用用户可见DOM、Gateway/workspace网络回执、thread-qualified history/SSE cursor、权威item/time/order投影和只读runtime/trace证据；只检查DOM存在、健康端点或数据库行都不足以通过。基础两轮使用固定golden；每个新增可见Turn也 MUST 从权威history projection取得预期item identity/order、`item_count`与`elapsed_ms`，并与live DOM及刷新后的DOM精确一致：同正文不同identity保留，同identity只出现一次，无可展开item时count为0且标题不得显示首条思考。测试 MUST 使用 `out/tests/integration/clients/web/conversation_lifecycle/test_conversation_lifecycle/workspace/` 作为隔离根，将多个workspace分别放入`hub/`、`spoke-b/`、`spoke-c/`等确定性子目录并分配隔离端口，产物放同级`artifacts/`。A/B/C Gateway必须作为三个独立进程，分别使用同名输出根`runtime/gateways/{hub-a,spoke-b,spoke-c}/boxteam-home/`作为`BOXTEAM_HOME`，只在这些目录内建立稳定identity、测试peer credential与policy snapshot；A通过真实loopback SSH转发等价fixture建立到B/C的全双工WebSocket channel，跨spoke场景必须经过`B → A → C`及`C → A → B`响应路径，不得读取或写入用户正常`${BOXTEAM_HOME}`/`~/.boxteams`，也不得以resolver stub、直接请求远端workspace或B/C直连冒充。pytest拥有的外置`E2ETestControlHarness` MUST 分别提供`residency_clock`、`wait_deadline_clock`，以及按operation/phase寻址的`CommunicationAdmissionBarrier`、`SessionLifecyclePhaseBarrier`和`CopyCaptureBarrier`；后两者至少覆盖deletion journal/fence/final rename、source revision freeze、attachment claim、captured marker、owner_reserved、target publication及attachment finalization。测试backend只通过fixture composition的内部port连接；生产composition只绑定真实时钟与立即返回的no-op phase observer/barrier，不得注册test client、HTTP路由、模型工具或热开关。barrier只暂停并上报phase，不修改业务状态/identity/时间/结果；pytest收到ack后从进程外终止/重启进程，backend不得由test hook自杀。harness socket/log只写同名输出根`runtime/test-control/`和`artifacts/`，不得进入业务库/checkpoint/canonical时间戳；fixture teardown必须关闭全部进程/端口。测试可推进虚拟时间但不得sleep真实阈值或改短产品合同。所有模型请求使用本模块拥有且按SessionThread/model-call identity匹配的确定性ModelStream fixture，禁止访问真实Provider或由并发thread共享顺序游标。

动态identity MUST 通过pytest fixture composition注入的确定性`IdentifierFactory`生成，且每个值仍满足 `migrate-identifiers-to-uuidv7` 的 `uuidv7-identifier-profile` 与canonical ID validator；生产composition只绑定随机factory。测试仍必须经正常Gateway/Web API创建Session/child，不得预写数据库；ModelStream manifest使用已知identity生成并精确匹配裸ID/link/tool args，不得忽略动态字段。该模块 MUST 标记为独占serial E2E组，并用同名输出目录内带PID/start-time校验的`E2EProcessLease`租用避开8010–8016的整套loopback端口和owner manifest。有效lease不得被抢占或通过杀进程清理；stale lease只有验证owner已不存在后才可恢复。每个case使用独立fixture子目录/identity seed，teardown只停止owner manifest列出的本次进程并断言端口释放。

#### Scenario: 基础两轮 tool loop 仍是前置验收

- **WHEN** 运行 `BOXTEAM_E2E_SECTION8_FOCUS=default bun run test:matrix -- --suite=integration-web-conversation-lifecycle`
- **THEN** 基础两轮分别按后端权威坐标精确断言时长、item数量、reasoning→tool→reasoning展开顺序和刷新恢复；不存在按文本去重、重影item或“无消息但计数”。任一新场景不得替代这些断言

#### Scenario: 多 Session 与 child 右侧对话在真实页面中隔离

- **WHEN** E2E创建Git/分析/实施/汇总Session，并由分析或实施main创建child，再从右侧侧边栏打开该child、查看历史，并在已有委派执行期间发送用户消息
- **THEN** 左侧Session导航只列出产品Session，main/child的DOM、历史、SSE/cursor和工具item只更新精确thread；新消息先显示queued，第一条execution继续至少一次tool-loop时其Provider request不包含queued root，刷新历史仍按第一Turn完整tool/final→第二Turn排序而不按物理append交错。同一child按FIFO执行而sibling child可并行，child显示Goal disabled，汇报只以一条带source-thread provenance的main item出现

#### Scenario: idle unload 与 cold history 在 Web E2E 中可证明

- **WHEN** fake clock依次推进到29分59秒、30分00秒，并在cold后只打开child历史，最后发送一条新消息
- **THEN** 只读residency与trace证明仅在30分钟且无blocker、该generation的`LifetimeScope`已取消并排空自有task/句柄后转为cold；另以当前child保有`launch_pending`进程claim或正在`starting|running|paused|stopping`的Node调试进程推进同一fake clock，验证仍resident且显示脱敏debug blocker，debug owner核实终态/结清lease后重新起算30分钟。其它consumer仍持有的共享watch不随child关闭；terminal/browser由领域owner按唯一保留合同核实删除`thread`临时资源，保留`workspace`原实例并移除child关联，MCP继续遵循其owner/lease合同。查看历史不唤醒，新消息才reload；reload产生新的runtime generation，但持久`GraphBinding(graph_id, graph_revision, graph_schema_hash, capability_profile_hash)`逐字段等于卸载前值，Provider/runtime trace证明该次执行使用这一精确binding而非当前latest graph。上述过程不改变已提交item、stable prefix、sealed bytes或历史排序

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
- **THEN**source按原send operation找回同一outbox/communication，target以新grant返回原acceptance receipt；DOM、双端communication/trace和target history证明没有第二个ambient item、Job、Turn或可见消息。AgentLoop 自动续接仅适用于 itemized identity requirement 定义的可证明非终态 pending `send/read/wait` checkpoint；丢失 receipt 本身不授权重放 Provider 或其它外部副作用

#### Scenario: source 丢失 read 或 wait response 后恢复同一 observation

- **WHEN** target已冻结远端read page或无selector wait baseline，而source在保存response/tool result前重启；期间目标新增Turn或Job
- **THEN** source以同一source call/operation invocation和新grant恢复原RemoteObservationRecord/PageRecord，page、selector集合、baseline及terminal envelope不变，该调用方DOM/history只出现一个tool result；read下一页使用新source call但仍属cursor绑定的同一observation/read series，record过期明确失败，不把目标新状态静默混入。原 AgentLoop checkpoint 的自动续接沿 itemized identity requirement 的全部证明条件；已终态/lost 执行只能按显式 resume 合同处理

#### Scenario: 远端裸 ID discovery 不泄露或递归

- **WHEN**用户消息只包含远端裸Session ID，或该ID有多个已授权候选、只存在未授权候选，并在wait期间撤销权限
- **THEN**真实Gateway trace证明source只查询local workspace并经唯一hub对其它spoke做一次有界fan-out，`max_transit_gateways=1`且`max_gateway_hops=2`；workspace未知时hub transit discovery grant只能执行target spoke本地exact lookup，唯一授权目标确定后才另发绑定完整target、origin和path的operation grant。歧义只返回候选数并要求qualified link，未授权与不存在不可区分；nonce在hub/target重启后重放仍被持久registry拒绝。重连轮换channel epoch/route revision时，已提交outbox仍绑定原GlobalThreadAddress和communication/preimage且不重新裸ID选路。默认无规则时核心能力可用；registry故障、wait grant寿命不足或中途撤权均明确失败，恢复权限后下一次实际调用立即生效；cursor/selector不能绕过最新授权，所有拒绝均无目标context副作用

#### Scenario: 物理交错与真实压缩都按逻辑 Item 投影

- **WHEN**E2E用admission barrier让同一child第二个queued Turn root先于第一Turn后续reasoning/tool/final物理提交，并为专用Session在创建另一Turn前通过正常workspace配置/ToolSet API启用`compact_conversation`，再由确定性模型调用该真实产品工具触发context compaction
- **THEN**history API、live DOM和刷新DOM都按各Turn的`logical_item_ordinal`逐项匹配identity、`item_count`和`elapsed_ms`；另一个Turn的root不进入首Turn，canonical `compaction_summary`除该压缩操作自身可能存在并分别计数的tool_call/tool_result外只额外建立一个ordinal，且不伪装成thinking/final或重复carrier
- **AND**测试不得用生产不可达的test-only压缩开关、绕过正常ToolSet启用流程、直接预写projection或按DOM节点反推预期值

#### Scenario: Session 删除与新副作用在 Web E2E 中线性化

- **WHEN**E2E用外置barrier分别让child发布、team fanout、target inbox/execution binding及独立`skill_load`/ToolSet control与同一Session删除竞争，并在workspace catalog已deleting但部分local fence仍active的故障点重启backend
- **THEN**操作先行时只存在捕获旧generation的可恢复lease且删除等待terminal；删除先行时没有新thread/context/inbox/ambient/wakeup/Job/attachment副作用。cold history在topology shared内取得shared `SessionReadGuard`后立即释放topology，原reader尚未完成时删除即可提交catalog deleting并拒绝新reader；已开始的reader可完成，删除等待其guard及全部handle关闭后才关闭local fence并隔离目录；catalog已deleting但部分local fence仍active的崩溃恢复窗口只允许catalog metadata观察，新的thread history/detail和mutation明确`session_deletion_pending`。删除排空期间不持gate，受许可旧lease可短时重取gate完成settlement，随后继续同一删除且不重新active或接受late callback
- **AND**target read/wait在建立`RemoteObservationRecord`前同样竞争gate；删除先行时无snapshot/baseline/subscription，observation先行时删除返回明确target-deleted或完成原冻结结果并等待lease terminal。catalog tombstone后普通resolver不可打开隔离节点，但source丢失response后可在恢复窗口内以同一source call和新授权定点取得相同terminal envelope；窗口结束才物理清理并明确`operation-retry-expired`，不因最长300秒wait遗失恢复事实或静默重开

#### Scenario: Web E2E 验证 copy 的 source snapshot 与删除竞态

- **WHEN**同一pytest模块用barrier分别让本地`full_rollout_copy`停在source capture前/中间，在冻结revision后追加source Turn/view变化，并与source Session删除跑双顺序
- **THEN**target history只包含`SourceCopySnapshot`冻结的SQLite revision、JSONL committed offsets和detail manifest；删除先行时无可见target，shared guard先行时删除只等capture durable，释放后source可删除而copy不回读隔离节点。partial capture定点abort，captured marker到operation CAS崩溃继续同一snapshot，trace证明没有双gate、扫盘补齐或换revision

#### Scenario: Web E2E 验证 pinned retention 与跨端 fork 授权

- **WHEN** 同一产品E2E创建独立detached/pinned target并验证删除双顺序，再以URI选择远端新Session main和现有Session的新child
- **THEN** 本地claim先于capture且恢复/释放唯一；跨端有fork导出/创建授权时成功并在目标端发布，只有send/read/wait grant时明确拒绝且零目标副作用，既有通信行为保持

#### Scenario: Web E2E 验证 copy attachment claim 与工作副本释放

- **WHEN** 来源 Turn 引用可用附件，copy 已建立保留但尚未发布，测试删除来源或触发最后关联释放，并在 claim preparing、owner_reserved 和 publication 后重启
- **THEN** 沿 managed-attachment-directories 的唯一目录 owner 保护原 pin／ingest／holder／claim 与发布边界；同工作区复用原目录身份，跨工作区建立独立目标目录及可靠冻结版本，不按 digest 合并目录。原件可用的正常工作副本释放须按原 URI 重建，有修改或实际占用时保留；required 版本真丢失则不发布，optional unavailable 保留原原因
- **AND** 恢复只收敛原 operation 的预留与关联，不重建旧 blob writer、owner-ref 或 digest claim 回退；来源历史身份与已封存 request_hash 保持

#### Scenario: Web E2E 验证 target 删除与 attachment finalization

- **WHEN** target 已发布但原 claim 尚待 finalization，copy finalizer 与 target Session 删除竞争原 gate，并在两个顺序的 settlement 中途重启
- **THEN** finalizer 先行则结清原 claim 后正常解除目标关联；删除先行则沿原结算链释放本次预留或已提交关联，恢复不复活 target、不隔离含非终态 record 的节点
- **AND** 附件目录按最后关联、实际占用、初始基线与可靠原件决定保留／释放／重建，不连带删除已修改结果或仍被历史和 fork 依赖的冻结版本

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

历史服务和 Turn resolver SHALL 将 `accepted_ingress_id` 与 `acceptance_idempotency_key` 视为两个不同的 thread-local identity，并分别约束 `(session_id, thread_id, accepted_ingress_id)` 与 `(session_id, thread_id, acceptance_idempotency_key)` 唯一；每个 identity 只能一对一指向一个 accepted Turn。相同 ingress、相同 acceptance key、相同 payload hash 和相同 origin branch 的重复 acceptance 只能返回既有 `turn_id`、root 和 initial execution，不创建第二个历史 Turn；同 ingress 被不同 key 重用、同 key 搭配不同 ingress/payload/branch，或跨 thread 直接复用裸 identity 时，必须返回明确 acceptance idempotency conflict，且不修改原 Turn 或历史顺序。跨 owner fork 保留历史 acceptance local ID和原事实，以目标当前归属和冻结历史binding核验；新目标准入不得复用历史key，具体合同消费 checkpoint-context-branching。

#### Scenario: 重复 acceptance 不产生第二个历史 Turn

- **WHEN** 同一 SessionThread 收到相同 `accepted_ingress_id`、`acceptance_idempotency_key` 和 payload hash 的重试
- **THEN** resolver 返回原 Turn 的 root 和 initial execution，历史分页仍只显示一个 Turn

#### Scenario: acceptance identity 冲突不修改历史

- **WHEN** 同一 `accepted_ingress_id` 被不同 acceptance key 使用，或同一 key 的 payload hash/origin branch 不同
- **THEN** 服务返回可诊断的 acceptance idempotency conflict，不创建、重编号或覆盖任何 Turn/item

### Requirement: Internal execution history 使用独立 envelope 和 typed display coordinate

顶层 history envelope MUST 仅在有效 include 包含 `internal` 时填充 `internal_executions: InternalExecutionHistoryDTO[]`；有效 include 与默认 Web 集合以「内部 execution history 由持久 admission 独立加载」为准。每个 `InternalExecutionHistoryDTO` 的字段固定为 `execution_id`、`job_id`、`session_id`、`thread_id`、`status`、`created_at`、`updated_at`、`display_items` 和 `response_items`；不得套用 `TurnDetailDTO`，也不得增加 `turn_id`、root/final Turn 字段或把内部 execution 映射成 synthetic Turn。DTO 的 `session_id/thread_id` 表示该条 durable execution/admission 的真实历史 scope，`execution_id/job_id`、状态和时间也沿该历史事实投影；fork 不将这些字段重写为目标身份。请求路径、顶层 `page.thread_id` 和 cursor 绑定本次读取的目标容器；reader MUST 先验证目标 snapshot 与数据库当前 owner 一致，再以目标本地冻结 namespace binding 校验每条历史 scope，禁止重定向读取来源。再次 fork 可保留更早来源的精确历史 scope，不把所有继承记录强制归到直接来源。`response_items` 只表示该 `execution_id` 下本页命中的内部输出 history，跨页完整性由同一 history cursor 保证，不声明单页已包含该 execution 的全部输出。

`TurnHistoryLoadRequest` SHALL 接受可选 `execution_id`（1–256 字符）以读取同一目标容器中、属于选定 view 的单个 internal execution；继承 execution 仍须通过上述历史 binding 校验。请求 MUST 只使用 `tail` 或 `before`，不得同时提供 `turn_ids`、`tool_call_ids`、`anchor_turn_id`、`turns`、`before_turns` 或 `after_turns`；显式 `include` MUST 包含 `internal`。唯一 history reader MUST 在 SQL/索引边界联合约束目标 Session、thread、选定 view/checkpoint 和 execution，保持原分页硬上限与独立 envelope。非活动轮廓的原 execution 查询继续使用 `TurnCursorDTO.query_mode=execution`；显式 activity_outline 查询使用统一 `query_mode=activity`，其入口与续读约束消费「活动轮廓与 Item 正文回补使用同一有界历史合同」。两种模式均 MUST 冻结精确 `execution_id` 及首次请求的目标 owner/view/checkpoint；续读选择器或归属冲突必须明确拒绝，不切换目标，不启动 runtime。调用方不得用 DTO 的历史 scope 替换请求目标或 cursor owner。原 execution_item_ids 被统一的 item_ids 替换；该定点请求保持单个 execution、最多四项、tail 且无 cursor，沿同一 snapshot 验证成员，不放宽上述禁止混用的 Turn／工具／窗口选择器约束。

#### Scenario: 精确报告读取和有界续页

- **WHEN** 用户从 child 报告引用请求其来源 thread 的 `execution_id`，再以同一选择器和游标向前续读
- **THEN** 每页只返回该 owner/view 中同一 internal execution 的有界内容，runtime 保持 cold，不创建 Turn、Job 或 graph，main 与 sibling 历史不变

#### Scenario: execution 选择器不能混用或改变游标绑定

- **WHEN** 请求混用 execution 与 Turn/tool/window 选择器、使用非 tail/before 方向、显式 include 缺少 internal，或续页更改 execution、Session/thread、view/checkpoint
- **THEN** 系统明确拒绝请求，不返回其它 owner 的内容，也不重新解释为默认历史读取

每个 `display_items` 元素 MUST 使用 `InternalExecutionDisplayItemDTO={kind,text,item_id,item_sequence,logical_item_ordinal,created_at,provenance}`。`item_id` 和 `item_sequence` 必须指向当前 canonical display carrier item 的稳定 identity 与物理坐标；`kind` 为 `generated_session_result`。`provenance` 是 typed source identity object，仅允许有值时出现的 `source_session_id`、`source_thread_id`、`delegation_id`、`source_execution_id`、`source_item_id`，不得携带 run/generator/control ID 或分支正文。普通 Turn history、SSE 与 Web 默认展示不得把这些 provenance ID 当可见文本；Web 对生成回报只展示安全 `text` 和“会话生成”标签，不展示私有 prompt、source branch 正文或可由其推导的内容。

Turn summary/detail MUST 投影必填非负 `root_logical_item_ordinal`，从选定 view 的 `context_view_turns.root_input_item_id` 对应真实 canonical root membership 读取；有可见 canonical 成员的 Turn 可以使用其 hidden backing root 的原坐标，坐标本身不授权显示 root 正文。每个 internal display Item 的必填非负 `logical_item_ordinal` 从同一 view 的可见 display-only membership 读取。两个字段只是既有 `context_view_items.logical_item_ordinal` 的只读投影，不产生第二排序账本。Web MUST 以该共同 view 坐标合并完整 Turn 的 root anchor 和 standalone notice；Turn 自身成员仍按显式 logical membership 展示，不因 notice 插入而拆散或改归属。缺少对应 membership/ordinal 必须明确报 projection 错误，不用活动首尾、物理邻接、墙钟或默认值补顺序。Turn ordinal 与 internal item sequence 仍分别服务于各自分页，不直接互比。

每个 Turn summary/detail MUST 携带本次真实读取快照的 `source_view_id` 和 `source_view_revision`；分页、detail 和客户端镜像合并保留各行自己的 binding，不用页面当前 head 覆写旧行。消息操作首次请求原样携带该 binding 和点击 item，来源已漂移时明确拒绝，不按 item 猜祖先或悄悄选择最新 view。

`source_view_revision` MUST 表示该精确 Session/thread 的 rollout `database_meta.history_view_revision`，不是单个 view 的局部版本，也不是 Turn `revision` 或 `projection_epoch`。同一 owner 的正常写入可以在保持 projection epoch 时将 pinned view 写时复制为 successor；客户端判断迟到快照时 MUST 使用该 owner 的历史版本，不能因 view ID 不同就丢失新旧顺序。不同 owner 的版本不得比较；接受较新快照也不得替旧行伪造新的 binding。相同 binding 的正文丰富度合并与较新 binding 的完整替换仍遵守原合同，旧响应不能回写正文、internal execution 或分页游标。

共同坐标的唯一 owner MUST 是现有 view membership writer：`context_view_items` 以 `(view_id,item_id)` 标识成员，并在同一 view 内为 canonical 与 display-only 成员统一分配非负、唯一的 `logical_item_ordinal`；不得为展示另建计数器。新成员由该 owner 在同一权威事务中追加，已存在成员幂等重入不得重编号。固定 `view_id/history_view_revision` 的读快照中序号与可见性 MUST 稳定；成员变化必须推进 history revision，使旧游标 stale。派生 view 与 rewind MUST 只继承显式选中的源成员、保持其相对逻辑顺序，可在新 view 中重新分配局部序号，不能按物理 sequence 重排已选源成员；fork MUST 保留所选历史item local ID和相对逻辑顺序，并以目标归属/冻结view binding定位目标本地成员；新目标view由owner创建，不能用源物理sequence猜选中范围。

同一选定 view 中，按 `context_view_turns.logical_turn_ordinal` 排列的可见 Turn，其 root membership ordinal MUST 严格递增；membership owner 在建立或变更 view 时维持此不变量。重复 ordinal、缺少 root 成员或两种 Turn 顺序冲突 MUST 明确报 projection integrity 错误，不由客户端 tie-break、重新排序或默认值掩盖。相同 display identity 的分页重影只做幂等去重，若带回不同坐标必须报错。新增 DTO 坐标只投影现有成员顺序，不改变 Provider selection、plan ordering 或执行准入的权威规则。

#### Scenario: 零活动及交错提交使用共同 view 顺序

- **WHEN** 视图包含无可展开活动的 Turn、早于首个 root 的 notice、位于 root 与首个活动之间的 notice，或多个 Turn 的物理 item sequence 交错
- **THEN** history 返回各自真实 membership ordinal，Web live 终态投影与刷新后的 standalone notice/完整 Turn root 次序一致；`item_count=0` 和 diagnostic `first_item_sequence`/`last_item_sequence` 不改变位置，同一 notice 不重复且不成为 Turn 成员

#### Scenario: view 成员顺序完整性与派生

- **WHEN** history 读取派生、rewind 或 fork 后的 view，或数据中出现重复序号、缺少 root 成员、root 顺序与 Turn ordinal 冲突
- **THEN** 合法 view 返回目标局部成员坐标且保持所选成员相对顺序；损坏 view 明确失败，不用物理序列恢复或静默重排；同一 revision 的重复读取与重复分页 identity 坐标一致

#### Scenario: internal history 保持 execution-scoped 且不伪造 Turn

- **WHEN** 客户端显式请求 include `internal` 并读取一次 internal execution
- **THEN** history envelope 的 `thread_id` 标识请求目标，`InternalExecutionHistoryDTO` 保留经目标本地 binding 校验的真实历史 session/thread、execution/job、状态和时间；其中 `display_items` 坐标指向目标本地的真实 runtime notice carrier，`response_items` 只属于同一 execution
- **AND** 响应不含 `turn_id`，不构造 `TurnDetailDTO`、用户 root 或 final response，不改变任何 completed Turn
- **AND** 首屏及 around 的内部执行摘要保留普通思考／工具的轻量活动身份，即使没有模型提供的安全摘要也能展开；未请求思考、参数和结果正文时不打开这些 canonical 正文，不把“没有正文投影”当成“没有活动”

#### Scenario: Web 只呈现安全的生成回报

- **WHEN** Web 渲染 `generated_session_result` display Item
- **THEN** 可见内容仅为安全固定文案和“会话生成”标签；DOM 不包含来源 ID、run/control ID、模型 prompt 或 source branch 正文

### Requirement: 历史字段在类型定义处明确业务时间语义

历史DTO及其内部复制、存储、mapping参数 SHALL 在类型定义或声明处区分实体与事件。`TurnBaseDTO.created_at` SHALL 明确说明所投影的权威来源及事件；不得仅因字段同名就等同于TurnRecord创建时间、首条/root输入消息时间、execution启动时间或复制操作时间。`updated_at`、`completed_at`及活动时长起止同样 SHALL 说明真实投影、回退来源与所属owner；字段名不得暗示不存在的独立终态事件，非终态可有值时须明确说明。时间字段 SHALL 说明序列化格式、时区处理或数值单位，以及复制时保留源事实还是产生目标操作事实。字段名已能明确语义时不要求冗长重复注释。

内部存在歧义的`timestamp`等参数 SHALL 改成明确的主体/事件组合名并全量迁移调用方，或在拥有该字段的类型处补齐含义；DTO字段名保持时 SHALL 提供可被接口消费者读取的语义描述。整理 MUST 保持既有历史时长取值及其真实起止投影、逻辑顺序和复制事实合同，不把存储投影时间差统一称为acceptance到terminal的独立计时，不新增旧名兼容层、别名双写或第二套权威时间。

#### Scenario: 复制保留历史时间并区分新操作时间

- **WHEN** 跨Session复制已有Turn/root及其history，源业务时间与目标复制时间不同
- **THEN** 类型和参数名称或注释明确区分两类时间，目标历史继续按原复制合同保留源业务事实，mapping/journal等新目标操作记录使用自身owner时间，历史时长与顺序保持原合同

#### Scenario: DTO通用时间字段有明确来源

- **WHEN** 调用方仅从Turn summary/detail或internal execution DTO的字段声明及接口描述读取时间语义
- **THEN** 可以明确辨认每个时间属于哪个主体和事件、其权威来源及单位/时区，不需要根据字段名或当前值相等推测，也不把Turn与internal execution的时间混用

### Requirement: 消息正文查询复用 canonical 历史

精确 SessionThread 的消息搜索 SHALL 从当前历史视图可见的 canonical 用户输入和助手输出读取正文，不查询 Trace、前端缓存或另一份完整消息历史。首版 SHALL 明确采用区分大小写的字面量、非重叠匹配；不混入 reasoning、工具结果、受保护正文或内部执行。每页 SHALL 限制扫描条数、读取字节、结果数和执行期限，分别返回覆盖、受限原因、扫描量及 owner cursor；未搜索完成不得伪报完整零匹配。

命中 SHALL 保留 Session、Thread、Turn、canonical item、消息身份和不可变内容版本，位置采用从零开始、右端不包含的 Unicode 码点区间，片段携带自身起点且仅供展示。分页 SHALL 绑定精确 owner、查询、rollout、历史视图及其版本；原视图失效返回明确冲突，不能混合旧页。

#### Scenario: 未加载的 child 消息命中

- **WHEN** 用户查询精确 child 中尚未打开或加载的历史正文
- **THEN** 原 owner 返回该 child 的 canonical 命中及精确 Turn，不读取 main 或依赖浏览器缓存

#### Scenario: 限制与续查

- **WHEN** 查询达到结果、扫描、字节或时间限制
- **THEN** 返回明确受限状态及适用续查，不能以没有匹配表示完整搜索结束；单项超过硬字节上限时明确不可继续该页，不反复返回无法推进的 cursor

### Requirement: 工作区消息查询由后端限定发现与正文预算

工作区范围的消息搜索 SHALL 由原历史服务编排 catalog 与 SessionThread owner 的有界读取，发现 main 与已发布 child，再复用同一 canonical 正文查询。浏览器不得遍历全量 Session 或以已打开标签替代发现。多个 Thread SHALL 共用本页正文条数、字节量和执行期限；目录、Thread 数和结果数分别受限。返回命中 SHALL 保留精确 Thread 的归属、名称与原消息引用，不创建跨 owner 全局正文索引。

续页 SHALL 固定 Gateway、workspace、查询、catalog revision、当前 Session 的 Thread 目录版本和当前历史位置。各 Thread 是其实际读取时的快照，不承诺跨库同一时刻；已完成分支新增消息需重新查询。目录或当前历史失效 SHALL 返回冲突，不能丢弃旧 cursor 后悄悄重查。

工作区消息／工具结果续点的校验 SHALL 携带 `owner=workspace_message_search`、原详细消息和实际校验点的 `reason`，区分 `malformed`、`owner_mismatch`、`query_mismatch`、`category_mismatch`、`position_out_of_bounds` 与 `catalog_revision`。非法形态、归属、查询、类别或位置沿用 HTTP 400 及 `invalid_turn_cursor`；catalog revision 变化返回 HTTP 409 `stale_message_search_cursor`。当前 Session 的 Thread 发布版本冲突沿用 `session_thread_catalog` 的 `stale_thread_catalog`；Thread 内历史失效继续由原历史 owner 发布。原工作区历史入口与统一搜索 SHALL 返回相同领域详情和本次 request_id；Gateway 保留实际状态、输入续点及零交付，并允许显式重查原分支，不自动重放或解析消息文字。

#### Scenario: 未打开 child 的工作区查询

- **WHEN** 用户在所选工作区查询只有未打开 child 才有的正文
- **THEN** 后端发现该 child 并返回精确归属，尚无历史的 child 不被物化，其他工作区不被读取

#### Scenario: 跨 Thread 预算与失效

- **WHEN** 多个 Thread 的累计正文读取达到本页预算，或续查期间目录／当前历史发生变化
- **THEN** 前者返回明确受限状态及原位置续查，后者明确拒绝旧页；均不得伪报完整无匹配

### Requirement: 搜索命中通过原历史读取入口核验

原 around 历史入口 SHALL 接受独立的消息位置引用，在同一读快照核验 item 仍属于指定 Turn、可见视图、正文版本及位置范围，再沿唯一历史分页链加载。该引用不得用于更换 Thread 或推导其它机器路径。

核验成功时，around 响应 SHALL 附带本次定位的一条 canonical 原文及身份、位置引用。消费者在所属 Turn 内临时展示原文并高亮码点区间，等待虚拟列表实际挂载后定位；不得从截断片段或 Markdown DOM 猜测位置。该原文只属于本次导航，不写入时间线缓存或持久展示态，关闭定位即释放。

#### Scenario: 搜索后视图或正文引用失效

- **WHEN** 用户接受的消息已不在目标历史视图、版本不符或位置越界
- **THEN** 返回明确的历史引用冲突，不以当前最新消息、main Thread 或会话顶部替代

### Requirement: 工具结果正文沿同一历史 owner 显式查询

精确 Thread 与工作区历史搜索 SHALL 接受显式 `tool_result` 类别；省略类别仍只搜索用户／助手消息，两者 cursor 不得混用。工具结果 SHALL 从当前可见 canonical item 及其原完整输出引用读取，沿消息搜索同一条链路消费共享条数、字节和期限预算，不查询 Trace 或重放工具。完整输出超出剩余预算 SHALL 明确受限；单项超出硬上限不得返回不能推进的 cursor。

同 Turn、同 ModelCall 的 stream 与 checkpoint 工具结果载体 SHALL 沿原历史详情归并顺序，取 `item_sequence` 最早的可见载体，不固定优先某种载体。保留 canonical item 与 ModelCall 限定调用身份，不按同名工具或裸调用 ID 合并跨执行结果。命中正文位置采用相同 Unicode 码点合同，完整输出的既有版本核对只在同次读取中完成，不新增文件预读。

原 around 入口 SHALL 在同一快照核验工具引用，定点加载原工具详情，临时原文展示于精确结果行。完整输出已删除、改写或位置失效 SHALL 返回历史引用冲突，不展示另一版本，不回退到截断片段或同名结果。

#### Scenario: 完整工具输出中间的命中

- **WHEN** 未打开 child 的工具结果只在已物化完整输出的中间包含查询词
- **THEN** 显式工具类别返回精确 canonical 结果，原详情加载后显示并定位完整原文，消息类别不混入该结果，查询不产生模型或工具执行

#### Scenario: 完整输出失效

- **WHEN** 搜索后原完整输出被删除或被改为同长度的不同内容
- **THEN** 原 around 入口明确拒绝旧引用，保留其他可用历史，不把新正文当作原命中

### Requirement: 工具结果元数据发现沿原历史视图

精确 SessionThread SHALL 提供有界工具结果元数据目录，沿同一历史 owner 的可见载体去重及既有调用索引返回 canonical item、Turn、ModelCall 限定调用身份、名称和状态。目录 MUST NOT 读取 canonical 或完整工具输出正文，也不得触发工具执行。续页 SHALL 绑定原视图及 revision，视图变化沿原历史游标合同明确拒绝；尚未物化的 Thread 返回空目录且不物化历史。

接受候选 SHALL 在原 around 读取中通过独立工具结果 item 选择器核验可见性与 Turn 归属，按原工具调用身份补载并定位同一个 ToolRow，不伪造正文命中区间或工具输出 URI。

#### Scenario: 浏览工具结果后精确打开

- **WHEN** 用户在 child Thread 的资源快速访问中浏览工具结果并接受其中一项
- **THEN** 浏览和筛选只读原元数据，接受后只加载该结果的原调用详情并定位，main 或其它 Thread 不混入，工具不重放

#### Scenario: 历史改变后的续页

- **WHEN** 用户使用先前目录的续点查询，而原历史视图或 revision 已改变
- **THEN** owner 返回明确的失效历史游标，不静默从新视图继续或漏过结果

### Requirement: 上下文标记恢复使用原历史 owner 定点读取

原 history 请求 SHALL 支持 `anchor_context={model_call_id,anchor}`，仅作为单个 Turn 首次 around 或单个 execution 首次 tail 的补充选择，与其它精确目标、正文详情、活动轮廓及 continuation 请求互斥。同一 snapshot SHALL 按请求过程的可见性、原消费请求去重规则和 anchor 类型有界查询至多128项标记元数据，不读取来源正文、不遍历 context_cursor，也不新建标记 Item／URI。该组超限时原 context_cursor SHALL 冻结相同选择、owner、view／revision、上界及续点；续读不得混入后续消费请求，不改变主历史 before／after 游标。确认目标不存在时 SHALL 返回原结构化404，明确 history_locate 和目标来源并保留 request_id；实际查询失败不得冒充目标不可用。

#### Scenario: 同一 Turn 的第二次请求位于首标记页之外
- **WHEN** 一个 Turn 的首次请求已有133项标记，第二次请求又消费新来源，恢复第二次请求的标记
- **THEN** 不续读前133项即可直接返回第二个请求的标记；定点读取首请求的 continuation 也只交付该请求余下元数据，不推进主时间线游标

#### Scenario: 标记属于其它过程或位置类型不符
- **WHEN** 调用方指定不属于当前请求过程的 model_call_id 或不存在的 anchor 类型组合
- **THEN** 同一可见性查询确认无匹配后返回带目标来源的404，不用相邻请求的标记冒充命中

### Requirement: 活动轮廓与 Item 正文回补使用同一有界历史合同

原 history 接口 SHALL 支持活动轮廓与最多四个选中 Item 的受限正文回补，适用于普通 Turn 和内部 execution。轮廓只交付顺序、身份、真实组边界、类型、状态、工具名与已有安全摘要／时间；不得附带完整思考、参数、结果或加密 payload。Item 回补 SHALL 固定同一 owner、history view／revision 并沿原预算验证成员可见性，不推进主历史游标。首次活动轮廓及 item_ids 定点请求 SHALL 在原 TurnHistoryLoadRequest 成对回传已取得页面或 summary 的 source_view_id／source_view_revision，原 reader 在同一 snapshot 校验当前 view／revision；cursor 续读可消费冻结值，显式 binding 冲突、缺一半或版本漂移沿原 invalid／stale 合同拒绝，不悄悄改到当前最新投影。原主时间线初读不增加此详情前置要求，未提交流式 draft 不伪造历史 binding。轮廓首次 SHALL 使用 direction=tail，并互斥选择单个 turn_ids=[id] 或 execution_id；include 显式包含 activity_outline，可加受限 assistant_text，内部执行仍包含 internal。不得混入 item_ids、tool_call_ids、around／anchor 选择器、context_cursor 或主窗口数量选择器。每页最多256个 canonical record，并受原字节预算限制；原 cursor codec 以 query_mode=activity 冻结该 process_ref、目标 owner、view／revision、include、首次成员上界和逻辑 Item 成员续点；不保留 part_ordinal 或 Item 内部 continuation，通过 before_cursor＋direction=before 向前续读。省略 selector／include 使用冻结值，显式不一致必须拒绝；此模式从请求派生，不新增请求 mode 开关、token 格式或 execution 专用 phase。主时间线与活动轮廓游标不能互换，context_cursor 仍只用于上下文 metadata。

item_ids 回补 SHALL 显式限定一个 Turn 或 execution，最多四个不重复真实 Item，direction=tail 且没有任何 cursor；不得混入轮廓续页或其它定位／工具选择器。定点响应同时交付所选成员的真实组首／展示段边界元数据，允许挂载必要标题及目标，不要求遍历轮廓尾页至命中，不将未读区间标作完整。正文受原字节上限约束，超大单项保留原截断事实及既有完整输出／详情入口。超出字节或成员上限必须明确提供适用 continuation／truncation，不伪报完整；不得为获得轮廓而物化整轮正文或扫描 Trace。完整 owner 投影与轮廓／正文片段的应用统一遵守 checkpoint-history-loading 的 「历史与实时共享 Item 内容投影」合同，不以片段整体替换其它已加载成员。

#### Scenario: 长过程展开与单项工具读取
- **WHEN** 一个过程有大量工具和大型结果，用户展开轮廓后仅打开其中一项
- **THEN** 轮廓按预算分页，工具正文只补读选中成员；其它结果字节不进入该响应，Item 回补不改变历史分页位置

#### Scenario: 回补来源或版本不匹配
- **WHEN** Item 属于另一个 Thread／owner，或原 continuation 与当前历史视图不匹配
- **THEN** 沿原历史错误合同明确拒绝，不从来源 Thread 重寻址、不扫描磁盘修补、不返回另一个成员

#### Scenario: 普通 Turn 与 execution 的轮廓同样续读
- **WHEN** 用户展开超过256个 canonical record 的普通 Turn 或内部 execution，并补读更早轮廓
- **THEN** 首次尾页与 before 续页使用同一 activity 模式、真实过程归属及冻结上界，各页保持权威顺序并明确未读范围，不改写主历史 before_cursor／after_cursor 或上下文 context_cursor

#### Scenario: 轮廓与正文响应乱序
- **WHEN** 选中 Item 的正文先返回，而同 revision 的轻量轮廓随后返回
- **THEN** 通过同一 canonical reducer 保留正文及其它成员；原请求覆盖外的成员不删除，已失效 binding 的旧响应不应用

#### Scenario: 显式定位不遍历整个活动轮廓
- **WHEN** 用户跳到一个很长过程早期、尚未加载的 Item
- **THEN** 原 around 核验归属后，通过同一 item_ids 入口定点取得所需正文及真实组／段边界，挂载必要标题与目标；不先逐页拉完整轮廓，不把中间缺页当作不存在

轮廓直接消费既有 Item 索引，不能依赖 response_outline_json、复合 Part 数组或第二份展示索引。真实组首边界由原 owner 有界解析；页中只有部分消息组成员时保留原 ordinal／size，不重编号或从 size 推断工具数量。单个超预算 Item 明确截断并保留既有详情入口，不拆伪 Item 或增加 Part 续页。

### Requirement: Item 附近窗口由原历史快照完成定位

按 Item around SHALL 在原 history 请求的一次 snapshot 中完成可见性、真实归属、坐标解析和有界页面读取，返回实际 located_item 与原 view／revision。anchor_item_id 与 anchor_turn_id 或显式详情选择不得同时生效。内部 execution 附近窗口 SHALL 保持与普通 Turn 和上下文事件的真实逻辑顺序；不得为了定位加载整个 execution 或改变主历史事实。

#### Scenario: 相同 Item 字符串存在于不同目标
- **WHEN** 客户端在固定工作区与 Thread 请求 Item 附近窗口
- **THEN** 只核验目标 Thread 当前可见的真实成员，不跨工作区或来源 lineage 搜索，返回归属和窗口顺序一致

#### Scenario: 定位长内部 execution 的中间成员
- **WHEN** Item 位于一个超过单页上限的内部 execution 中间
- **THEN** 原入口直接返回包含目标的受限窗口和前后继续访问能力，不从尾部反复翻页直到命中

### Requirement: 工具历史保留声明关系和可持久执行摘要

历史与实时 SHALL 分开表达声明来源、结果事实和执行证据。同一原模型消息的调用各为独立 Item，公开 source.message_group 镜像原 message_group_id、ordinal、size；有组时字段完整且 0≤ordinal<size，没有显式组时缺省／null，不以协议默认零值造组。同批由完整 owner、ModelCall 和原消息组共同确认；同一 ModelCall 的不同消息、同一活动组或同一执行流不等于同批。组 ordinal 覆盖文本、思考等全部成员，只保持组内调用的相对声明顺序，不是全局排序或执行顺序。

原执行 owner SHALL 在 invocation 开始和真实返回／异常／确认取消边界冻结观察，沿原 ToolStarted／ToolResultCompletion→commit_tool_result→typed manifest／SQLite 链路持久交付可空 tool_execution 安全摘要，包含原 scoped invocation／attempt／execution 身份、真实历史 session/thread、turn_stream_id、状态／outcome、started_seq、可得 started_at 与 execution_completed_at。当前一个 scoped call 一个 attempt，不新增 attempts 数组、批次表、执行日志、计时服务或逐调用查询。completed_at／completed_seq 保留镜像发布时间／序号含义，不能冒充实际结束；事件消费、提交锁、批量 gather 或结果发布延迟不能延长真实区间。缺原证据明确不可得，不凭 call completed、结果存在、取消请求或恢复清理时间补造结束；存储失败沿原错误合同暴露。

原结果 payload.tool_outcome SHALL 独立经 typed manifest、SQLite 和公共 Item 投影返回，null 与已记录 unknown 分开；它不依赖完整执行时序，不从 completed 推断成功。call 只从同 view 可见、精确关联的结果取得派生摘要，不回写不可变 call canonical；result 保留自己的 Item／原消息来源，不复制 call 的声明组。配对核完整 owner、声明 ModelCall、真实 scoped tool_call_id 及已知 invocation／attempt，不用工具名、到达顺序、Provider 索引或最近候选猜配。模型回退的新 ModelCall 不覆盖旧调用。

默认 tool_summary、activity_outline 及 internal 摘要 SHALL 沿原 snapshot 批量关联本页所需的轻量声明／结果／执行证据，保留 read fence、目标 view 的成员可见性与历史 scope binding；不先取整轮调用、读取参数／结果正文或扫描 Trace。组末 locator 只定位原消息元数据，不要求组末本身可见才提供选中成员的安全摘要。fork／rewind 读取目标已冻结且可见的证据，不回读来源 Thread；清空实时内存后刷新使用相同语义，不靠终态 snapshot 长期驻留。

只有原执行 owner 确认同一可比较 owner／执行流范围、相同时间依据的两个有限真实区间满足 max(startA,startB)<min(endA,endB)，才能说明这两个 attempt 实际重叠。未闭合、缺时间、时间不可比或取消未确认时不猜并行／串行；半开边界相接不算重叠，两两重叠不传递，也不推断依赖。不同声明组可发生真实重叠但不合批，跨范围不能比较；工具调用区间不代替后台资源生命周期。UI 默认无需并行徽标，解释只消费已加载证据，不新增请求、全历史平方级配对或并行缓存。

#### Scenario: 同批先后执行与完成镜像延迟
- **WHEN** 原消息声明 A、B，A 已真实结束但其提交或完成事件延迟，B 随后开始
- **THEN** 刷新与实时仍按原声明顺序显示 A、B，真实区间不重叠，不因镜像延迟、call completed 或同批声明误标并行／成功

#### Scenario: 不同声明组重叠与反序结果
- **WHEN** G1 的 A 与 G2 的 B 在同一可比较执行流中真实重叠，且 B 的回执先提交
- **THEN** 两条调用保留各自声明组和位置，结果按精确调用独立结算；只说明真实 attempt 重叠，不合并 Provider 数组或移动声明行

#### Scenario: 页内只有回执或部分声明
- **WHEN** 页中仅有 B 或 result，另一个 call／组末不在当前可见范围
- **THEN** 保留实际 Item 和已知结果，批量关联只取得授权安全证据，不补占位调用、不读取页外正文、不把组 size 当工具数；补入真实 call 后合行仍保留 result 独立定位

#### Scenario: 取消与未知结果不伪造结束
- **WHEN** 只收到取消请求、进程丢失，或原 owner 确认取消但缺少真实结束观察
- **THEN** 分别保留已知状态、unknown 或 cancelled 结果，缺少的有限区间保持不可得，不将取消请求或恢复时间当作已停止证据

#### Scenario: 迟到摘要与换 view
- **WHEN** 同 binding 已知终态与开始边界后收到迟到 running／缺时间摘要，或响应来自旧 view
- **THEN** 同 attempt 可补缺失真实边界而不倒退终态；旧 binding 不能向新 view 补回已排除结果，清空实时缓存后读取仍取得同样的可见声明与执行事实

### Requirement: 历史失败原因来自原执行终态而不是展示猜测

Turn 历史 summary 与 detail SHALL 以原 rollout convergence 的终态提交为唯一失败来源，failure、execution／Turn 状态和 history revision 在同一原事务提交；消息流仅为传输镜像，不作为历史权威来源。读取 SHALL 沿实际 view／revision 与 terminal execution 的原提交关联定位，精确绑定原 SessionThread、Turn 和执行，不扫描全部 commits 猜最后一次错误。已经接受但尚未产生模型／工具 Item 的失败也必须保留原因。失败 metadata MUST NOT 创建虚构消息、改变 Item 数量或依赖展开详情。仅有状态而没有原因的历史 SHALL 明确表示未记录详情，不猜测工具、Provider 或当前配置原因。读取 MUST NOT 扫描 Trace、日志或全历史进行补写／重建，且须服从原 cursor、字节及延迟预算。

默认紧凑摘要 SHALL 仅为原 failure 的显示投影，不写回 summary／detail、前端缓存或消息流中的失败字段。刷新后 SHALL 仍可沿原按需诊断入口读取／复制已经交付的安全原因及关联，不依赖旧 JS Error／cause、展开 Item 或日志尚未轮转；日志不可用只影响日志导航。原公开预算及截断标记保持，不承诺还原未保存文本，不增加失败详情 API、额外存储／迁移或扫描补读。

#### Scenario: Agent 初始化失败且 Item 为零
- **WHEN** 用户经真实发送入口接受 Turn 后，Agent 工具选项校验失败
- **THEN** 实时和刷新后的 summary／detail 均显示原校验原因及真实归属，Item 保持 0，不显示“后端没有提供可用的失败详情”替代已存在原因

#### Scenario: 原历史没有保存原因
- **WHEN** 原权威记录只有失败／未知状态而没有失败原因
- **THEN** 页面如实显示该记录未保存详情，不伪造失败来源，不读相邻事件补齐，不阻断其它可读取 Turn

#### Scenario: 分页和详情展开后错误保持一致
- **WHEN** 用户读取摘要、展开详情、离开后再按同一历史快照定位失败 Turn
- **THEN** 各入口保留同一已记录原因与归属，正文裁剪不删除失败 metadata，也不把另一个执行的错误附到该 Turn

#### Scenario: 刷新后原日志已经不可用
- **WHEN** 已保存失败原因的 Turn 先以紧凑摘要展示，刷新后原日志已轮转或离线
- **THEN** 用户仍可按需读取／复制 summary／detail 中原预算内的安全原因及关联，原 failure 不变，不要求展开 Item、不用旧内存异常或扫描日志重建；日志动作如实表达不可用

### Requirement: attempt 失败不得替代 Turn 的权威结果

模型或工具 attempt 的失败 SHALL 继续保存在原 attempt 明细；只有原 convergence 确认 Turn 失败时才投影 Turn.failure。成功终态 MUST NOT 因较早失败事件而带上整轮 failure。显式恢复产生新 execution 后，旧失败 SHALL 留在原执行终态提交，当前投影按实际历史快照选择 execution，新活动或成功不得继承旧失败。模型回退、工具继续执行及恢复决策 MUST 保持原规则。

#### Scenario: 模型回退成功
- **WHEN** 模型 A 失败，原回退链选择模型 B 且 Turn 最终成功
- **THEN** A 的失败可在 attempt 明细中查看，Turn 保持成功且不带 failure，刷新后仍不显示整轮失败

#### Scenario: 工具错误后继续完成
- **WHEN** 工具返回 isError，Agent 继续执行并成功完成 Turn
- **THEN** 工具调用保留原错误，summary／detail 不把工具错误投影为 Turn.failure

#### Scenario: 失败执行被显式恢复
- **WHEN** 失败后按原规则创建新 execution，并分别读取旧历史快照和新活动／成功快照
- **THEN** 旧快照保留旧 failure，新快照使用实际所选 execution 的结果，不从最近错误事件继承旧 failure
