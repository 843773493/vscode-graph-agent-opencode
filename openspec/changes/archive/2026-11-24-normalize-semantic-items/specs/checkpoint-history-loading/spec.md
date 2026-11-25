## REMOVED Requirements

### Requirement: 历史与 live 使用同一 Turn response part 模型

**Reason**：复合 Item 再展开 Part 造成两套持久内容身份，由统一语义 Item 和其安全投影替代。

**Migration**：将历史／实时 DTO、protobuf、前端 reducer、详情缓存和渲染消费者一次迁到 Item 投影；删除旧字段、Part ordinal 及转发别名。Provider 原协议来源不因同名而删除。

## ADDED Requirements

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

## MODIFIED Requirements

### Requirement: 前端历史定位不暴露 View 实现

系统 SHALL 让前端通过真实 `turn_id` 或 `item_id` 定位历史；无 Turn 内容保留真实 execution／Item 归属，不伪造 Turn。view、branch、checkpoint 和 message range 不成为客户端阅读锚点；后端提供的读取／操作 binding SHALL 按原合同原样回传和校验，不要求前端解释 view 图。Item 跳转不附加通用 Part ID，fork／rewind／replay 仍由原操作 owner 判断边界能力。

#### Scenario: 同一个 Turn 出现在多个 view
- **WHEN** append、rewind、fork 或 compaction 后打开稳定 Item 引用
- **THEN** 原 owner 核验当前 view 的真实成员、scope 和可见性后定位；客户端不维护 Item→多个 view 的第二事实源，不将不在所选 view 的 Item 冒充存在

#### Scenario: 内容定位不依赖 Turn 存在
- **WHEN** 用户点击 internal execution 中的 Item URI 或搜索结果
- **THEN** 原 Thread 的 around 入口有界加载该 Item，正常进入阅读位置控制器，不改开原文或等待不存在的 turn_id

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

## REMOVED Requirements

### Requirement: 正常历史 reader 只服务 v2，v1 仅用于一次性 migration/import

**Reason**：正常运行目标改为统一语义Item的格式3；用户已确认退出未发布格式1／2的转换支持。

**Migration**：删除未发布 v1→v2 importer、专用 staging／report 和旧 reader，保留原件；其它有序 SQLite 结构迁移仍归原 owner，不借此改写真实 Home 或只改版本号。

## ADDED Requirements

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
