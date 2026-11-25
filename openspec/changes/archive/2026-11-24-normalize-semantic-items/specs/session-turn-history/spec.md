## MODIFIED Requirements

### Requirement: Rollout 是聊天历史的唯一权威来源

系统 SHALL 直接从会话 rollout 的 `index.sqlite` 和 `rollout.jsonl` 读取聊天历史。正常读取只接受 canonical 格式3语义 Item 与原 storage owner 登记的当前 SQLite 结构；本轮集成目标为 schema34，不将早期未发布的 schema31／32／33 当作兼容来源。元数据和 JSONL envelope 必须一致，旧版、未知版本或未完整发布的来源明确拒绝并保留原件，不运行已退出支持的 v1→v2 importer。上下文边界由 SQLite context view 表达，不创建物理 segment。Trace、日志和旧 turn projection MUST NOT 作为 `/bootstrap` 或 `/history` 的数据源，也不得在 rollout 缺失时静默回退。

#### Scenario: 旧 Trace 不能伪造历史

- **WHEN** 会话只有旧 Trace 文件而没有有效 rollout
- **THEN** 历史 API 返回可诊断的 rollout 缺失或损坏错误，不返回旧投影中的 Turn

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

### Requirement: 历史 cursor 必须校验精确快照版本

历史 cursor SHALL 复用 checkpoint-history-loading 的唯一 v3 复合游标合同（逻辑 Item 续点，不保留 Part 坐标或旧版本解码），绑定精确 SessionThread、view、history_view_revision、projection epoch、查询选择器与逻辑位置。成员变化、普通追加或终态更新推进该历史版本时旧 cursor MUST 明确 stale；不得把 token 内部推进阶段当成按滚动次数增加页面批次。rewind、branch 切换和删除同样不得静默切到新历史。

#### Scenario: rewind 后使用旧游标

- **WHEN** 客户端在 rewind 后提交旧游标
- **THEN** API 返回可识别的 stale-cursor 错误，不静默返回另一条 branch

#### Scenario: 同 epoch 的历史版本改变

- **WHEN** 同一 view 或 successor 在 projection epoch 不变时因追加或终态更新推进 history_view_revision
- **THEN** 旧 cursor 明确失效，客户端沿原刷新和定位入口重新读取，不混合两个历史快照

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

## ADDED Requirements

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
