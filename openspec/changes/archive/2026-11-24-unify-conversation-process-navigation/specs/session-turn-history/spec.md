## RENAMED Requirements

- FROM: `### Requirement: Web 正式入口支持按稳定 Turn 身份重复跳转`
- TO: `### Requirement: Web 正式入口支持按稳定来源坐标重复定位`

## MODIFIED Requirements

### Requirement: Turn 活动统计与按需中间消息

每个历史 Turn SHALL 显示同一历史 revision 的权威 duration_ms 和 item_count。逻辑活动成员、去重与 elapsed_ms 的唯一计量合同沿本规范的历史摘要和 checkpoint-history-loading，不按分组、组件或隐藏 carrier 重新计量。三层过程展开 SHALL 通过原 SQLite-backed history owner 按层加载：第一层取得有界中间文本与活动轮廓，第二层展示组内轻量标题／状态，第三层才读取所选成员的受限思考、参数或结果正文。未展开组不得因第一层打开而全量详情化，其它 Turn 也不得被顺带读取。零成员不制造展开按钮；失败 metadata 和必要操作入口不依赖详情展开。

非 final 的普通文本 SHALL 作为显式过程成员参与相同计量，不因 Turn 失败、中断或没有 final pointer 而丢失。原 `text`／`assistant_text` 正文选择与默认摘要中的 `final_response` 正文覆盖保持可读，但不改变该部件的 `text` 身份、不升级成 `final_text`；活动轮廓中的普通文本仍遵守原有界正文预算。计数与诊断范围 SHALL 使用同一成员集合，不能出现计数为零但仍携带成员 sequence 范围的响应。

#### Scenario: 仅含普通文本的中断过程
- **WHEN** 一轮执行只留下非 final 文本后被中断，或失败前已产生普通中间说明
- **THEN** 冷启动首读、活动轮廓及 Item 点读保留相同文本部件与真实来源，过程计数非零，独立最终回答保持为空

#### Scenario: 统计行默认折叠
- **WHEN** around 或 before/after 返回有可展开活动的已结束 Turn
- **THEN** 显示统计行和右侧折叠箭头，但不读取或渲染中间 assistant、reasoning、tool_call 和 tool_result 全部正文

#### Scenario: 逐层展开统计行
- **WHEN** 用户先展开过程、再打开活动组、最后展开一个工具
- **THEN** 各步只加载对应缺失的受限投影；工具正文只在第三步取得，其它工具和 Turn 不详情化，重复开关复用已经满足的投影

#### Scenario: 展开统计行
- **WHEN** 用户点击某一 Turn 的统计行
- **THEN** 按三层展开只取得本层缺失的有界投影，第三层才补读目标成员正文；其它 Turn 不详情化，开关复用已取得内容。

### Requirement: Web 正式入口支持按稳定来源坐标重复定位

Web 主窗口的主聊天区域 SHALL 保留用户可操作的“跳到指定轮次”入口，并将其与 Item／Part 搜索、引用和浏览恢复统一到原历史读取与虚拟列表定位链路。Turn 入口允许输入稳定 turn_id 或选择已加载轮次；内容目标使用真实 Item／Part，支持没有 turn_id 的内部 execution。目标 MUST 固定到完整 Gateway／工作区／Session／Thread，展示序号和虚拟列表索引不得作为身份。

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

### Requirement: Turn 是默认分页边界

系统 SHALL 将用户消息、合并 steering、工具活动和模型最终状态组织为完整 Turn；内部消息和工具记录不得把 Turn 拆成独立的主时间线分页项。

默认分页完整性 SHALL 指真实 Turn envelope 与成员归属不被拆散，不代表一页必须交付该 Turn 的所有正文。过程内部的 activity_outline 和选中 Item 正文可以有界续读，响应明确所覆盖成员与未加载范围；不产生伪 Turn、不以详情片段替换整个 Turn，不影响主时间线分页坐标。

#### Scenario: 工具活动保持完整

- **WHEN** 一个 Turn 包含 assistant tool_call、多个 tool_result 和最终 assistant 响应
- **THEN** 任意历史方向的结果都不会拆分这个 Turn

#### Scenario: 大型 Turn 的内部详情分页
- **WHEN** 一个 Turn 的工具活动或中间文本超过详情单页预算
- **THEN** 主时间线仍展示同一个 Turn，内部轮廓与正文分片补读且保留既有成员，未加载内容不被当作已删除或完整交付


### Requirement: Internal execution history 使用独立 envelope 和 typed display coordinate

顶层 history envelope MUST 仅在有效 include 包含 `internal` 时填充 `internal_executions: InternalExecutionHistoryDTO[]`；有效 include 与默认 Web 集合以「内部 execution history 由持久 admission 独立加载」为准。每个 `InternalExecutionHistoryDTO` 的字段固定为 `execution_id`、`job_id`、`session_id`、`thread_id`、`status`、`created_at`、`updated_at`、`display_parts` 和 `response_parts`；不得套用 `TurnDetailDTO`，也不得增加 `turn_id`、root/final Turn 字段或把内部 execution 映射成 synthetic Turn。DTO 的 `session_id/thread_id` 表示该条 durable execution/admission 的真实历史 scope，`execution_id/job_id`、状态和时间也沿该历史事实投影；fork 不将这些字段重写为目标身份。请求路径、顶层 `page.thread_id` 和 cursor 绑定本次读取的目标容器；reader MUST 先验证目标 snapshot 与数据库当前 owner 一致，再以目标本地冻结 namespace binding 校验每条历史 scope，禁止重定向读取来源。再次 fork 可保留更早来源的精确历史 scope，不把所有继承记录强制归到直接来源。`response_parts` 只表示该 `execution_id` 下本页命中的内部输出 history，跨页完整性由同一 history cursor 保证，不声明单页已包含该 execution 的全部输出。

`TurnHistoryLoadRequest` SHALL 接受可选 `execution_id`（1–256 字符）以读取同一目标容器中、属于选定 view 的单个 internal execution；继承 execution 仍须通过上述历史 binding 校验。请求 MUST 只使用 `tail` 或 `before`，不得同时提供 `turn_ids`、`tool_call_ids`、`anchor_turn_id`、`turns`、`before_turns` 或 `after_turns`；显式 `include` MUST 包含 `internal`。唯一 history reader MUST 在 SQL/索引边界联合约束目标 Session、thread、选定 view/checkpoint 和 execution，保持原分页硬上限与独立 envelope。非活动轮廓的原 execution 查询继续使用 `TurnCursorDTO.query_mode=execution`；显式 activity_outline 查询使用统一 `query_mode=activity`，其入口与续读约束消费「活动轮廓与 Item 正文回补使用同一有界历史合同」。两种模式均 MUST 冻结精确 `execution_id` 及首次请求的目标 owner/view/checkpoint；续读选择器或归属冲突必须明确拒绝，不切换目标，不启动 runtime。调用方不得用 DTO 的历史 scope 替换请求目标或 cursor owner。原 execution_item_ids 被统一的 item_ids 替换；该定点请求保持单个 execution、最多四项、tail 且无 cursor，沿同一 snapshot 验证成员，不放宽上述禁止混用的 Turn／工具／窗口选择器约束。

#### Scenario: 精确报告读取和有界续页

- **WHEN** 用户从 child 报告引用请求其来源 thread 的 `execution_id`，再以同一选择器和游标向前续读
- **THEN** 每页只返回该 owner/view 中同一 internal execution 的有界内容，runtime 保持 cold，不创建 Turn、Job 或 graph，main 与 sibling 历史不变

#### Scenario: execution 选择器不能混用或改变游标绑定

- **WHEN** 请求混用 execution 与 Turn/tool/window 选择器、使用非 tail/before 方向、显式 include 缺少 internal，或续页更改 execution、Session/thread、view/checkpoint
- **THEN** 系统明确拒绝请求，不返回其它 owner 的内容，也不重新解释为默认历史读取

每个 `display_parts` 元素 MUST 使用 `InternalExecutionDisplayPartDTO={kind,text,item_id,item_sequence,logical_item_ordinal,created_at,provenance}`。`item_id` 和 `item_sequence` 必须指向当前 canonical display carrier item 的稳定 identity 与物理坐标；`kind` 为 `generated_session_result`。`provenance` 是 typed source identity object，仅允许有值时出现的 `source_session_id`、`source_thread_id`、`delegation_id`、`source_execution_id`、`source_item_id`，不得携带 run/generator/control ID 或分支正文。普通 Turn history、SSE 与 Web 默认展示不得把这些 provenance ID 当可见文本；Web 对生成回报只展示安全 `text` 和“会话生成”标签，不展示私有 prompt、source branch 正文或可由其推导的内容。

Turn summary/detail MUST 投影必填非负 `root_logical_item_ordinal`，从选定 view 的 `context_view_turns.root_input_item_id` 对应真实 canonical root membership 读取；有可见 canonical 成员的 Turn 可以使用其 hidden backing root 的原坐标，坐标本身不授权显示 root 正文。每个 internal display part 的必填非负 `logical_item_ordinal` 从同一 view 的可见 display-only membership 读取。两个字段只是既有 `context_view_items.logical_item_ordinal` 的只读投影，不产生第二排序账本。Web MUST 以该共同 view 坐标合并完整 Turn 的 root anchor 和 standalone notice；Turn 自身成员仍按显式 logical membership 展示，不因 notice 插入而拆散或改归属。缺少对应 membership/ordinal 必须明确报 projection 错误，不用活动首尾、物理邻接、墙钟或默认值补顺序。Turn ordinal 与 internal item sequence 仍分别服务于各自分页，不直接互比。

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
- **THEN** history envelope 的 `thread_id` 标识请求目标，`InternalExecutionHistoryDTO` 保留经目标本地 binding 校验的真实历史 session/thread、execution/job、状态和时间；其中 `display_parts` 坐标指向目标本地的真实 runtime notice carrier，`response_parts` 只属于同一 execution
- **AND** 响应不含 `turn_id`，不构造 `TurnDetailDTO`、用户 root 或 final response，不改变任何 completed Turn
- **AND** 首屏及 around 的内部执行摘要保留普通思考／工具的轻量活动身份，即使没有模型提供的安全摘要也能展开；未请求思考、参数和结果正文时不打开这些 canonical 正文，不把“没有正文投影”当成“没有活动”

#### Scenario: Web 只呈现安全的生成回报

- **WHEN** Web 渲染 `generated_session_result` display part
- **THEN** 可见内容仅为安全固定文案和“会话生成”标签；DOM 不包含来源 ID、run/control ID、模型 prompt 或 source branch 正文

## ADDED Requirements

### Requirement: 上下文标记恢复使用原历史 owner 定点读取

原 history 请求 SHALL 支持 `anchor_context={model_call_id,anchor}`，仅作为单个 Turn 首次 around 或单个 execution 首次 tail 的补充选择，与其它精确目标、正文详情、活动轮廓及 continuation 请求互斥。同一 snapshot SHALL 按请求过程的可见性、原消费请求去重规则和 anchor 类型有界查询至多128项标记元数据，不读取来源正文、不遍历 context_cursor，也不新建标记 Item／URI。该组超限时原 context_cursor SHALL 冻结相同选择、owner、view／revision、上界及续点；续读不得混入后续消费请求，不改变主历史 before／after 游标。确认目标不存在时 SHALL 返回原结构化404，明确 history_locate 和目标来源并保留 request_id；实际查询失败不得冒充目标不可用。

#### Scenario: 同一 Turn 的第二次请求位于首标记页之外
- **WHEN** 一个 Turn 的首次请求已有133项标记，第二次请求又消费新来源，恢复第二次请求的标记
- **THEN** 不续读前133项即可直接返回第二个请求的标记；定点读取首请求的 continuation 也只交付该请求余下元数据，不推进主时间线游标

#### Scenario: 标记属于其它过程或位置类型不符
- **WHEN** 调用方指定不属于当前请求过程的 model_call_id 或不存在的 anchor 类型组合
- **THEN** 同一可见性查询确认无匹配后返回带目标来源的404，不用相邻请求的标记冒充命中

### Requirement: 活动轮廓与 Item 正文回补使用同一有界历史合同

原 history 接口 SHALL 支持活动轮廓与最多四个选中 Item 的受限正文回补，适用于普通 Turn 和内部 execution。轮廓只交付顺序、身份、真实组边界、类型、状态、工具名与已有安全摘要／时间；不得附带完整思考、参数、结果或加密 payload。Item 回补 SHALL 固定同一 owner、history view／revision 并沿原预算验证成员可见性，不推进主历史游标。首次活动轮廓及 item_ids 定点请求 SHALL 在原 TurnHistoryLoadRequest 成对回传已取得页面或 summary 的 source_view_id／source_view_revision，原 reader 在同一 snapshot 校验当前 view／revision；cursor 续读可消费冻结值，显式 binding 冲突、缺一半或版本漂移沿原 invalid／stale 合同拒绝，不悄悄改到当前最新投影。原主时间线初读不增加此详情前置要求，未提交流式 Part 不伪造历史 binding。轮廓首次 SHALL 使用 direction=tail，并互斥选择单个 turn_ids=[id] 或 execution_id；include 显式包含 activity_outline，可加受限 assistant_text，内部执行仍包含 internal。不得混入 item_ids、tool_call_ids、around／anchor 选择器、context_cursor 或主窗口数量选择器。每页最多256个 canonical record，并受原字节预算限制；原 cursor codec 以 query_mode=activity 冻结该 process_ref、目标 owner、view／revision、include、首次成员上界和成员续点（需要时含原 part_ordinal，不能因字节预算跳过同一 Item 的其它部件），通过 before_cursor＋direction=before 向前续读。省略 selector／include 使用冻结值，显式不一致必须拒绝；此模式从请求派生，不新增请求 mode 开关、token 格式或 execution 专用 phase。主时间线与活动轮廓游标不能互换，context_cursor 仍只用于上下文 metadata。

item_ids 回补 SHALL 显式限定一个 Turn 或 execution，最多四个不重复真实 Item，direction=tail 且没有任何 cursor；不得混入轮廓续页或其它定位／工具选择器。定点响应同时交付所选成员的真实组首／展示段边界元数据，允许挂载必要标题及目标，不要求遍历轮廓尾页至命中，不将未读区间标作完整。正文受原字节上限约束，超大单项保留原截断事实及既有完整输出／详情入口。超出字节或成员上限必须明确提供适用 continuation／truncation，不伪报完整；不得为获得轮廓而物化整轮正文或扫描 Trace。完整 owner 投影与轮廓／正文片段的应用统一遵守 checkpoint-history-loading 的 response part 合同，不以片段整体替换其它已加载成员。

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
- **WHEN** 用户跳到一个很长过程早期、尚未加载的 Item／Part
- **THEN** 原 around 核验归属后，通过同一 item_ids 入口定点取得所需正文及真实组／段边界，挂载必要标题与目标；不先逐页拉完整轮廓，不把中间缺页当作不存在

### Requirement: Item 附近窗口由原历史快照完成定位

按 Item around SHALL 在原 history 请求的一次 snapshot 中完成可见性、真实归属、坐标解析和有界页面读取，返回实际 located_item 与原 view／revision。anchor_item_id 与 anchor_turn_id 或显式详情选择不得同时生效。内部 execution 附近窗口 SHALL 保持与普通 Turn 和上下文事件的真实逻辑顺序；不得为了定位加载整个 execution 或改变主历史事实。

#### Scenario: 相同 Item 字符串存在于不同目标
- **WHEN** 客户端在固定工作区与 Thread 请求 Item 附近窗口
- **THEN** 只核验目标 Thread 当前可见的真实成员，不跨工作区或来源 lineage 搜索，返回归属和窗口顺序一致

#### Scenario: 定位长内部 execution 的中间成员
- **WHEN** Item 位于一个超过单页上限的内部 execution 中间
- **THEN** 原入口直接返回包含目标的受限窗口和前后继续访问能力，不从尾部反复翻页直到命中
