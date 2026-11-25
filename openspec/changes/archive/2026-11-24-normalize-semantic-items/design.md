## Context

动机见 proposal。以下以编写时的根源码为依据。

| 位置 | 已有实现 | 本次需要统一的点 |
|---|---|---|
| `checkpoint/message_codec.py:items_for_message` | 消息构造成 primary，再调用工具消息展开 | 无工具的结构化输出也必须走同一语义拆分 |
| `checkpoint/codec/groups.py` | 拆工具伴随 content 和多次调用；组末 anchor 只允许 tool_call | 通用消息组支持 assistant 内容组；移除工具专用分支 |
| `mapping/itemized/canonical_langchain.py` | 从 canonical 组恢复临时消息、压缩来源 | 与 checkpoint codec 使用同一分组语义，保护协议形状 |
| `mapping/turn_response_parts.py` | 把复合 Item 再展开 Part，分配 ordinal 和轮廓 | 已提交内容直接投影 Item，不再二次生成持久身份 |
| `storage/catalog` 与 SQL | `response_outline_json` 被展开为 Part 坐标，活动分页含部件位置 | 改为原 Item 投影和逻辑 membership 坐标 |
| `web/state/timeline/transcriptNodes.ts` | Item source 同时要求 `id` 和 `part` | Item source 只保留 item_id；展示 slot 与内容内位置由客户端持有 |

已有 main specs 仍允许 content-part 操作锚点；前置变更 `unify-conversation-process-navigation` 进一步依赖 Part 轮廓、组首 Part 和阅读格式2。本变更是对这些合同的后续替换，不另建一套并行 timeline。

## Goals / Non-Goals

**Goals:**

- 一份 canonical 语义拆分规则贯穿持久写入、上下文投影、历史、实时提交与恢复；是否带工具调用不影响内容 Item 粒度。
- 一个已提交可展示内容对应一个 Item 身份；一个 Item 可以有正文、摘要、受保护存在性等多种视图，不因此生成多个业务身份。
- 普通定位和活动分页不依赖 Part；Provider 协议仍能无损恢复。
- 删除不必要的轮廓存储、二次排序、去重、临时到最终 Part 别名和重复查询。

**Non-Goals:**

- 不改工具权限、审批、团队协作、Gateway 职责或常驻连接机制。
- 不把 JSON 任意字段、Markdown 段落、代码行、token／delta、工具结果中的每个嵌套块都变成 Item。
- 不重做用户输入接受事务、附件上传／GC、压缩算法或 Provider 网络库；不为了本变更新增第三方 Provider 解析框架。
- 不在不同客户端之间同步阅读位置；不为全部未来扩展预建 Part 注册服务或通用迁移调度器。

## Decisions

### 1. 以可独立识别的语义内容为 Item，结构化不等于复合消息

`semantic_kind` 仍表达业务类别，`payload_kind` 表达载荷形式。保留现有闭合枚举，优先复用；不新增与 Item 同义的 ContentItem、Segment 或 PersistentPart。

| 输入情形 | canonical 表达 | 保留在载荷／协议边界的内容 |
|---|---|---|
| assistant 顶层 thinking、text、image 有序块 | 各自独立 Item：reasoning、assistant_output、assistant_output | 图片 Item 可用单块 structured_content，保留原媒体引用；不把图片当文本，也不为此强制下载媒体 |
| 同一消息的多次工具调用 | 每次调用一个 tool_call Item | 真实调用 ID、参数、ModelCall scope |
| 一次工具返回文本＋图片等多个结果块 | 一个 tool_result Item | 这是一个调用的完整回执；内部 rich payload 不自动成为独立调用结果或新定位合同 |
| 一份 reasoning carrier 含签名、summary、密文 | 一个有独立来源的 reasoning Item | 同一 carrier 的保护字段及其安全视图；不把签名或 summary 再拆成独立思考事实 |
| 用户提交文本＋附件 | 保持唯一 user_input root 和原输入／附件合同 | 不为媒体块再创建 user root；显式 attachment Item 继续由原附件 owner 决定 |
| 单条 runtime notice、压缩摘要 | 保持原单项业务身份 | 结构化字段不是独立消息 |
| 未知 Provider 内容块 | 沿原 typed extension／opaque 合同保留或明确拒绝 | 不 stringify、丢弃或将受保护数据降级为 text |

顶层输出块的顺序在 writer 处冻结；同类型但来源不同的两个文本块仍为两个 Item。一个流式块的多个 delta 只更新同一 draft；不按到达次数生成 Item。不按内容相同去重。

替代方案“继续保留复合 Item、优化 Part 索引”被否决：它不能消除两套定位身份。方案“所有 payload 递归拆到底”被否决：它破坏回执、输入和保护边界。

### 2. 通用消息组只保存协议归属，不拥有第二套展示成员

沿原 `message_group_id`、`ProjectionGroup`、typed projection identity 记录原消息的有序成员、str／list 形状、原角色、ModelCall 和必要 wire 信息。将现有工具专用组校验与重建推广到普通 assistant 输出，不增加另一套 message-group 表或 Part manifest。

- `ProjectionGroup.size>=1`；多成员记录有序关系；单成员只在原正文形态不能由自身payload表达时保留显式 `content_form`（如一次纯工具调用的空 `[]` 不得变为 `""`）。已有原子流式block不被强制补成一个完整消息组，不为记住形态补空内容 Item。组末 anchor 不再被限制为 tool_call；每个 Item 有唯一 ID，消息组不是用户阅读锚点。
- 原消息 ID、终止原因和 usage 等消息级事实沿现有组 anchor 归属保存，不复制到每个成员重复计费。
- 一个已拆开的语义块可保留原 block 类型与 ID／index，供协议还原；这些来源坐标不成为历史定位或前端持久身份。
- 同组、同模型调用、原角色以及显式成员关系共同约束重建；不能按邻接、相同正文或相同 role 合并无关消息。
- 空消息、纯工具消息、媒体输出、invalid_tool_calls 和受保护 reasoning 均沿原实际状态保存，不为凑组补空文本 Item。invalid_tool_calls 属于原消息的无效声明信息，沿唯一组anchor的typed协议payload保留（内容以原content wrapper、工具以原调用payload、思考以原carrier envelope承载），不把无效参数放入无正文的projection manifest，不复制到每个内容Item或计为可执行tool_call；同一消息同时有有效／无效声明也须往返保持。
- 一组在原提交事务中完整发布，size只描述实际已持久成员；中断时按原draft收敛规则保留真实partial内容，不把尚未产生的块算进组大小。流式来源block的原index不要求连续，不能拿Provider index冒充组ordinal。
- 组内 canonical 校验在写入／转换与实际 group 重建时执行；暖历史摘要不读取整组正文做往返自检。

复用现有 normalizer／codec 叶映射，迁齐 `items_for_message`、`project_message`、`project_canonical_items`、summary-source 和 Provider request 编译，不在不同入口再写一套拆分器。调用方可以请求不同投影，核心语义规则只有一份。

### 2.1 工具声明的批次和顺序不等于实际调度

本节的最小合同是：**一个调用一个 Item，声明关系随来源保存，执行关系随真实 attempt 保存；前端合行仅改变展示。** 三类关系不能互相替代：

这里的“同批”精确指同一条原模型消息中的声明，不泛指一次 ModelCall、一个 Turn 或一个前端活动组。一次 ModelCall 若产生多条原消息，各消息的调用数组分别拆分、分别重建；即使调用执行重叠，也不能合成一批。验收须同时经过真实 writer、公开摘要序列化、实时提交／重连和清空实时内存后的历史读取：原消息归属与声明行序一致，执行关系只随当前可见的真实证据变化，不能只验证 codec 能还原数组。

| 要回答的问题 | 使用已有合同 | 不新增的机制 |
|---|---|---|
| A、B是否同批、谁先声明 | 完整owner＋ModelCall＋原消息组，组内ordinal | 独立batch实体、调用顺序表 |
| A、B是否实际重叠 | 精确配对的attempt及可比较真实执行区间 | 持久parallel／serial标记、依赖图 |
| 刷新后能否恢复同样展示 | 默认历史的安全来源与执行摘要，及当前view可见性 | 前端记忆原数组、逐调用补查、Trace回放 |

一个 `semantic_kind=tool_call` Item 只声明一次调用。沿用已有 payload schema：若使用 `tool_calls` 字段，它必须恰含一个有效调用；不得将多调用数组藏入 `structured_content` 或扩展字段。参数内部的数组、批处理参数不受此规则递归拆分。Provider 原响应允许多个调用，拆分在原 codec 完成，重建原 assistant 消息时才按组内调用成员顺序恢复 `tool_calls=[A,B,…]`。不为本次改动另创 singular payload、批次实体或执行调度器；invalid_tool_calls 沿原无效声明合同保存，不成为可执行调用。

流式 `tool_calls` 的单个网络片段可能携带多个调用的增量，也可能先收到B的参数、后收到A的参数。原adapter按Provider提供的调用来源与索引累计各自draft，声明顺序使用协议明确给出的来源顺序，不使用片段到达顺序；Provider索引只在该协议边界使用，不直接充当Item身份或`ProjectionGroup.ordinal`。尚无足够来源信息时沿原draft规则等待关联，不猜调用ID、占位Item或组大小；没有明确原始顺序的协议不得由前端补猜。参数尚未完整的draft不因另一个调用完成就变成可执行声明，终止时继续沿原partial／invalid合同处理。

消息组封存不是工具执行屏障。一个调用已经满足原调度器的启动条件时，本次拆分不能额外要求它等待同组其它声明参数完整、整组canonical提交或其它回执；原调度器若本来要求完整模型响应，仍遵守该条件，不为此新增流式提前执行。执行可以早于声明组封存，前端沿原draft来源消费已确认的执行状态，提交后沿真实来源收敛到调用Item；不因为尚无组size／ordinal就扣留执行证据，也不凭执行先后重排声明或提前生成组元数据。组完整发布约束只针对canonical消息组事务，不扩展成跨声明、执行、回执的整批事务。

| 信息 | 唯一来源和表达 | 展示含义 |
|---|---|---|
| 模型提出的同批调用 | 完整 owner＋ModelCall＋原消息组的显式 membership；单成员沿原 Item 来源 | 同一响应消息提出；不能只凭 model_call_id 合并一次流中的不同消息 |
| 原声明顺序 | `ProjectionGroup.ordinal` 包含正文／思考等全部成员，筛出 tool_call 后保持相对顺序 | A、B 的稳定展示顺序；不用完成顺序、event 到达顺序或数组下标生成持久身份 |
| 实际执行身份 | 原 tool_invocation_id、tool_attempt_id、tool_execution_id 及 scoped tool_call_id 关联 | 同名工具、Provider ID 跨 ModelCall 重用、重试均不误配；Item ID 仍为阅读身份 |
| 实际执行区间 | 原执行 owner 的 ToolStarted 开始证据及真实完成观察；完成事件的序号／时间独立保留其镜像含义 | 能证明是否发生执行重叠；ToolCall 起止是声明生成，ToolCompleted 发布也可能晚于真实完成 |

这里的执行区间只描述该次工具 invocation／attempt，不自动覆盖它启动的后台任务、终端进程、浏览器资源或委派任务的生命周期。工具若返回 job／资源引用后继续后台运行，调用完成只表示本次调用已返回，调用的 success 也不代表后台任务成功；后台对象仍沿其原 owner 的状态与定位入口展示。不将其生命周期塞入 tool_execution，不延长调用区间，不为本变更增加后台任务订阅或统一跨服务时间线。关系说明应明确是“工具调用执行重叠／先后”，不能将调用区间不重叠说成后台工作没有并行。

原消息组、前端活动组和执行流有不同边界：原消息组回答“哪些成员由同一消息声明”，活动组可以容纳多次消息／ModelCall，执行流只提供执行事件的比较范围。不得用活动组、Turn或turn_stream_id相同来证明同批，也不得把同一活动组内的多个原消息合成一个Provider调用数组。若原请求包含parallel_tool_calls等选项，沿原协议边界保留其含义；它不证明调度器实际同时执行了调用，不作为UI并行证据，也不因Item拆分改变调度配置。

“同批”和“执行重叠”是独立关系：比较真实执行区间不以同一个message_group_id为前提。不同原消息甚至不同ModelCall的调用，只要原执行owner确认属于同一可比较执行范围且真实有限区间重叠，也可以说明这两个执行发生重叠；它们仍属于各自的声明组，不能因此合批、改写Provider消息或调整调用行位置。来自不同目标、工作区或不可比较执行流的时间数字即使相交，也不能据此声明并行。

组内多次调用保持原 Provider 调用 ID和真实来源；已提交投影使用当前 view 的逻辑 membership，原组 ordinal 只还原声明关系，不覆盖 fork／rewind 后 view 的可见性与顺序。工具行可以将 call／result 合并显示，按 call 的位置固定；回执仍保留自身 Item、真实来源及逻辑坐标，不把结果从 canonical 历史重排到调用位置，也不重复展示同一结果。单个回执到达不代表其它调用已完成，call Item 的 `status=completed` 只表示声明完整，不能据此把工具标成执行成功。

排序层次固定为：当前view的权威逻辑成员顺序决定不同原消息的位置，原消息内的调用保持显式成员顺序；`ProjectionGroup.ordinal`只在自己的原消息组内有意义，不是全局排序键。不同组都可以有ordinal=0，不能先收集全部工具再按ordinal、开始时间或完成时间重排。实时尚未提交的声明仍沿原adapter已确认的来源顺序，提交时以真实映射接续，不在全局重排已有内容。

分页只取得result而没有对应call时，保留result自身的Item引用、位置和已知结果；没有足够关联信息时不猜配，也不为页外call补占位行。随后补入同view内真实call时，沿原call／result合行规则更新展示，不能重复呈现同一回执或让result的URI改指call。合行后的call主定位和result独立定位均由原节点入口解析：从result跳转必须能定位其真实回执展示部位，不能仅因result未单独占行就报不存在。工具行相对call的位置不改变底层result的逻辑坐标。

调用行不依赖执行实例已经创建：声明完整但尚无ToolStarted时即可按原声明来源出现，执行身份与证据随后附到对应调用。已提交行以真实call Item身份定位，开始、完成及call／result合行不改用attempt／execution ID作为内容身份；未提交时仍沿原draft到提交关系收敛，不另造占位Item或持久别名。尚无执行摘要与确实缺失声明关联是不同情况，不能因为tool_execution为空就删除已有调用行。

并行只由同一可比较执行范围内两个真实 attempt 的运行区间重叠证明：对两个已知起止区间，`max(start(A),start(B))<min(end(A),end(B))`；终止边界使用半开区间，A结束后B才开始不算重叠。未闭合区间、普通实时快照内同时出现两个running、取消请求或缺终态均不足以证明重叠；本变更不为运行中贴并行标签新增探测、心跳或时钟服务。执行范围沿原完整owner＋turn_stream_id（无Turn时沿实际execution的流身份）核验；起止必须使用同一可比较时间依据，不能将时间与event_seq混比。completed_seq描述完成镜像的发布位置，不替代较早冻结的完成观察；跨范围的序号、客户端收包时间、canonical created_at 和声明完成时间不能推断调度。缺证据、时间不可比或起止倒置时不标并行／串行，不修补或倒推时间；字段非法／存储损坏仍走原错误出口。重叠仅证明被比较的 attempt，不将整组所有调用或重试一概标为并行；非重叠也不证明存在依赖链，不展示猜测的串行依赖。

状态复用执行 owner 的值域；没有排队事实时只表示已声明／尚未观察到执行，不伪造 queued。当前每个scoped call只允许一个attempt；模型回退／重试产生新ModelCall和新声明时，保留各自调用Item、执行身份与失败结果，不将新调用附成旧调用的第二attempt，也不覆盖旧终态。同一声明的重复通知不复制call Item。结果必须以原 scoped 调用和 attempt 配对，无法确认时沿原未知／待关联合同处理，不能按工具名、最近调用或到达顺序匹配。

取消沿同一执行 owner 区分请求和结果，不新增取消状态机：

| 原执行事实 | 结果／时序投影 | 前端允许表达 |
|---|---|---|
| 仅发出取消或中断请求，尚未确认实际停止 | 不因此生成 `cancelled` 结果或 `execution_completed_at` | 保留原已知执行状态；请求本身不证明结束或非重叠 |
| 执行 owner 确认取消，且已观察到实际结束 | 执行 outcome 保留 `cancelled`，原结果 marker 为 `cancelled`；真实结束沿 `execution_completed_at` 交付 | 已取消；有完整可比较区间时仍可判断此前是否与其它执行重叠 |
| 断连／进程丢失后无法确认实际结果 | 执行 outcome 沿原 `outcome_unknown` 等值域，结果 marker 为 `unknown`；不补造实际结束 | 结果未知；不能因请求取消或恢复清理就显示已停止 |

取消结果与结束时序仍独立：只有确认取消的结果而缺真实结束观察时，保留 `cancelled`，只将运行区间标为不可得。执行 outcome 与结果 marker 沿原映射统一穿透 Python DTO、protobuf／生成客户端、实时快照和历史摘要，不把 `cancelled` 降为成功、一般失败或未知。单个调用取消不自动结算同批其它调用或整个 Turn，过程终态仍由原 owner 决定。

刷新所需的声明关系和最小执行摘要从原历史 owner 返回，不能依赖终态 realtime snapshot 的长期驻留。已有 tool projection 的 started_at／completed_at 必须先核其 writer 的实际含义；若来自声明时间，不得改名冒充执行时间。真实执行 owner 在原终态提交／恢复边界发布可持久读取的 attempt 身份、状态和实际起止证据，原 history reader 将必要字段接入已有工具投影；复用现有存储原语，不另建执行事件日志、展示缓存或向 Trace／SSE 回放查询。已有历史无该证据时保留“未知／不可得”，不能倒推补造。有效证据未写入或存储故障与明确不可得分别处理，不能吞错误。

实施落点明确为原 `MessageStreamToolCallsMixin.start_tool/complete_tool` → `ToolResultCompletion` → Saver 的 `commit_tool_result` → 原 catalog／history 投影，不新增跨业务收敛器。保留现有“canonical 结果先提交，tool.completed 事件随后镜像”的顺序：执行 owner 在真实边界冻结起止观察，随原终态提交交给持久化 owner；尚未分配的 completed_seq 不得提前猜号。若事件晚于结果发布，区分真实执行完成时刻与事件镜像写入时刻；两者不能混成同一计时。进程丢失或结果未知只能保留已确认的 start，不把清理／恢复时刻登记为实际停止时刻。开始证据取原 ToolStarted，终态证据必须来自真实完成观察；原恢复只可复用已经冻结的证据，不回放 Trace 或补猜缺口。

源码核对：现有`message_stream/projection.py`的生命周期投影将事件`emitted_at`写入`completed_at`，它是镜像发布时刻。保留这层含义；本变更用可空的`execution_completed_at`命名实际完成观察，随原结果提交持久化并用于新历史执行摘要。实时投影消费同一冻结值，不以`completed_at`回填它；canonical先提交时尚无completed_seq属于正常未分配，不因补齐此字段再更新不可变Item。缺观察值时不具备有限执行区间，不能把旧字段换个名称当作已经取得证据。

冻结边界还必须早于异步消费造成的延迟：沿原工具调用 owner／callback，在实际 invocation 开始与返回／异常／确认取消的边界取得观察值，再经原事件和提交路径传递；不能在事件消费者取到通知、取得完成锁、记录文件更改、发布事件或等待整批 `gather` 后逐条处理结果时调用当前时间冒充执行边界。实施前的 `complete_tool` 入口取时方案不足以单独证明真实完成，因为 `processor.py` 在调用它前可能等待其它处理；源端观察切片已替换这一采集点。后续路径同样须贯通原执行来源冻结值，无法取得可信来源则保留不可得，不新增计时服务、独立事件流或定时补测。执行异常只在原 owner 已确认 invocation 结束时形成结束观察；异常被较晚消费不改变该观察，也不从取消请求补造停止时间。

源端观察通过原LangChain inline callback在流通知发布前完成；等待durable start的原异步callback仍独立等待，不在inline中等消费者以免死锁。开始观察沿原callback身份交接传给`ToolStarted.started_at`，`started_seq`仍是镜像发布序号；完成观察更新原active attempt并随结果提交，`complete_tool`只消费已冻结值。无可信开始来源的工具镜像不以`emitted_at`补出`started_at`，恢复同样保持不可得。这个交接只替换原callback binding中的值，不新增计时表、关系缓存或执行状态owner。

历史 Item 的声明来源沿现有 typed manifest 给出 `message_group_id` 及 `ProjectionGroup.ordinal/size`；实际执行沿原工具投影提供可空的单个 `tool_execution` 安全摘要，对齐既有 `ToolExecutionSnapshot` 的真实身份、状态／outcome、流范围和可得起止字段。canonical Item status 与该执行 status 分开消费。该证据由原 typed projection manifest 镜像以供 SQLite 摘要批量读取，不把完整参数、结果或原始事件塞进 manifest；不存在证据时返回不可得，不凭结果存在补造时间。当前运行时只允许一个 scoped call 对应一个 attempt，这个约束保留；本变更不为展示新增同调用多 attempt 支持、重试调度或 attempts 数组。模型回退／重试产生的新 ModelCall 仍保留各自原调用和终态，不能合并为同一次工具执行。未来若原 owner 正式开放多 attempt，再由原详情分页承接，不能在这里先实现一套备用路径。

公开来源用 `source.message_group={message_group_id,ordinal,size}` 镜像该原始关系，与同一source的ModelCall及owner共同使用；没有显式ProjectionGroup时不强造该对象。实时未提交调用沿原事件信封的model_call_id和可得的provider_call_index保留来源顺序，快照保留这两个可空字段；provider_call_index只作协议来源，不能当Item身份、组ordinal、执行顺序或同批证据，不从它创建另一份持久调用顺序表。

`source.message_group` 是可空的完整对象：存在时三个字段必须同时可用，`message_group_id` 非空，`0 <= ordinal < size`，且 `size >= 1`。它镜像原组，不随分页、include 或可见成员数量重编号；无组使用原协议的缺省／null表达，不能由 protobuf 标量默认值补出 `{ordinal:0,size:0}`。校验沿原 typed DTO／协议边界完成，不在每次渲染时重新读取整组核验。公开序列化、生成客户端与历史实际响应须共同保持此语义。

消息来源与调用配对分开：call 的 `source.message_group` 描述原 assistant 声明消息，result 的该字段（若存在）描述结果自己的原消息；不能为方便合行将 call 的组复制到 result。回执关联沿原 scoped call／ModelCall／invocation关系，执行摘要沿原绑定附入调用行。合行展示以 call 为主定位来源，同时保留 result 自身的 Item 引用及来源；从结果定位回调用沿原关联，不新增一份 `declaration_group` 字段。这样 `[A,B]` 的回执即使按 B、A 到达，也不会被重新归入 assistant 消息或在 Provider 重建时混入声明数组。

配对沿原完整 owner、声明的 ModelCall 和真实 scoped tool_call_id，已知 invocation／attempt 时同时核其关联；result 的声明归属指产生原调用的 ModelCall，不改成结果到达时的当前 ModelCall。`provider_call_index`／`call_index`、组 ordinal、工具名和普通 `message_sequence` 都不能替代调用身份。不同 ModelCall 的第0次调用以及复用同一 Provider call ID 的调用必须保持独立；来源不足时保留未关联结果，不以最近一行或唯一已加载候选猜配。这里约束原关联的消费，不新增配对键字段、别名表或索引服务。

声明关系必须贯通原 writer、typed manifest、公共 DTO／protobuf、实时提交收敛与历史 reader，而不是只存在于 canonical payload。未提交 draft 使用原流中已经明确的调用来源和顺序；完整消息组尚未冻结时不发布猜测的 size／ordinal。提交后沿真实来源映射接入确定的组成员信息，重连后的原 snapshot／补读入口也返回相同语义；不能只靠前端记住第一次收到的 tool_calls 数组。重放声明或执行镜像只更新原来源，不新增调用、改变组大小或覆盖其它成员。

以 `reasoning(0)、text(1)、call A(2)、call B(3)` 为例，四个成员的组 size 均为4，工具声明数为2；后者只能从完整且可见的真实调用成员得出。页中只取得 B 时保留其 ordinal=3、size=4，不重编号为0，不显示四个工具或整批已完成，也不为 A 补占位行。B 的结果先到、A 未启动或失败均不改变两次声明的数量和归属。codec 往返验收分别核对有序调用 ID／参数、语义成员数和回执关联；不能只比较拼接正文或最终成功状态。

`tool_summary`、`activity_outline` 和 internal execution 摘要均包含范围内的最小安全声明／执行关系；参数、结果正文、保护数据仍只在明确 include 后读取。call所在页与result所在页不同，不应使已有执行证据暂时消失：原history reader通过同一scope、ModelCall和scoped call的轻量投影关联读取可见result的摘要，沿原snapshot／read fence与预算返回，不补入结果正文或页外展示行。所选view排除了该结果时不得借全局catalog补回它的状态。工具详情沿原预算和continuation按需返回；默认摘要不携带attempts或关系对数组、不扫描全组正文、不请求N次per-call API。活动页从组中段开始仍给出真实组边界和本页关系，页外／view中已不可见的成员不被补成可见行，也不因仅拿到本页而宣称整批并行或全部结束。

以下「前端消费合同与验收对照」统一列出来源、执行区间及刷新规则的示例，避免分别维护声明顺序和执行关系的两套解释。展开、刷新、局部分页与工具合行均使用同一规则，不为解释关系等待补齐整批证据。

#### 前端消费合同与验收对照

声明来源与执行摘要是两类数据，不再新增一个持久的`parallel`／`serial`字段或调用关系矩阵。前端沿原工具行展示同批归属和各自状态；本变更不要求默认新增并行／串行徽标。需要解释执行关系时，才以已有摘要在详情或结构化悬停中表达可证明的重叠。关系派生不触发网络请求，也不阻塞首屏或排序。

| 摘要内容 | 必要语义 | 缺失时的行为 |
|---|---|---|
| 声明定位 | 当前view的调用Item身份、完整owner／ModelCall、原消息组及成员ordinal／size | 不借相同工具名、邻接或相同ModelCall拼组；原子单成员不强造消息组 |
| `tool_outcome` | 原结果的安全 marker；调用投影仅经精确可见回执关联派生 | `null` 不等于 `unknown` 或成功；有结果而无时序时仍保留真实 marker |
| `tool_execution` | 可空的原invocation／attempt／execution、所属执行流、执行status／outcome及可得起止证据 | `null`只表示本次读取没有可用执行摘要，不表示未执行、排队或成功；不能覆盖已有同binding的已知证据 |
| `started_at`、`execution_completed_at` | 同一可比较执行范围内的真实边界 | 缺少任一边界时不推断有限运行区间；合法缺证据与读取损坏沿原错误出口区分 |
| `completed_at`、`completed_seq`（若可得） | 完成镜像发布事实 | 不参与实际执行重叠判断，不要求canonical提交前就存在 |

同一binding的摘要与实时更新合并仍复用原有序更新规则，不能用迟到的running／不可得摘要覆盖已知终态；切换view则按新binding重新判定可见事实，不把上一view的结果强带进来。binding沿原完整owner、projection_epoch、source_view_id及source_view_revision核验，并只合并同一真实调用／attempt；不同binding的内容不能因item_id或Provider call ID相同而拼接。已加载detail正文与轻摘要元数据分别沿原投影合并：保留正文不等于整条拒绝新摘要，新摘要也不能将正文降成未加载；接受有效的执行状态／来源更新仍须满足原binding与有序更新合同。不为此另加客户端执行状态树或版本账本。

工具行还需区分“已知结果”与“可知时序”：已关联的结果按原tool_outcome／执行outcome表达成功、失败、取消或未知，不以通用completed字符串替代结果语义。只有声明时使用中性的已声明／尚无执行状态；已有结果但没有tool_execution时仍展示真实结果，只把起止与执行关系标为不可得，不能因此退回已声明。原执行owner明确报告running且没有真实终态时，按其状态表达运行中，不要求同时取得started_at；真实执行身份及状态已经明确而时间缺失时，仍保留整份可用摘要和已有结束观察，不以缺一个时间字段为由返回null或退回已声明。时间字段只限制耗时与执行关系的推导，不能代替状态判断；收到取消请求本身不结算为已停止。以上是对现有工具行的派生规则，不新增持久UI状态枚举。

结果标记的交付路径固定为：原 `tool_result.payload.tool_outcome` → 写入时原 typed projection manifest 的安全镜像 → SQLite 摘要 → 公共 Item DTO／生成协议的可空 `tool_outcome` → 原工具行。沿用 `success|failure|cancelled|unknown`，不从正文、异常关键词或 canonical `status=completed` 推断；`null` 表示本次没有可用的结果标记，`unknown` 则是已记录的未知结果，两者不能混用。canonical 中该 marker 仍只属于 tool_result，manifest 镜像不成为另一份 canonical 顶层字段，也不在每次摘要读取时打开正文核验。调用 Item 的公开投影可经原精确回执关联携带该只读 marker，不能因此改写调用 payload、canonical status 或原声明来源；非工具投影不使用该字段。result-only 页从结果自身取得 marker，不要求先找到 call 或 tool_execution。实时提交、历史 summary／detail 和 internal execution 使用同一映射；已有失败结果但没有执行起止时，刷新仍须展示失败。

跨页摘要关联以本页已经选择的工具 Item 为输入，沿原索引批量取得其在同 snapshot／read fence、所选 view 内可见的精确回执 marker 和执行摘要；查询不能先取整个 Turn／execution 的全部调用再在内存截取。仅为关联而读取原消息组末 anchor 的既有 locator，不意味着将该成员加入当前 view 或补回其内容；结果的可见性仍须独立核验。页外可见回执可提供摘要，被 view 排除的回执不能提供结果 marker 或终态证据。无精确关联时保留原调用／独立结果，不增加候选扫描、正文兜底或新结果索引。这些读取仍计入原历史条数、字节和总延迟预算。

同一结果已有合法的request／runtime两类carrier时，各自canonical Item、原消息来源和既有正文survivor选择保持不变；两个carrier不代表两次执行，也不增加声明组成员。调用摘要只合并本次view／read fence内精确关联载体已有的非空`tool_outcome`与`tool_execution`，空值不抹掉另一载体的已知证据，非空事实冲突沿原错误出口报告，不按载体到达先后择优。result-only投影仍读取该结果自身的摘要；排除有执行证据的carrier后，不能从不可见载体补回时序。沿原批量关联完成，不新增载体优先级、持久合并结果或摘要读取时的正文比较。

以下仅示意必要来源关系，其他Item字段省略；source.message_group的size始终包含内容成员：

| Item | semantic_kind | 原消息来源 | 调用关联 | 独立执行摘要 |
|---|---|---|---|---|
| I0 | reasoning | ModelCall M，组G，ordinal0／size4 | 无 | 无 |
| I1 | assistant_output | M，G，ordinal1／size4 | 无 | 无 |
| I2 | tool_call | M，G，ordinal2／size4 | M的调用A | A的原attempt，可空 |
| I3 | tool_call | M，G，ordinal3／size4 | M的调用B | B的原attempt，可空 |
| Rb | tool_result | 自己的原结果消息，不属于G | 原调用M／B | B的同一attempt，可空 |

I2、I3各自的payload.tool_calls恰含自己的一个声明；重建G才还原[A,B]。Rb先到不改变G、行序或A的状态；没有时序证据时仍可以知道B的真实结果，不能据此判断A／B并行或存在串行依赖。

对应公开摘要，I2的`source.message_group`为`{message_group_id:"G",ordinal:2,size:4}`，I3为`{message_group_id:"G",ordinal:3,size:4}`，两者的`source.model_call_id`均为M；完整owner继续由原响应scope提供。各自`tool_call_id`关联自己的调用，`tool_execution`仅描述自己的真实执行。这里的字段片段不是完整DTO，也不新增批次字段；参数和结果正文不需要随摘要交付。

同一声明的渐进展示按以下顺序验收。表中的执行边界仅为同一可比较范围内的示意值，实际字段仍使用原协议时间类型：

| 当前已知事实 | 固定调用行 | 可解释的关系 |
|---|---|---|
| I2、I3已提交，尚无执行摘要 | A、B，两者只表示已声明 | 同批；执行关系不可得 |
| A已有running状态；B已成功返回并有完整区间`[5,10)`；A尚无实际结束 | A仍运行，B独立完成，顺序仍为A、B | 同批；不能仅凭两者的运行镜像证明重叠 |
| 随后补齐A的真实区间`[0,20)` | 同两条调用行，分别保留原结果 | 此时可证明A、B执行重叠，不重建行身份 |
| 清空实时内存，从同view取得同样的声明与执行摘要 | 恢复相同两行和各自结果 | 使用同一证据得到相同结论，不要求回放事件 |
| 页中只取得I3，或view只保留B | 只显示B及其可见摘要，仍保留ordinal=3／size=4 | 不制造A行，不判断两者关系或整批状态 |

声明组元数据或执行证据补齐只更新既有行的来源／状态／关系说明，不创建新的批次容器身份，也不因结论从不可得变为重叠而重置已有展开选择、阅读锚点或键盘焦点。原draft到提交的真实身份收敛仍按既有合同处理。

判断粒度始终是两个已声明的调用。某个工具内部并发处理多个参数、发起多个HTTP请求或返回多块结果，不因此生成额外调用Item，也不证明它与另一个工具调用重叠；工具内部细节仍由原工具详情owner解释。本变更不递归发现或展示新的执行子图。

“先后执行”只描述这两个attempt已观察到的时间关系，不证明调度器采用串行策略、B依赖A的结果或其它调用也不重叠。若UI解释先后，应明确真实执行方向，不能按声明顺序补成A后B。默认列表不增加强制关系标签；刷新、重连和有界分页仍消费上述同一来源及证据，不改变行序。

以下时间仅示意同一可比较范围的真实执行边界，不是新增协议字段；前六例的原声明与行序保持`[A,B]`，其余示例分别保留三调用或跨消息组的原声明顺序：

| 真实执行证据 | 可表达的关系 | 不得表达的结论 |
|---|---|---|
| A `[0,10)`，B `[10,20)` | 同批、观察到A结束后B开始；边界相接不重叠 | 因数组顺序而存在A→B依赖、采用串行调度策略，或两个调用并行 |
| B `[0,10)`，A `[10,20)` | 同批、观察到B结束后A开始；行序仍为A、B | 将B移到A前，按声明顺序描述A先执行，或断言串行调度策略 |
| A `[0,20)`，B `[5,10)`，B先返回 | 同批、两个attempt发生重叠；独立结算B | 把B移到A前，或B完成后结束A |
| A `[0,10)`，B `[11,20)`；A完成镜像在15才发布 | 同批、真实区间不重叠 | 用镜像的15代替实际结束10，误标并行 |
| A、B真实区间重叠，但消费端在两者返回后才依次处理A、B通知 | 保留源端冻结的两段真实区间及重叠关系；行序仍为A、B | 将消费者依次处理误判为先后执行，或用批量返回时刻替代各自结束 |
| 只有A／B的running镜像或一项没有可信结束边界 | 同批和已知状态，执行关系不可确定 | 串行、并行或未执行 |
| 同批A `[0,10)`，B `[5,15)`，C `[10,20)` | A与B、B与C分别重叠；A与C不重叠 | 把重叠当作可传递关系，称三者同时运行 |
| 不同原声明组的A `[0,20)`、B `[5,10)`，同一可比较执行范围 | 不同批次的两个执行发生重叠 | 因执行重叠将两组声明合批，或因不同批次否认真实重叠 |
| 不同目标／工作区或不可比较执行流中，A、B的时间数字相交 | 各自已知状态与来源；没有可比较的执行关系 | 仅因数字相交而显示并行、跨范围比较事件序号 |

判定只消费本次已加载且所选view允许的证据，不为了算出整批关系加载所有页、两两生成持久边或重读工具正文。中段分页与不完整view保留这种局部可知性。默认列表不生成全量两两关系；详情或悬停只比较当前需要解释的执行，不能在每次流式更新时对全部历史调用作平方级配对。关系可从已有证据推导，不因此增加持久字段、缓存或专用分析服务。

#### 计时显示不混用内容间隔与工具运行区间

| 数据 | 含义与消费位置 | 不得替代的事实 |
|---|---|---|
| Item `elapsed_ms` | 原历史计量合同中相邻逻辑活动成员的时间间隔 | 该工具的运行时长、排队时长或调度依赖 |
| `execution_completed_at - started_at` | 同一真实attempt、同一可比较时间依据上的已知执行区间长度；详情需要时从已有证据派生 | 声明生成耗时、完成镜像延迟或整个过程的耗时 |
| Turn `duration_ms` | 原生命周期owner给出的acceptance至terminal固定耗时；未终结为null，实时计时沿原UI规则 | 消息投影更新时间差、每次请求时钟、各工具耗时之和或某消息组的运行时长 |

工具call／result合行不能把它们的Item间隔改名为“工具耗时”，也不能再把同一attempt计两次。例如A运行于`[0,20)`、B运行于`[5,10)`时，两次执行分别为20和5个时间单位；25不是这批工具的经过时间，也不是Turn耗时。当前不新增批次耗时、累计工具耗时、区间并集统计或对应存储字段；若原工具详情显示运行耗时，只使用该attempt可得的真实证据，缺证据不以Item间隔或结果提交时间补值。运行中已有计时沿原执行状态处理，不把客户端当前时间当作持久结束证据。

### 3. final 是已有 Item 的展示角色，不再产生聚合内容事实

保留 Turn 的唯一 `final_item_id` 终态入口：它指向由真实 finalization 选定的最终输出消息组内最后一个完成的输出 Item；组内其它最终输出内容通过该明确消息组的有序 assistant_output 成员解析。reasoning、tool_call／result 和其它 ModelCall 不进入最终回答。

一个 final 消息组可能有多个文本／媒体 Item，UI 可连续呈现为一份回答；每块保留自己的 Item ID。`final_response` 文本如仍为现有消费者提供，只作有界派生值，不成为定位键、canonical 聚合 Item 或另一份持久 final 成员清单。没有输出内容的正常完成仍为 completed_empty；未知、失败或中断不推断为 final。

实时到终态的替换按 writer 的真实 draft／Provider block 与提交 Item 关系完成；最终内容移出过程折叠区不改变 Item 节点键。不按最后一条文本、正文长度、文本相等或固定的大 ordinal 推断 final。原 canonical 状态和 provenance 不因重新展示而修改。

执行结束沿非摘要模型结束事件的原 message_id 读取已提交 checkpoint 消息，再用原 canonical manifest 经统一 Item reader 取得有序成员；不生成新 assistant message_id、不追加 replaces 关系、不重新写 checkpoint。没有原身份或原输出尚未提交时明确失败，不从最近文本推导来源。模型切换更新来源，后续摘要候选不能清空或覆盖原最终输出来源与块。

原输出已经提交时，终态走原 metadata-only convergence：普通 Turn 核 final_item_id 属于同 execution、completed assistant_output 且在当前 active view 可见；无 Turn execution 核其已有可见完成输出，只收敛 execution／ModelCall／assembly，不增加 Turn final 字段。重复提交沿原终态幂等键返回，不复制正文或增加引用账本。完成与中断仍由同一 owner 事务裁决；完成已赢得裁决时，外层停止编排直接交付完成结果，不再取消尚在发布完成镜像的原 Job。

末次模型请求的完整用量随原 AIMessage 首次 canonical 写入冻结，复用同一个 usage mapper；只有消息组 anchor 携带消息级 usage。终态不另补一条用量消息，不用整轮累计值替换原请求统计。

### 4. 持久内容投影以 Item 为单位，展示结构由原客户端入口组织

历史／实时 reducer 迁到 Item 内容投影，已提交条目必须携带真实 item_id、semantic/payload 类型、安全投影、来源、归属、可用性与逻辑顺序。未提交条目使用显式 draft 身份，与持久 Item 分开；来源到提交关系只在本次流和收敛边界使用，不建设持久别名库。

协议及消费者统一采用 `response_items`、`display_items` 和 `TurnResponseItemDTO` 等与原职责对应的名称，替换原 response_parts／display_parts／TurnResponsePartDTO，不保留同义转发。实际字段迁齐后删除 `part_id`、`part_ordinal`、`activity_group_part_ordinal` 和 `_FINAL_PART_ORDINAL`。已提交排序只使用当前 view 的逻辑 Item ordinal；draft 排序保留原流的真实来源顺序。

统一响应投影的 `id` 在已提交内容上必须等于 `source.item_id`，不拼接种类、slot或序号；draft和上下文标记仍以自身真实来源标识，不因此成为canonical内容。原工具执行摘要的 `started_at` 可空：已经明确的执行身份、状态和实际结束观察不因开始时间不可得而整包丢弃；缺边界仍不能判断运行区间。格式变更沿既有浏览器历史镜像owner登记为3，旧镜像只失效当前条目并从权威历史重读，不转换草稿或删除阅读位置。

非文本 assistant_output 使用同一投影的 `kind=content`、`carrier_type` 和可空 `content` 表达单个原结构块，不另建媒体 Item／协议服务。SQLite 既有 typed manifest 只镜像 `output_block_type`，不保存 URL、base64 或媒体正文；默认历史只交付类型和未加载状态。用户显式补读时沿原 item_ids、view／read fence 和单页预算取得该块，final 内容保留同一 Item 身份与 final 角色。JSON、URL、base64 只原样交付或明确受限，不截出损坏对象；单项沿原64KiB预算、整页沿原总预算记账，超限保留身份和类型及 truncated。此显示限制不改 canonical／Provider 请求内容。已加载详情不能被迟到轻摘要清空。

Web 在原正文节点渲染可识别图片／音频／视频引用，未知但已允许公开的块提供原结构查看；仅因块类型未知不得当作思考、文本或悄悄丢弃。媒体加载失败明确显示，未加载按钮不冒充已有正文。流式块封存只表示块完成，不能据此将媒体或文本升级为 final；使用同一权威 final 来源。

工具 call/result 可聚合为一行，但行内仍引用两个 Item；reasoning 的摘要／正文共用一个 Item 的不同 slot。活动组 ID 只用真实组首 Item 与过程／段落来源；分页只取得组中段时，由原 reader 在同一 snapshot 有界查询真实边界，不把页首冒充组首。边界查询复用索引，不能先扫描全部历史再截页。

上下文虚线、过程标题、通知等继续用原真实业务来源；不是 canonical 内容的节点不必伪造成 Item。保留现有统一展示节点和滚动入口，只替换其中 Item source 的身份，避免为了删除 Part 再写一套滚动框架。

### 5. 删除复合轮廓，用原 Item 索引提供轻量摘要

迁齐消费者后删除 `message_projections.response_outline_json`、`activity_outline()`／`internal_item_outline()` 对复合 Item 的二次展开，以及 SQL 对轮廓 JSON 的 json_each 和 Part 级计数／边界算法。

现有 `item_catalog`／Item projection 保存查询所需的 semantic、类型、状态、membership、时间、工具引用与安全摘要；正文按原 JSONL locator 显式有界读取。不得把同样的多 Part 数组换名塞回 item_projections，也不新增 presentation_index 或全历史轮廓表。现有 `message_projections` 的其它真实消费者先逐项核对，不因删除一个字段而删除仍服务 checkpoint／正文查询的列或表。

原 `reasoning_blocks` 安全索引在 SQLite31 收束为按 canonical `item_id` 唯一的一行，不再按消息级匿名块合并或以 Provider Item ID 作为主键。正文、摘要各有原64KiB预算及截断标记，签名／密文只保存存在性和既有安全证据；不同视图不增加 Item 计数。codec 复用原 carrier 解析，完整消息提交与无消息 locator 的独立思考共用同一写入方，普通 Turn／无 Turn 历史沿同一语义读取；不得恢复 final 专用思考补读、在 catalog 解析 Provider envelope 或用组 ordinal 冒充 Provider block 来源。只请求摘要时不能取出同 Item 的思考正文；切换安全视图不改变 Item 身份。

保留历史 include 的 `activity_outline` 请求意图：它仍表示轻量活动清单，响应改为 Item 投影；名称本身不等于需要独立轮廓存储。不另加 `item_outline` 并行 include。

主历史仍按 Turn 和无 Turn execution 的原合同分页；活动详情 continuation 的最小成员为 Item，移除页内 Part 坐标。单个 Item 达正文预算时沿原 truncation／详情入口处理，不能无限分页追读正文或扩大预算。最多256个 canonical record、原字节上限、同 snapshot membership/read fence、owner／revision 校验保持。cursor 只服务查询，不成为阅读锚点。

统计按可展开逻辑 Item 去重，非 final 的可见文本／媒体仍是活动成员；保持原排除规则：root、final、纯加密存在性、隐藏 notice、carrier、传输 chunk 不计；call 和 result 即使合一行仍各计一次。耗时沿原真实业务时间和相邻逻辑成员计算，不按 DOM 或物理相邻记录重新解释。

### 6. 阅读锚点只引用 Item，内容内位置属于当前客户端

```text
后端：item_id ＋ canonical 语义、来源、membership 和按需内容
                  ↑ 引用
客户端：scope ＋ source_ref(item_id) ＋ slot ＋ 本地内容位置 ＋ viewport_offset
```

Item source 删除 `part` 属性。普通正文、摘要、媒体等以 slot 区别展示部位；slot 不复制为后端业务身份。长 Markdown 内的块号／块内偏移和 viewport_offset 仅用于该客户端恢复附近位置，不上传为 canonical Part、不参与 fork／replay，不保证不同渲染器重排后像素精确。

折叠标题的临时 DOM 成员关系使用完整节点键（source_ref＋slot），不能只登记 Item 来源，否则同一 Item 的正文与摘要会误命中兄弟标题。过程标题同时登记实际活动组标题及内容节点；仍由原展示投影即时派生，不新增注册服务或持久成员表。明确补读后确认 slot 已不存在时，沿原不可恢复出口报告并保留记录；不能仅因 DOM 未挂载判定内容被删除。

保存范围为当前客户端安装／浏览器配置、原 Gateway＋工作区＋Session＋Thread scope；后端业务版本变化触发原展示缓存失效，跨客户端不复制 reading_anchor／disclosures。Web 和 Electron 即使使用相同源码也使用各自现有存储环境；同客户端多编辑器沿原 editor/view-state owner 行为，不新增全局同步服务。

恢复先应用折叠状态，再定位 Item 或最近可见祖先；普通恢复不自动展开。显式 Item URI／搜索跳转沿原 around 入口核验后按既有交互展开；真实删除、view 不可见、无权限、过期和网络失败仍分别表达。虚拟列表卸载、尚未加载、正文截断不等于业务删除。流式 draft 未提交时只作当前编辑器临时保位，持久记录保存真实过程来源及 follow_latest，不伪造 Item。

### 7. 操作边界与来源 block 分离，不把 UI 块升级为操作锚点

fork／rewind／replay／compaction 默认使用 Item 边界，带 before／inclusive、真实 view／branch 和能力核验；不再新增通用 content_part durable anchor。中断把当前语义 draft 终态化为 partial／cancelled 等真实状态，后续 resume 追加新 Item 并沿原关系关联，不能原地改历史。

Provider part/block ID、content_part_refs 等若仍被原编译器用于签名、密文、流式来源映射或去重，保留在原 typed 协议边界；逐个消费者证明不再需要后才删除。它们不得重新渗入阅读锚点、业务 URI、历史活动主键或工具调用 ID。

同一 wire 消息组内的 Item 可被定位，不代表所有成员位置均可执行 rewind：沿原操作 owner 判断 Provider 因果和不可拆签名边界；不合法的边界明确拒绝，不静默扩到整消息或返回空成功。不为保留 Part 级任意截断能力新增 fragment hash／稀疏 Part 索引。

### 8. 在原目录纵向替换，不增加统一中转服务

| Owner／目录 | 修改责任 | 必须删除或替换的旧内容 |
|---|---|---|
| `app/domain/itemized` | Item／group 合同、版本、能力和唯一校验 | tool_call-only group 限制、通用 Part 操作锚点合同 |
| `rollout_context/checkpoint/codec` 与 `message_codec.py` | 统一拆分与重建、组归属、draft 收敛 | 只处理工具的展开分支、普通消息复合打包路径 |
| `app/services/mapping/itemized` | Provider／LangChain、summary-source、去重与压缩来源 | 各自重复拆块、按 Part 推断 canonical 来源 |
| `rollout_context/storage/catalog` 与原 migrations | Item 轻量读取、membership、活动边界／cursor | response_outline_json 与 Part 级 SQL |
| `app/schemas/internal_v2`、`proto/boxteam/workspace`、SDK 生成协议 | 新字段、验证与所有生成消费者 | 旧字段／类型；删除 protobuf 字段号与名称须 reserved，不复用 |
| `src/clients/web` 原 state／hooks／chat／storage | Item 投影、实时合并、三层展示、客户端阅读态 | Part 主键、别名、重复状态与旧持久坐标 |
| `src/clients/workbench` 原 Opener | Item URI 与编辑器跳转 | Part 定位参数及猜测性回退 |

不新建数据库、注册服务、BFF、Gateway Item 转换器或全局事件总线。名称调整迁齐直接／间接调用和测试后物理删除，不能留 response_parts 与 response_items 双字段。

## Risks / Trade-offs

- **协议还原丢失签名、str/list、invalid calls 或 usage** → 以已有支持的 Provider fixture 做真实 writer→canonical→重建往返，核原有语义字段和保护范围；未知输入明确失败，不 stringify。
- **细化 Item 后索引和统计变多** → 仅拆独立顶层语义块；不按 chunk 写入。保持分页／字节硬限、批量读取和原计数排除规则。
- **final 多块出现重复或丢失** → 由明确 finalized 消息组一次性确定输出成员；同一 Item 在中间区／final 区原子迁移，保持节点 ID。
- **声明与执行时间混淆，刷新丢失并行证据** → 只消费原执行 owner 的可持久摘要；分别验证同批但先后执行、实际重叠、交错完成、重试及缺证据，不从 ToolCall／Item 时间补造运行区间。
- **为了无损迁移引入大量旧格式分支** → 正常运行只读目标格式；来源确认和转换只走原离线 owner。没有支持来源时不为了 dev 残留实现猜测修复。
- **删除所有“part”文本破坏第三方协议** → 删除的是项目通用业务／展示身份；Provider SDK 字段、原始诊断和仍有实际必要的 typed wire 来源不按关键词清扫。

## Migration Plan

1. 实施前重新读取 Git、发布证据与原 owner 版本：初稿快照为 canonical format2、rollout SQLite30、阅读格式2、history cursor v2。实际集成目标为 canonical format3、rollout SQLite schema34、阅读格式3、唯一history cursor v3（移除活动续点中的Part坐标，其它绑定及双lane规则不变）。SQLite31 是早期思考安全索引切片，schema32 登记语义 Item 基础，压缩变更随后以 schema33 登记候选／前序结果／原文行范围、schema34 登记手动下一请求采用意图；当前版本只由 `storage/schema.py` 唯一声明。这些未发布中间态不据此增加自动转换或旧结构双读。若并发变更再次占用目标号，先按原 owner 修订本设计，禁止混用同版本不同语义或另建版本账本。
   这些版本互相独立；不批量替换代码和文档里的“v2”。`workspace/v2`协议命名空间、`context-plan-hash:v2`算法标识不因Item版本升3自动改名，只有其自身合同实际变化才由原owner登记。
2. 先在隔离 fixture 完成目标格式的唯一 writer／reader／消费者。原 `unify-conversation-process-navigation` 的流程、时间预算和非 Part 节点仍保留；其归档先于本变更。归档前把本设计的替代清单与该变更最新落盘版本逐项核对，不能让旧 delta 后归档覆盖新合同。
3. 用户已明确同意退出未发布 v1/v2 离线转换支持。删除 `v1_jsonl_to_v2_jsonl` 实现、专用 staging／报告表、历史终态物化分支及其测试／矩阵入口；不冻结另一套旧 writer／reader。格式1／2及未知版本在唯一版本闸门明确拒绝，原件保持，不提示已删除的转换命令。既有有序 SQLite 结构迁移保留原 owner 与顺序，不借本次授权转换真实 dev 数据。
4. 核 `R_current`／`R_previous` 与用户另外明确指定的来源，沿 `version-upgrade-conventions` 决定必要转换。现有 dev v29→v30 授权只覆盖当时新增的投影，不涵盖此处 canonical 拆分；没有来源／授权时使用隔离新 Home，保留原数据并报告不匹配，不自动升级真实 dev。
5. 若支持来源需要转换，使用原存储 owner 显式 `v<来源版本>_<来源格式>_to_v<目标版本>_<目标格式>` 转换，停止 writer、锁内备份 canonical／SQLite／必要关联存储，按 catalog 已登记来源处理。保留原用户、Thread、Turn、execution、ModelCall 与来源事实；已原子 Item 保持 ID。复合 Item 拆分沿原单一转换计划分配子 Item ID，原 ID 保留为组末真实语义成员，不另保留一条旧聚合 Item。
6. 转换必须迁齐所有引用原复合 Item 的 view membership、checkpoint、final pointer、搜索引用和封存上下文选择，不能只重建 message_projections 或删除列。旧复合 Item 引用在转换内按其真实语义展开到完整目标成员；组末原 ID 不得被当作完整旧消息的等价内容。核已封存 Provider 请求的正文、顺序、工具配对、媒体引用、保护事实和原请求 hash 未改变；目标 plan 使用其真实格式版本和重新计算的 plan hash，原审计值保留为转换来源，不以新 hash 冒充旧封存结果。不能确定来源或无法保持封存语义时在发布转换前明确拒绝，原件不开放给目标 writer。转换映射只用于这次事务与恢复证据，不进入正常查询别名链。
7. 删除轮廓字段是目标 SQLite 结构迁移的一部分；派生数据从已确认的目标 canonical 重算。正式转换不宣称 canonical 字节 hash 不变，逐项核语义等价并保留原件；不扫描磁盘吸收未登记数据。
8. 客户端阅读结构版本按原视图 owner 更新。只有支持来源可证明旧 Item／Part 对应唯一目标 Item 时才能由既有转换入口转换阅读记录；未支持开发格式保留原记录、明确说明无法精确恢复，沿正常可用过程位置恢复，不清空全部 IndexedDB。不能直接把 `part` 字段删掉后声称原位置精确保留。
9. 失败使用同一批原件恢复一致的旧格式和旧代码；新 writer 已写入后不得只回退旧二进制。实际数据转换、部署和启停遵守各实例已有授权范围。

## Validation

少量有判别力的入口验收覆盖：无工具混合输出、同内容带工具调用、两次并行工具及未知结果、媒体与签名／密文、用户附件 root、空／失败／中断、final 多块、无 Turn execution，以及 fork／压缩／上下文编译。先核真实序列化和跨层类型，再运行对应定向检查，不为每个 DTO 字段重复写测试。

工具场景通过真实 writer／执行入口覆盖同批 `[A,B]` 的先后运行与重叠运行、B先完成、不同ModelCall重用调用ID、原规则允许的跨ModelCall重试与同call第二attempt拒绝、尚未执行／取消／进程丢失和明确缺执行证据；核默认摘要、分页中段、实时收敛和刷新一致。核 canonical 单调用数组及原消息重建顺序，默认读取不带参数／结果、不读Trace或旧终态实时缓存，不因补元数据增加逐调用请求或无界历史扫描。

真实 Workbench→Gateway→Backend 验证刷新、三层折叠、搜索／URI Item 定位、分页穿过同一组、长文本重排、两个独立客户端的阅读态隔离、六会话与窄拖拽；不发真实上游模型请求作为默认性能装具。固定128 Turn 的现有 fixture 加入混合块和大型工具结果，不预写浏览器缓存冒充读取。

沿用更严格预算：history 暖读 p95≤200ms，已有内容的折叠／保位单次≤200ms，网络展开 p95≤500ms且单次≤1500ms，首次数据读取≤3000ms，Composer 可操作每次≤2000ms。完整计到响应解析或目标内容实际可读，至少20次未筛选暖样本，保留 p50/p95/max、request_id、规模、字节与源码指纹。不把启动／构建计入暖读，也不扣内部检查或队列时间。完整性全检只在写入、显式转换／恢复和原 owner 的必要失效边界执行，不进入普通请求循环。

所有测试使用既有 matrix runner 或外部 timeout＋内存保护。涉及前端必须真实类型检查及 `bun run --cwd src/clients/web build`；正式测试产物沿原 `out/tests/<测试路径>/`，临时诊断使用 `out/tests/temp/YYYY/MM/DD/<task_name>/`，不向项目根注册测试工作区。已通过的检查仅在相关源码变化后补验，不默认全仓重复运行。
