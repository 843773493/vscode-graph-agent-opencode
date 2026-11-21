## Context

当前 `context_notices.py` 从已派发的 model call selection 查询 public `runtime_notice`，历史 reader 将其投影为 `context_notice`。查询排除了 request-only contribution，无法覆盖静态 System Prompt、工作区指引和 Skill。现有 `ContextNotice` 点击展开时读取全文，再在客户端截取预览；Workbench `itemActions` 先读正文选择打开方式，`itemDocuments` 又可能重复读取。

已有 `ContextContribution` 保存来源版本、hash、长度及 assembly 归属；`assembly/content_store.py` 提供不可变内容 DAG，`context_assembly_contributions`／`context_assembly_selections` 提供查询基础。本设计不假定所有来源已完整封存，也不假定现有 assembly reader 已支持单段读取；实施第一步须核对真实写入和读回。

现有读取限制有具体代码依据：`domain/itemized/enums.py` 已有 `DetailProtection`；`mapping/itemized/history_turns/item_content.py` 拒绝打开非 public 或 opaque Item 正文；`checkpoint/owner/context_source.py` 的普通 overlay 同时使用 `visibility="internal"`、`protection="public"`。这些是既有数据／读取约束，不代表本功能要建立保密制度。System Prompt、工作区指引和 Skill 不因为默认隐藏而被认定为秘密，本次不新增权限、分级、审批或脱敏流程。

### 实施核对：现有来源、消费与读取链（2026-11-19）

- `agents/instruction_producers.py` 的 R01/R02/R10、`middleware_assembly.py` 的工具指引及 `skill_context.py` 的 Skill／catalog 均向原 ContextSourceManager 登记。`checkpoint/seal/preparation.py` 在真实准入边界取得来源批次；`runtime/prefix_compiler.py::source_contributions` 将完整来源和变更 notice 分别登记为有版本的 contribution。不能只从 canonical runtime_notice 查询这些来源。
- `checkpoint/owner/composition.py::register_context_contribution` 的 SQLite registry 和 ledger 只保留 manifest，正文不在 contribution 的 `body` 中常驻。`checkpoint/owner/context_projection.py::_bind_request_detail_refs` 已为每个实际选入的 request-only ref 准备 `request_source`／`request_replay` detail，并将精确 DetailRef 放入 selection；`checkpoint/seal/lifecycle.py` 和 assembly 封存事务共同发布正文、绑定与请求。普通 internal 来源的 protection 为 public。
- 完整规范请求已经由 `assembly/content_store.py` 的不可变 DAG 保存；原 request detail 是单段来源的既有权威正文。因此本次定点读取沿 `context_assembly_contributions → included selection → DetailRef`，不再向 DAG 添加另一份来源正文，也不通过完整 assembly reader 展开所有 DAG 节点。只校验所选来源的历史 owner、版本与原 detail，沿原 8 MiB 读取边界返回；需要完整请求时仍使用原 DAG 重建及 request_hash 校验。
- 普通用户打断由 `business/session_interrupt_service.py` 接受后，经 `MessageService`／`runtime_reminder.py` 提交 `observe_pending` 的 checkpoint_reminder，半成品由原流收尾独立保留；内部 execution 的打断在 `checkpoint/owner/internal_execution_control.py` 的 rollout 接受事务中同时提交提醒。`checkpoint/durable/queries.py::execution_context_selection` 按后续 admission 的 commit fence 接纳此前用户执行的 `pending_next_turn` 通知，`seal/preparation.py` 再封存实际选集。因此提醒的 producer execution 和实际消费 call 必须分别保留；仅存在 pending Item 不能生成标记。
- 原 `storage/catalog/context_notices.py` 仅查询 public canonical runtime_notice，以“历史任何先前调用曾选入”去重，并依赖该 call 的可见输出定位。它尚不支持 request-only、A/B/A／选择中断重引入、用户消息前锚点及明确 continuation。`rollout_history/reader.py::_indexed_page` 批量挂载到 response_parts，`mapping/itemized/history_turns/context_notices.py` 负责纯映射；新位置必须同时迁齐这些消费者，不能只改组件。
- `ContextNotice.tsx` 的展开会先读全文再截预览；`workbench/react/itemActions.ts` 为选择打开方式先读正文，`itemDocuments.ts` 的 FileService 随后再次读取。两处须迁为元数据选择与接受后单次正文读取，正文不进入历史镜像。`conversationMerge.ts`、`messageStreamProjection.ts`、`transcriptItems.ts` 也消费原 context_notice，必须保留 live/history 合并与真实位置。
- 普通 Item 的格式列表仍依赖原 `item_contents` 对选中 payload 的投影（文本 fence／JSON、多个媒体块及工具输出），现有 catalog 没有可直接返回的格式清单。`content_metadata` 只减少选项响应的正文传输，不减少该次服务端正文读取；原普通 Item 流程本就有“选格式→编辑器读取”的成本。本轮不为消除此成本增加索引或正文缓存。上下文标记使用已知文本格式和冻结引用，来源选择／取消零正文请求，接受查看由编辑器单次读取。
- `fork/copy_items.py` 已收集 sealed selection 的 detail 依赖并保留历史 namespace 和原 DetailRef；`assembly/detail_retention.py` 已保护 sealed/terminal assembly 的选入 detail，不需要新引用账本。发现一个具体缺口：`runtime/detail_store.py` 即使正文仍受上述引用保护，也会在默认 30 天后拒绝读取必需回放正文。本轮在原读取 owner 修正此处，GC 仍只回收无引用候选，普通可选详情的到期限制保持原合同。
- 回归基点包括 `test_context_notice_history.py`、`rollout_context/test_assembly_content.py`、`test_context_plan_detail_store.py`、`test_rollout_fork_transfer.py`，前端包括 `ContextNotice.test.tsx` 和原 Item 菜单／只读文档测试。上述核对是源码事实；真实停止→续发、Fork 和浏览器性能仍须通过本变更的产品入口验收，不能用本节代替测试证据。实时接入当前统一 `/api/gateway/realtime` owner，尚未接通派发后的 marker 失效通知。

## Goals / Non-Goals

**Goals:** 默认历史仅增加有界元数据；用户能知道某处存在上下文文本，通过右键查看当时冻结正文；历史／实时／Fork 复用同一来源与顺序；正常会话读取成本不随正文总量增长。

**Non-Goals:** 不把 request-only 来源转为普通消息、Turn 或活动计数；不展示所有候选计划；不增加工具 schema 的默认时间线标记；不新增快照体系、全局索引、常驻轮询或正文缓存数据库；不改变模型请求选择、组装和执行；不扩建权限、快照清理或大文件处理框架。工具定义仍沿原请求详情查看。

## Decisions

### D1. 两类上下文引用，共用一个标记与正文读取合同

- canonical 提示继续使用真实 Item 引用。
- request-only 提示使用所属历史 scope、`assembly_id` 与该 assembly 的 `contribution_id` 定点引用；来源 revision/hash 来自封存记录，不能只靠可变 source alias。
- 对外引用采用明确区分的类型，扩展原 inspection DTO 和正文 owner，不引入平行的“聊天提示读取服务”，也不为贡献制造 `item_id`。内部路径、数据库 detail key 不直接作为客户端可任意读取的路径。
- 元数据包括稳定标记身份、引用、来源类别与名称、revision、正文长度、已知可用状态、所属实际 model call 和位置。状态仅报告索引中已知的事实，不为显示状态逐条打开文件，也不保证未探测的正文一定可读；实际读取失败由原接口明确返回。缺失的可选名称不触发正文读取。
- 内容查看必须区分“来源冻结正文”和“完整请求的组装结果”：一个合并 system wire item 可以由多个 contribution 组成；查看某段不能声称它等同于整个最终 System Prompt。完整请求继续沿原请求详情入口。
- 隐藏片段也可能以 user/human wire role 或普通用户消息中的注入片段承载；发现与定位以已有结构化来源、冻结内容引用和 selection 为准，不能只匹配 system role／`runtime_notice`。普通用户原文仍显示原气泡，额外内部片段可由标记查看。若 producer 尚未为这段注入保留独立冻结定位，在原 producer／内容 owner 补齐，不由前端解析标签或把整条用户原文当作内部来源。

替代方案：把所有来源转成 Item 会改变生命周期和历史语义；直接返回 assembly JSON 会破坏轻量读取；使用当前配置会失真。因此均不采用。

### D2. 标记是派生展示，不新增业务事实

默认覆盖实际派发请求选入且未以普通消息完整展示的上下文文本。普通 internal 来源照常可查看；已有非 public／opaque 内容继续走原读取接口的既有行为，不在本功能中扩张或重新定义。不新增“禁止查看 System Prompt”等策略。未选入、未派发、仅工具 schema、已作为委派任务等普通展示的内容不生成重复标记。

发现入口是已派发 assembly 的 included selection，不是简单枚举 contribution 表。沿现有 typed ref／binding 解析 canonical、request-only 以及实际选入 overlay 所需的 backing contribution；同一选入文本与其 backing 不重复生成标记，未选入 backing 不自行展示。来源版本沿现有 source identity／revision／hash 判定，不增加独立的“来源启用／离开”持久状态机。

位置由历史 owner 提供，区分实际消费引用与展示锚点；不能把所有标记都塞进 Turn response parts 的首条 assistant 前面：

1. 首次实际派发时使用的 System Prompt、初始内部 user/human 注入组成初始标记，显示在 transcript 的第一条真实用户消息之前。它是时间线开头的普通成员，滚动离开后不常驻、不置顶；只有加载到包含真实起点的历史页才显示，不把任意已加载页顶部当作起点。
2. 旧 execution 的打断产生 pending `checkpoint_reminder`，实际被后续请求选用后，在该后续请求关联的新用户消息之前显示标记，位置在旧轮次已保留内容之后。旧 execution 是产生来源，后续请求是消费来源，展示不能因为前者归属而退回旧轮次。新用户消息已显示时，补入标记仍须放在它上方，不追加到回答末尾。
3. 同一 Turn 中后续模型调用才首次引入的其它提示，仍以该调用边界为锚点，不全部提升到 Turn root 之前。

已有标记 metadata 扩展可区分 `before_user_message` 与 `before_model_call` 的展示锚点，使用真实 message／Turn／model call 身份；引用和顺序仍由现有历史 owner 生成，不伪造 canonical ordinal、修改真实 wire request 顺序或新建事件存储。标记内部按 sealed selection／contribution 顺序排列；没有输出的失败请求仍能锚定输入／请求边界，不依赖下一条 assistant 存在。

同一请求边界合并为一条居中虚线，菜单可以列出内部来源；同一来源版本持续带入不逐次刷屏。当前可见 lineage 中首次使用、来源有效版本变化或实际选择中断后重新引入时显示，A→B→A 仍是一次可见变化；重试复用相同输入不重复。不能只用正文相同判定身份。原数据库索引判断已派发请求的必要前驱，不逐条恢复先前 assembly、不把本页首条误当首次使用。完整请求详情始终能查看未重复标记的全部来源。

分组还须保持展示锚点一致；同一 model call 中应位于用户输入前的提示不能与应位于输入后的提示合并后错位。新增打断事件按真实事件／Item 身份区分，不因提示模板或正文相同被去重为旧事件。pending 提醒尚未实际选入派发请求时不冒充“已使用上下文”；续发后的 marker 必须在该请求派发后、响应仍运行期间可见，不能等终态或刷新才出现。

### D3. 历史元数据沿原页面返回

扩展既有历史 summary／metadata projection 和 context marker 结构，不增加页面初始化的串行请求。每页在原 read snapshot 内按 Turn／execution／model call 批量查询 SQLite，只读取返回页及必要的索引前驱信息。不打开 JSONL、不恢复完整 assembly、不逐来源重新计算 hash，不从 LLM 日志反推标记。

沿用历史页字节／数量预算。标记超出返回预算时给出明确 continuation，可通过用户动作加载后续元数据；不默默截断，也不在后台自动排空。标记子分页不拆 Turn、不推动真实历史游标，不在更多页已加载后重复显示旧标记。

实时通过现有观察通道提供相关历史 revision／范围失效通知，由原历史 owner 补取对应页元数据；只在派发证据可读后通知，不广播正文、不新增每标记 SSE 或轮询。现有 message stream 更新不得覆盖标记，关闭视图沿原生命周期释放观察者。

新调用可在 Turn／canonical history revision 不变时完成 assembly 派发，实施须核现有观察游标能否覆盖这一事实；不能只监听 Turn 终态。若原 revision 不覆盖，在现有历史／观察 owner 增加必要失效通知或关联游标，不新建标记状态源。前端不得因“Turn revision 未变”丢弃新标记。

### D4. 冻结正文在现有 owner 内按引用保存和读取

在现有 seal／dispatch 事务边界，核所选文本均能通过原不可变内容 owner 精确读回，必要时补齐该 owner 的内容保存和定位信息。引用保持所属 scope、assembly 和来源版本绑定；保存相同内容复用现有 DAG，不写新的聊天副本。

轻量查询不调用完整 `get_context_assembly`；右键读取也只打开所选正文及必要依赖，不重建整份 request hash preimage 或全部来源。封存时核完整组装、正文绑定与 hash；元数据暖读只核 scope／view／索引引用；正文读取核目标绑定及所读内容的必要完整性，不逐次审计整库或整份历史。不可变内容按原 owner 生命周期复用核验，不新增全局校验缓存。

修改配置、卸载插件、重启后端和 Fork 后均读取历史冻结版本。沿既有冻结 namespace／lineage 和保留规则维持引用，不再要求来源文件仍存在。本次只核现有清理路径是否会误删仍被历史使用的正文，发现具体问题在原 owner 修复，不新增垃圾回收、引用计数或 retention 策略。没有历史材料时返回明确不可用；不得用当前配置伪造，也不因为缺旧材料自动增加开发数据迁移。

### D5. 交互只在明确菜单操作后读取

默认标记是一条居中虚线，使用主题颜色与足够命中区域，不常驻大卡片、标题、正文或提示词摘要。悬停及键盘焦点只使用元数据；右击或 Shift+F10 打开原 Workbench 菜单。多来源时菜单用元数据选择来源，接受“查看上下文内容”或“复制内容”后才请求正文。左键不读取正文；若用于展开来源列表，只显示元数据。

复用原只读文档 Provider 与编辑器打开规则，URI 绑定完整目标和冻结引用，不能跟随当前焦点工作区。先用名称、媒体／语言等元数据选择展示，再由编辑器读取正文；复制直接调用同一个内容 owner，避免“预读选项→编辑器再次下载”。编辑器关闭后由原 FileService／文本模型释放正文；历史镜像和历史缓存只保留标记元数据。

正文读取复用原取消、大小和响应期限机制。所选正文由只读编辑器持有，不挂进聊天 DOM；超过现有上限时明确提示大小及限制，不静默截断后宣称全文。若现有入口缺少边界，只在原内容 owner 增加本次读取所需的直接限制，不以“沿用大文件能力”为由假设已有完整方案或另造分页／流式框架。目标不可用、引用失效、数据损坏和原内容接口拒绝均明确呈现，不自动切目标、不重试到当前配置。查看行为不改变 context view、输入队列、任务或已发送请求。

### D6. 模块与验证边界

| 责任 | 既有位置与修改方向 |
|---|---|
| 封存和正文 owner | `rollout_context/assembly`、既有 contribution／detail 存储；补齐冻结绑定与定点读取 |
| 轻量查询 | `rollout_context/storage/catalog/context_notices.py`、checkpoint reader；扩展原查询，必要索引归该数据库 owner |
| 历史投影 | `rollout_history/reader.py`、`mapping/itemized/history_turns`；统一 summary／detail／Fork／元数据 continuation |
| API／协议 | 原 history／items inspection 路由、DTO 与 proto；复用类型生成与原 Gateway 代理 |
| 实时 | 原历史更新／观察通道；通知已提交的元数据范围，不另建传输机制 |
| React | history state、timeline、`ContextNotice`、`itemContextMenu`；统一引用和低干扰标记 |
| Workbench | `react/itemActions.ts`、`itemDocuments.ts`；原菜单及只读编辑器支持贡献引用，避免重复读取 |

少量定向验证覆盖写入→封存→派发→分页→右键读取真实入口；补一个真实浏览器场景和原性能装具场景，不默认全仓测试。健康本机回环按既有128 Turn fixture 保持历史 reader p95≤200ms（目标100ms）及浏览器锚点恢复≤200ms；普通不超过512KiB正文接口暖读p95≤500ms、max≤1500ms，首次正文在原版编辑器可见≤3000ms，若其他规范更严格则取更严格值。20个暖样本保留原始值、p50／p95／max、request_id、响应大小与正文读次数；大正文另列数据规模和原大文件边界，不能混入普通样本掩盖问题。使用大量无关历史和大来源验证元数据查询没有全量读取，统计来自测试装具，不新增生产审计或监控。

### D7. 必过集成测试：新建、打断、续发和历史恢复

复用 `tests/integration/clients/web/itemized_context/` 与原消息流／打断的正式装具，形成一个完整真实浏览器场景。工作区写入对应正式测试的 `out/tests/<同名测试路径>/workspace/`，产物按原 run 目录保存；不得使用用户会话或项目根目录。使用确定性上游模型服务控制流式输出与暂停屏障，业务 API、Gateway、Backend、快照存储与 Workbench 全部真实，不预写 marker、不替身 history／inspect、不直接调用 reminder helper 充当中断流程。

| 阶段 | 必须断言 |
|---|---|
| 页面新建并发送 U1 | 等待首个请求真实派发、保持执行中；初始标记在 U1 上方且是 transcript 第一个内容成员，包含测试已配置并实际注入的 System Prompt 和内部 user/human 来源；U1 原文仍是正常气泡 |
| 初始标记右键查看 | 悬停和菜单打开／取消零正文读取；接受查看后才定点读取，原版只读编辑器正文与该请求冻结来源准确一致；关闭编辑器回到会话 |
| UI 打断首轮 | 点击原停止操作，确认旧执行真实取消并保留已有 partial／工具结果，等待原 reminder producer 的持久化回执；不凭按钮变灰认定打断成功 |
| 再从 Composer 发送 U2 | 确认同一 Thread 的后续请求实际使用该次 checkpoint_reminder，在回答仍运行期间出现顺序“旧轮次已保留内容→打断标记→U2→新响应”；初始来源不在 U2 前重复 |
| 打断标记右键查看 | 核引用关联该次真实打断及后续消费请求，接受查看后正文与冻结提醒全文一致且独立于 U1／U2；不从模型回复文本推测注入成功 |
| 纯历史加载 | 完成后续请求，记录标记身份／来源／锚点；关闭正文编辑器并刷新同页，再关闭会话 Tab 后重开，各阶段默认零正文请求，标记位置和身份保持一致；重新右键仍能读取相同冻结正文 |

初始标记可以同时包含实际注入的多个来源，断言按结构化来源身份匹配，不能只搜索页面有“System Prompt／打断”等字样。测试分别校验 DOM 相对顺序和后端 metadata／封存请求选择；不能以菜单能打开、任意一个 marker 存在或预置历史页面通过代替完整场景。对 pending 未消费状态、正文同模板但不同打断事件及不包含起点的历史分页位置，可在现有后端定向场景补验，不重复运行整套浏览器。

用上游暂停屏障避免模型速度决定中断时机；派发及用户打断的外部等待单列，服务提交派发事实到 marker 可见的内部延迟必须沿用普通交互预算：暖态≤2000ms，首次未缓存≤3000ms，不等待回答完成、不靠自动刷新。保留准确命令、源码指纹、截图、请求ID、请求计数、测试实际退出码，按原进程外期限／内存保护运行。这是变更完成条件，不能用单测、类型检查或构建替代。

## Risks / Trade-offs

- [来源正文未完全冻结] → 先核真实 producer 的封存与重启读回，不用 LLM 日志或当前配置补缺口。
- [去重需要跨页信息而退化为全历史扫描] → 同一数据库内沿身份／版本及派发顺序索引获取必要前驱；SQL执行计划和大规模读取证据必须验收。
- [Fork 位置与原 owner 混淆] → 使用既有冻结 namespace 和可见 view 映射，正文身份与显示锚点分开，不按物理相邻行推断。
- [接口类型变化影响既有 Item 菜单] → 全仓检索生产／测试调用方，一次迁齐，不保留新旧 inspection 合同双轨。
- [实时基础设施并发变更] → 接入实施时的唯一观察 owner，不在本文固定新 transport 或另起订阅。

## Migration Plan

按 tasks 的纵向切片扩展既有链路，数据结构需要修改时仅使用数据库 owner 的有序迁移，并遵守最近两次已验收 release 基线。已有冻结记录能直接查询的复用；缺失正文明确标记不可用，不自动重建开发数据、不重放模型请求。上线使用同一新协议及生成客户端，不保留旧标记读取旁路；回退仅针对代码／支持基线的既有发布流程，不能删除已封存历史。
