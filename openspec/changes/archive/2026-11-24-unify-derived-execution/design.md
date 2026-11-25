## Context

动机见 proposal。以共享 worktree 已有实现为起点，不回退压缩基础。

| 现有事实 | 本次调整 |
|---|---|
| internal admission 和 DerivedModelRequest 已有，但引用绑定 candidate／chunk／merge | 公共引用通用化，业务坐标留原压缩计划 |
| ModelCallExecution 已统一投影／seal／dispatch，但派生只接受摘要文本且每次调用即结束 execution | 单次模型调用只登记调用事实，由原运行循环决定 execution 何时结束 |
| model_call_graph.py 使用 LangChain create_agent，AgentToolNode承接工具 | 同一工厂提供派生绑定，不另写循环或复制主Turn消息pipeline |
| BackgroundTaskRegistry 有派生额度／发布gate／恢复，却按Thread合并所有派生，foreground直接启动 | 按真实task幂等，保留原队列，补一个总运行上限 |
| ToolCapabilityPolicy 禁执行会联动隐藏schema | 本次工具允许列表作为原执行策略的收窄，不改模型可见的sealed tools |
| 任务在Session控制库，rollout按真实Thread落盘，注册器以空Context启动 | 复用原位置，显式绑定本任务执行上下文 |

## Goals / Non-Goals

**Goals:** 用现有执行基础承载参数化派生请求，保留可复用缓存前缀、受限工具循环和真实结果，接入现有压缩。

**Non-Goals:** 不实施命名、记忆或团队产品；不新增严格结构化输出、派生暂停／恢复API、工具资源权限语言、历史版本工具池、调度服务或自动恢复执行。上述未来能力只能扩展本入口，不能预写第二链路。

## Decisions

### 1. 一个共同输入和原handler注册点

在原 BackgroundTaskDescriptor kind／version 映射中提供一个参数化派生 handler，供内部及HTTP共同调用。压缩保留自己的业务handler，调用同一执行核心；公共层不按purpose实现不同发送／重试逻辑，外部不能选择内部factory。

| 输入 | 处理 |
|---|---|
| 目标工作区、session_id、thread_id及可选来源范围 | 原resolver及Saver冻结授权来源，默认当前active view最近已提交闭合前缀 |
| 任务提示词 | 按原协议作为request-only后缀追加；输出风格或JSON文本要求也放这里 |
| 可选允许工具ID列表 | 缺省及空列表都不执行工具；目标资源范围沿原SessionThread及工具自身合同 |
| 可选模型选择 | 原resolver首次解析，省略时沿来源Agent配置；已有fallback照原路由处理 |
| 有限操作期限、调用上限 | 调用方或已注册owner提供；确有既有有限默认才复用，缺失则明确校验失败 |
| 幂等键与调用归属 | 原控制库task身份登记，外部不提供admission或namespace |

工具约束只是冻结请求中的typed允许列表，在原invocation准入取现有策略交集；不另签权限票据、不建立资源scope表达式或权限数据库。本次输出为文本（可包含Markdown／JSON文本），不新增response_format／ToolStrategy派生支持或自动解析、修复、重试格式；需要严格schema时后续在原输出协议扩展。

不新增派生专用输入token预算参数，也不由上下文容量生成输出上限。操作期限和模型调用次数上限继续约束整个task，与单次请求的模型容量分开处理。

一个task可关联多个execution，压缩chunk／merge／attempt仍按原计划分开；每个execution可包含多个model call与工具步骤。公共引用使用task与原计划身份替换 CompactionExecutionReference。原internal admission所需独立job identity继续签发，不创建主Job记录、不借父Turn。

未来名称、三类记忆草稿和繁忙Thread临时问答只证明这些参数足以表达用途，不预设触发频率、结果应用和UI，也不注册对应生产业务handler。

### 2. 幂等受理只冻结一次

原键首次受理绑定显式输入及其有效来源／配置／期限。重试先查原task，比对显式输入，复用已冻结计划；父append、配置默认值热更新和当前时钟不改变原身份。明确改变任务输入、来源、工具允许列表、模型或预算则冲突。同Session其它Thread重用同一task身份不能返回前者任务。

并发提交沿原Session短准入门与控制库唯一约束裁决一个赢家。来源版本／保留引用在原owner边界捕获，正文读取及网络工作不长持Session门。仅完整descriptor及计划可冷读后返回受理；受理前失败、冲突或满载的临时引用由原owner退出／维护机制回收，不建提交账本、事务协调器或新的持久中间状态。回执丢失后原键取得原task。

### 3. 来源闭合与缓存复用

来源默认按权威active view选择，不以未采用候选或最近派生assembly替代来源事实。默认截到最近工具协议闭合的已提交前缀；显式不闭合范围拒绝，不等待父工具、不读取draft或伪造结果。

普通派生冻结来源时，通过原候选owner优先选择已经ready且覆盖范围／内容版本适用的摘要，替换对应来源并完整保留其余内容；沿原候选顺序与覆盖规则选择，不拼接重叠候选、不扩大来源。没有适用摘要（包括尚在生成或已失效）就直接使用原内容，不等待、不启动摘要任务。选用摘要及其保留引用随首次计划冻结，重试和后续工具步骤不追踪新候选；不新增候选索引、轮询或每步历史核验。替换仅作用于派生请求投影，不改变来源view、候选采用事实或主CSM消费状态。压缩handler显式提供的块／merge输入仍沿其冻结计划，不由这项默认替换规则重写。

同Provider投影条件下复用原sealed root、tools与实际未变化的连续wire前缀；补齐sealed请求之后真实提交且被选中的闭合尾部，最后追加任务指令。摘要替换使历史变化时在原投影入口重建变化部分，只声明实际相同前缀可复用，不为缓存撤销默认替换、漏选尾部或扩大范围。签名思考块、受保护carrier和工具配对沿原协议处理。任务文本不覆盖来源root，不接受替换来源role的message数组。

无可复用seal时走原纯投影入口，不构建父DeepAgent；来源不可读或无合法闭合范围明确失败。换模型／分块导致重投影则记录真实限制，不承诺上游命中。每次请求（含后续工具结果）沿统一准备入口按实际模型容量检查完整输入；已知超限直接失败，不截断、不等待或生成摘要、不递归压缩。容量未知时沿原规则发送，上游overflow保留真实错误，不触发压缩补偿；普通模型fallback仍遵守原路由，不新增派生专属回退。沿原task／execution错误和Trace记录来源截止点、实际模型、容量／估算可信度及已有摘要替换或不可用原因，复用已有计划与错误引用，不额外复制上下文或建诊断账本。原压缩handler仍显式规划自己的分块与合并。

### 4. 模型和工具只用原执行链

复用 model_call_graph.py::create_model_call_agent 及 AgentToolNode，为本次execution提供独立输入／输出绑定；允许轻量运行图，不搬主execution_step的聊天发布、主Job调度或父checkpoint。模型绑定和输出处理仍归原图工厂，唯一发送仍归 ModelCallExecution，不手写第二个while循环。

原注册器空Context启动保持；显式绑定真实工作区／SessionThread、admission、冻结配置、日志及本次delta出口，不继承父暂停对象、取消token、activity writer或最近usage。工具依赖从原装配显式传入，不能通过借父runtime补齐。不支持派生上下文的工具明确拒绝，本次不重构所有工具。

允许工具按原稳定ID及信封内部真实目标核对，在实际执行点用原策略收窄，撤销生效但热更新不扩权。未知或不能在本上下文运行的显式允许项准入时报错；模型提出其它调用则沿原工具结果合同配对拒绝。保留schema不代表授权，不自动清tools或改tool_choice，也不新增或绕过审批；原工具若要求本次无法取得的人工确认，返回原错误。

使用本次装配的原工具目录及其校验，不为派生保存旧版本实现、逐次hash schema或建立兼容矩阵。插件／工具变化造成不可用或协议错误时由原owner返回真实失败，不替换成另一个同名目标。工具并发、参数校验、结果上限和operation归属沿原ToolNode／invocation机制；不新建逐工具次数账本或结果裁剪器。原Command校验禁止把结果应用到来源Graph，保留已经发生的操作事实。

### 4.1. 参数在所属边界解析，执行差异在装配时绑定

共享代码不等于每层接收完整派生参数并判断模式。当前源码已有值得收敛的接入点：`create_model_call_agent`以独立可选的`derived_request`／`derived_result`分支调用；`ModelCallExecution`在准备、执行入口判断请求类型，又在`_PreparedModelCall`保存`derived`布尔值供解码和callback配置判断。这些是本次迁移的替换对象，不是要求继续复制的公共模式。

采用既有图工厂内的组合方式，不新增运行时策略注册器或继承层级：

```text
HTTP／内部提交 → 同一准入 → 原任务descriptor／冻结输入
                                  ↓
                      handler装配本次执行依赖
                                  ↓
主会话：live准备 ──┐       ┌── 主会话输出／事件收敛
                  ├─ 原封存、投影、发送 ┤
派生：冻结准备 ───┘       └── 派生输出／任务收敛
```

| 参数／差异 | 唯一解释边界 | 下游取得什么 |
|---|---|---|
| API参数、默认值、幂等身份 | 原提交service及schema；内部调用复用该准入 | 原冻结输入与引用，不再传原始HTTP DTO |
| 来源、已有摘要替换、指令和模型选择 | 原来源／候选owner及handler；压缩块／merge由压缩handler准备 | 冻结plan、preparation、模型与admission |
| 主会话／派生的模型节点行为 | 原`model_call_graph`装配处 | 一项完整的模型调用Runnable，返回原ModelResponse |
| 工具允许列表和调用上下文 | 原工具装配与invocation执行点 | 本次执行绑定及现行授权交集，工具实现不读取purpose分支 |
| 排队、期限、发布、取消与恢复 | 原BackgroundTaskRegistry及版本化handler | 原task事实；effect判别仍留在该owner |
| wire、日志和持久化 | 原provider、日志、Saver owner | 规范化请求、真实身份及原版本数据；指令已是消息内容，不另收控制执行的整份任务参数 |

图工厂用一项完整的模型调用绑定替代两个可独立缺失的派生回调：复用框架已有`Runnable[ModelRequest, ModelResponse]`，在装配时选定同步／异步调用，图节点直接调用`invoke`／`ainvoke`。普通绑定执行原live准备和主会话协议；派生绑定在本地闭包中生产`DerivedModelRequest`、调用共同owner并保存`DerivedModelResult`，返回原ModelResponse。图的条件边和ToolNode继续共用。主执行暂停及派生上下文隔离在各自绑定边界接入，不让每轮模型节点重复判断派生模式；同步／异步入口均保持原契约，不增加每节点hook清单或“无操作策略”对象。

`DerivedModelRequest`是冻结准备边界的类型，允许在公共入口单点做typed分派，不能一路传入公共投影、网络调用和解码函数。主／派生准备均汇入既有`_ProviderPreparation`／`_PreparedModelCall`的封存与发送合同；共同invocation返回原模型消息，框架解码或派生文本处理由各自绑定负责。callback配置在装配时确定并作为调用值传递，移除`prepared['derived']`及其反复模式判断；模型构造、SDK重试参数、最终wire和日志沿既有owner处理，不复制第二发送器或复制同形准备DTO。主会话事件提交与派生结果提交确实不同，保持各自结果owner，不强行合成一个包含大量空字段／开关的通用收尾函数。

`purpose`只保留调用分类及原sealed-dispatch的身份约束，不作为业务路由表。分类与真实admission由同一执行装配点显式绑定，不能依赖普通Agent的默认分类：当前`DerivedExecutionHandler.step`构造模型未传`invocation_purpose`，而`_derived_preparation`要求derived／compaction，属于须迁齐的装配冲突。来源策略、输出语义、SDK重试和工具权限分别归上述owner；本次涉及的重试设置在原模型构造处一次解析为adapter已有实际参数，传输消费该值，不再在每次sync／async发送中按purpose重复推导。不能仅凭字符串为derived就声称已关闭隐藏重试，也不无关改造其它调用用途。

API、持久化冷读和实际工具执行属于不同信任／状态边界，各自必要校验保留；已经解析的同一运行值不在每层重复JSON解析、重查全历史或重新校验全部字段。新增用途只提供任务参数或在确有业务计划时注册原handler，不往公共模型、工具、日志、Saver加入命名／记忆／团队等purpose分支。现有协议／effect的必要分支按owner保留，不以“零if”为目标新增抽象。

### 5. 简单有界调度

移除“同Thread任意派生就返回已有task”的短路。压缩每Thread一个在途属于压缩业务，不能限制其它用途。

原注册器保留后台一项运行、四项等待；全部派生共用一个总运行上限2，使用原运行task集合核算，不再划分后台／交互各自的保留槽或新增计数镜像。无后台时可同时运行两个交互派生。交互无额度返回busy且不登记等待task；后台按原有界队列处理，不新增交互队列或调度配置。

pending提升复用同一task，有总额度才启动；已运行只等待原结果，不重复spawn、不抢占、不重置期限。取消及终态沿原注册器释放一次额度，ready结果不占运行槽。真实工具资源忙仍由其owner决定，派生优先级不绕过资源准入。

### 6. 一次期限及原调用上限

首次受理固定绝对截止时间，排队、规划、网络及结果提交均计入。活跃执行沿原asyncio期限控制，查询、出队及发送／发布边界发现到期就结算，已过期任务不得随后发出或发布完整答案。排队过期允许在这些边界惰性结算；不新增最早到期定时器、每task常驻计时协程或扫描服务。停止时仍由原注册器排空。

调用上限沿原模型执行／handler已有预算，SDK尝试不得形成隐藏重试乘数；不能受原预算控制的SDK自动重试在原adapter关闭。重复被禁工具仍消耗模型循环上限，但不新增逐工具扣额／并发余额机制。压缩32次及失败补尝试留原业务规则，不推广到全部用途。相对超时只首次转换，重试、提升及查询不恢复期限／额度，也不推导模型输出token限制。

### 7. 暂停、打断和停止复用原控制范围

| 操作 | 派生行为 |
|---|---|
| 主execution暂停／恢复 | 只控制主execution及主输入队列；派生不绑定它的ExecutionPause，不进入该队列或从该队列恢复 |
| 普通interrupt | 由原SessionStopService按SessionStopPolicy的scope及background_task执行；当前内置默认keep，派生继续，用户已有stop策略仍生效 |
| 会话stop | 同一策略入口处理；当前默认background_task=stop，取消范围内派生 |
| 显式cancel(task) | 原BackgroundTaskRegistry取消这一个task及原operation，不反向打断主Thread |
| 删除／后端关闭 | 原资源provider及生命周期排空，不留可再次启动的任务 |

不新增派生pause／resume API、paused持久态、暂停时间扣除或暂停快照。模型／工具节点现有execution_pause_boundary在没有主暂停绑定时按原逻辑运行，派生绝对期限不中止计时。会话stop或interrupt的清理必须真实经过 BackgroundTaskResourceProvider，不能仅靠“不继承父取消token”声称已适配。原压缩业务若需要随等待方中断而取消，显式调用共同cancel；单纯等待断开不隐式取消。

### 8. 完成、取消与恢复三个事实

1. 完整task结果已由原rollout／内容owner提交：随后确认控制库task；进程退出后只补确认，不重调模型。一个chunk或工具结果不算完整task结果。
2. 没有完整结果但原holder仍有效：仅返回真实pending／running，不接管它。
3. holder失效且没有完整结果：原注册器真实终结该次尝试，不自动续跑模型／工具。未决operation继续归原工具owner，不靠重跑猜测副作用。

结果发布、取消和期限沿原短publication gate及fencing裁决。结果先提交，普通取消不能抹除事实；取消／到期先关闭gate，晚到调用只留诊断或部分输出。删除和真实来源失效仍按原合同使结果不可用。取消回执不冒充外部写操作已经回滚；清理错误如实报告。不增加派生专用对账器、恢复状态机或后台轮询。

### 9. 数据归属及保留

来源目标工作区及一个真实SessionThread是存储归属，调用者只持因果关联及结果引用。路径必须由catalog与原resolver取得，S为会话节点、T为Thread节点；不能假设所有子Thread都在S/threads/{thread_id}，不按当前日期、名称或导航父链拼路径。

| 数据 | 现有落点／owner |
|---|---|
| task descriptor、checkpoint、幂等及结果引用 | S/session-control.sqlite，原后台任务表 |
| execution、model call、计划、seal和结果索引 | T/rollout/index.sqlite |
| 新增任务指令、模型／工具正文及详情 | 原rollout JSONL／SQLite内容及detail owner |
| 模型请求／响应诊断 | S/logs/llm_requests/，原日志writer关联真实Thread／execution |
| Trace | S/logs/traces/trace.sqlite，原事件及执行身份 |
| 工具文件、进程及资源 | 原operation／资源owner位置及生命周期 |

namespace不是新物理owner，不创建每任务数据库、fork目录或派生manifest。来源历史和sealed tools通过原内容引用复用；控制库不复制全文结果。原完整wire诊断日志沿原规则记录，不能成为第二恢复事实源。

派生图经原 `TraceEventSink` 写入现有类型事件：创建事件固定真实 Session／Thread 与 task、plan、execution 因果身份，以及冻结模型、canonical 来源截止点和摘要选择原因；`agent_start` 表示实际开始，模型登记后记录实际模型，结束记录完成、失败或取消。失败沿已有模型容量和本地容量异常的原预算报告估算可信度；上游错误未保留估算时明确标为未记录，不重复计数或猜造值。它不向普通 Job 调度总线发布生命周期，也不伪造 `JobService` 才能生成的队列 dispatch 快照；Trace 查询、Thread 索引和实时交付仍由原 Store 处理，结果正文不再复制进终态事件。

普通聊天、主latest、搜索和fork按原view／执行选择过滤派生记录；fork不重启来源task或把未采用结果变成目标聊天，原备份不另造一套。移动改名不搬数据，已知task ID仍须核显式工作区及SessionThread归属，不全盘扫描寻找任务。

运行期间沿原保留机制持必要引用，结束释放自己的holder，结果沿原owner保留。没有既有清理期限就保持现行行为，不因“临时”私设TTL或新增GC。来源删除先关准入、取消任务、沿原operation／资源owner排空再清理；删除一个子Thread不能清空同Session其它Thread的控制行或日志。合法pruning报告不可用，无清理事实的意外缺失仍报真实错误。

仅持结果引用不自动获得独立于来源的永久生命周期。未来业务正式接纳结果时由其owner完成原内容提交或取得独立保留，本次不实现该业务。日志保留不决定结果是否成功，过期查询不重调模型、不复活来源。

### 10. 一个工作区控制入口

沿原路由提供 /api/v1/derived-executions：POST提交、GET /{task_id}查询及有界wait、POST /{task_id}/cancel取消。内部和HTTP使用同一准入函数，原后台资源控制复用同一取消实现；不改造无关资源API或再增派生提交入口。

三种操作明确目标工作区及session_id／thread_id，不依赖导航激活态。提交文本及参数整体沿512KiB上限（已有更严限制优先）有界读取，历史传引用。回执只给task／execution、来源截止点、真实状态及结果／错误／operation引用，不直接序列化完整内部handle；大结果沿原内容reader获取。

相同输入重试返回原task，busy不登记新task，冲突／无效／缺失沿原统一错误处理。cancel幂等且不改写已完成结果；wait结束／断开只结束观察。每次HTTP保留单一request_id，task用于因果关联。常驻更新复用原realtime及其订阅／游标／快照约定，不新建连接、事件服务或轮询，重连仅查询原task。

### 11. 验证与复杂度约束

校验只在原owner的实际变化边界执行：受理核来源和显式参数，seal核最终请求，工具入口核现行策略，发布核期限／fencing，状态查询读索引及引用。按第4.1节核参数消费者和模式分支：共同invocation不读取派生布尔开关或任意purpose业务路由，工具不重复解析完整请求。不得每轮全历史hash、审计目录、初始化全部插件或重新构建父Graph。

暖控制响应完整预算p95≤500ms、单次≤1500ms，冷读≤3000ms，既有更严门优先。计时经过真实Gateway至完整响应，包括本地排队、校验、内部请求和序列化；有限wait及模型／工具等待单列，不以快速受理代替完成。使用既有确定性模型／工具装具和100k历史、有界active selection，保留至少20次暖样本，不新建生产性能监控。

验收以少量真实入口组合覆盖：同键并发与热更新重试、主Turn忙且暂停、工具循环与拒绝、总额度与提升、发布／取消及崩溃、主／子Thread路径和删除、Gateway scope与延迟。纯参数错误沿原schema测试，owner已有充分测试的通用行为复用，不为每句规范各写一套测试。

### 12. 与压缩change的分工

共同准入、引用、模型／工具运行、缓存、任务控制及外部接口由本change实施。压缩原change只负责candidate／chunk／merge计划、提示词、全覆盖与缩减、阈值及学习反馈、候选采用及其业务验证；唯一模型及32次预算等限制仍属压缩。

原rollout当前SQLite目标schema34由压缩change登记：schema32语义Item、schema33候选／直接前序结果／原文行范围、schema34候选行 `apply_on_next_request`。该列只协调显式手动下一请求采用，HTTP／模型工具可提升同一压缩task；它不成为公共派生模式、冻结输入字段或新账本，本change的参数化派生请求不因此触发主上下文压缩。schema32／33未发布，不据开发中间态新增自动升级或旧结构双读；新库仍走原owner唯一初始化。

压缩首次上游失败终结后的唯一关联补attempt复用原task／累计调用预算，只在既有控制行metadata.deadline与唯一retry lease的同一CAS事务登记本次前台期限；descriptor／冻结输入保留首次期限身份。共同注册器的运行、发布与恢复读取handler提供的当前attempt期限，查询及在途提升不重置；参数化派生请求不因此获得额外自动重试策略。压缩的显式manual失败新评估和S1→S2尚未消费采用意图仍归原压缩owner。

原change任务3.6／3.7和5.1／5.4／5.5／5.6的公共部分引用本合同，不重复派单。保留已有基础及验收证据，共同capability先归档、压缩后归档，不让无关工作互相等待。

## Risks / Trade-offs

- [精简后没有派生暂停或严格结构化输出] → 本次API如实只提供文本与取消；后续沿原控制／输出入口扩展。
- [保留tools后模型反复请求被禁操作] → 原执行点配对拒绝并消耗已有模型调用上限，不改wire或无限纠正。
- [排队过期惰性结算] → 查询／出队／发送前核期限，不发过期任务；原有界队列最多四项，不加期限定时器。
- [临时执行仍会有外部副作用] → 已授权工具用原operation事实，不增加自有回滚／恢复系统。

## Migration Plan

1. 核当前公共字段及全部直接／间接消费者，迁齐原typed引用、模型入口和reader；共享文件先协调写入。
2. 接入参数化handler、原图工厂及工具策略，按第4.1节集中调用绑定与准备边界，迁齐成对可选回调、prepared派生布尔开关及发送时重复purpose推导；压缩迁入共同核心。迁齐后物理删除被替换入口，不保留旧签名或再包兼容层。
3. 原注册器补总准入及原暂停／打断资源控制适配，完成真实入口验证后再勾任务。
4. 必要结构变更归原数据库owner及版本约定，不凭开发中间态增加兼容或自动转换真实数据。归档顺序沿第12节，回退沿原提交及备份，不增加运行时格式回退。
