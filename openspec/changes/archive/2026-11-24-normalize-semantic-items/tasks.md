## 1. 冻结唯一合同与受影响入口

- [x] 1.1 检查共享 Git 状态和 owner 指引，重读前置变更 `unify-conversation-process-navigation` 最新文件；确认三层折叠／非 Part 节点保留，本变更替代 Part、轮廓索引及相关坐标，不覆盖其它任务代码。
- [x] 1.2 全仓枚举 Item writer／codec、group reader、Provider／LangChain／summary-source、finalization、存储索引、history／search／URI、操作与前端消费者；区分 Provider 原 part 来源和项目自建 Part，列入本文件实施备注，不另建台账。
- [x] 1.3 核实际 owner 版本和发布来源，登记 canonical3、当前集成 SQLite schema34、阅读3、history cursor3目标及必要生成协议变更；语义基础 schema32 与后续压缩 schema33／34 沿原 storage owner 顺序登记，不把早期 SQLite31 索引切片当成最终结构或兼容来源。核对既有 tuple／group／hash 语义，不把版本号更新当作转换完成。

## 2. 统一 canonical 写入与消息组重建

- [x] 2.1 在原 domain／codec 内更新 Item 粒度及消息组合同：普通 assistant 顶层 thinking／text／media 和工具调用独立 Item，去掉 tool_call-only 组 anchor 限制，保持单回执、唯一用户 root、签名载荷等原子边界。
- [x] 2.2 将工具专用展开推广为唯一语义拆分入口，迁齐无工具、多工具、流式提交、内部 execution 和已有 canonical 复用路径；删除原普通结构化输出整包写入与重复展开函数。
- [x] 2.3 迁齐 checkpoint／LangChain／Provider request／summary-source 的组重建与来源去重；保留原角色、str/list、空消息、invalid_tool_calls、媒体引用、签名、密文、工具配对与消息级 usage 唯一归属。
- [x] 2.4 更新 finalization 和其消费者：final_item_id 指向明确最终输出组末输出 Item，完整回答沿该组的有序输出 Item 展示；移除聚合 final 内容事实、大 Part ordinal 排序和最后文本推断，保留原结果状态。
- [x] 2.5 用现有 fixture 通过真实 writer 做往返检查：无工具／带工具的相同混合块、并行调用、受保护思考、媒体、空／失败／中断和用户附件；补最少有判别力的用例，核真实序列化而非手工填索引。
- [x] 2.6 固定单个tool_call的基数：沿原payload的tool_calls只承载一次声明，原Provider数组在组重建时按ProjectionGroup成员顺序恢复；保留ModelCall／消息组范围、真实调用ID和全部内容成员ordinal，迁齐校验与调用方，不另造batch／call_ordinal或递归拆参数数组；区分原消息组、活动组与执行流，不凭展示分组合并原消息，不以parallel_tool_calls等请求选项推断调度。
  流式入口复用原adapter的调用来源累计，核B片段先到且A参数未完整时仍保留原声明顺序和独立partial／invalid状态；不以Provider索引直接生成Item身份／组ordinal，不补占位调用或猜组大小。
  核消息组完整发布不被扩大为执行屏障：已满足原启动条件的B不额外等待A参数、组提交或其它回执；保留原本等待完整模型响应的调度规则，不新增提前执行入口或批次执行事务。

## 3. 历史查询与公共协议一次迁齐

- [x] 3.1 在原 history DTO／protobuf 和实时提交合同中统一 Item 内容投影、draft／commit 关联、response_items／display_items 字段及类型；迁齐 Python SDK／TS 生成协议，删除旧字段号和名称时保留 reserved，不改第三方协议字段。
- [x] 3.2 改 summary／detail／internal execution／上下文正文与搜索读取，直接消费已有 Item 轻量投影和有界 locator；媒体不被 text-only mapper 丢弃，授权与保护规则保持。
  非文本切片已贯通：原typed manifest只镜像output_block_type，公开content投影沿原定点读和预算交付单块原对象；默认摘要无正文，final／内部输出及未知块不因空text丢弃，超限不切断JSON／URL／base64。此切片不代表搜索、所有上下文入口或整个3.2已验收。
- [x] 3.3 将活动组边界、item_count、elapsed 和 final 投影改为真实 Item membership；保留中间非 final 文本／媒体，排除原 root／final／纯密文／carrier 等，工具合行不合并其业务计数。
- [x] 3.4 在原 cursor codec 中一次迁齐主历史双lane、around、activity 和 execution 模式到目标合同，移除 Part 续点，保持绑定、首次 read fence、selector／include互斥、最多四项定点回补与256 record／字节预算。
- [x] 3.5 核首次窗口、零 Turn、长组中段定位、跨页边界、正文超限和乱序片段：旧 binding 拒绝、同 binding 轻量片段不降级已加载正文，定点Item不遍历全部活动页。
- [x] 3.6 迁齐后删除 response_outline_json、复合 activity_outline／internal_item_outline生成和SQL json_each展开；保留现有activity_outline请求语义及仍有真实消费者的消息投影字段，不新增第二份轮廓或展示索引。
- [x] 3.7 核ToolCall声明事件与ToolExecution真实执行事件的writer，沿原执行owner／提交恢复入口持久并投影最小invocation／attempt／execution身份、范围、状态及起止证据；用execution_completed_at区分真实完成观察，保留completed_at／completed_seq的镜像发布含义，不猜未分配序号或重写不可变Item。默认tool_summary／activity_outline／internal摘要可用，保持当前一个scoped call一个attempt，不新增attempt数组或重试调度；实际详情沿原预算按需读取。缺原证据明确不可得，不靠Trace／终态snapshot回放、不新建执行日志／批次表／逐调用API；fork／rewind沿目标冻结证据读取。
  原声明组来源同时贯通writer、typed manifest、公共DTO／protobuf、实时commit／重连和历史摘要；draft不猜组size／ordinal，提交后按真实来源接续，不能只在后端payload保留或依赖前端记住原数组。
  公开source.message_group统一完整对象或缺省／null的语义，核生成协议不补零值组；call与result保留各自原消息来源，结果配对和工具合行不复制声明组，不增加declaration_group副本。
  原结果payload.tool_outcome沿写入时typed manifest安全镜像、SQLite、公开Item DTO及生成协议独立交付，summary／detail／internal和实时使用同一映射；null与已记录unknown分开。调用投影只从精确关联且view可见的结果派生，不将marker写进call canonical或当作canonical顶层字段恢复。批量关联以本页工具为输入，不先取整轮全部调用；组末locator的使用与结果可见性分开，不靠可见组末或补读正文才能取到摘要。
  确认取消的execution outcome cancelled贯通原DTO／protobuf／生成客户端和实时／历史，不被白名单丢弃；与取消请求及outcome_unknown分开。结果和结束观察独立保留，不将请求／恢复清理时刻写为实际结束，不扩展调度或新增取消状态机。
  起止必须沿原invocation／callback源端冻结并贯通事件和提交，不在processor延迟消费、完成锁、文件更改记录或gather汇总后重新取时；核源端观察切片已替换的complete_tool采集点及其余调用／恢复消费者，不能仅把值字段改名。无可信来源不补造执行边界，不新增计时服务。
  合法request／runtime结果载体保持独立Item及原正文survivor规则；调用摘要只合并view／read fence内精确关联载体的非空安全事实，冲突明确报错，不以载体先后选证据、不增加执行次数或持久聚合层。

## 4. 实时、三层展示与客户端阅读状态

- [x] 4.1 迁齐实时 reducer／timeline／缓存合并到 Item 投影，draft沿真实来源在提交时收敛；不伪造Item／tool ID，不按文本去重，不保留Part别名、两套内容数组或重复状态树。
- [x] 4.2 更新三层过程、单项详情、工具合行、压缩摘要、媒体和final渲染；final与摘要／终态折叠一次更新，同一Item移父容器时节点身份不变，不丢失原操作和键盘入口。
- [x] 4.3 在原transcript节点与阅读控制器中移除Item source.part，使用item_id＋slot；长正文保留客户端内容内位置，标题／虚线等沿原独立来源，不为其创建canonical Item或新注册服务。
- [x] 4.4 更新原视图owner的阅读格式、折叠键与缓存失效；Web／Electron／其它浏览器隔离保存，各编辑器沿原运行位置规则，不新增后端游标同步或清空整个IndexedDB。
- [x] 4.5 迁齐Opener／搜索命中／引用／上下文菜单与around入口到Item定位，覆盖主／子Thread和无Turn execution；重复跳转、取消、用户手动折叠、迟到响应及真实错误归属保持原合同。
- [x] 4.6 用现有前端测试装具覆盖分页插入、虚拟卸载、折叠祖先、长文本重排、final多Item、媒体增高与两个独立客户端；不为CSS文案、计时tick或每个字段增加重复测试。
- [x] 4.7 工具行固定声明顺序并按真实attempt独立结算；声明完整但未启动即可显示，执行开始／完成与结果合行不改用execution身份定位内容。只有同一可比较范围内真实运行区间重叠才显示并行，缺证据只显示同批／已知状态；不同声明组可实际重叠但不能合批，两两重叠不传递。核call completed不当执行成功、一个结果不结束其它调用、取消不当停止、部分attempt重叠不标整批；同binding迟到摘要不倒退已知终态，换view不泄漏旧结果。默认不强加并行徽标，关系解释只消费已加载摘要，不阻塞列表、不新增请求／重试／依赖调度或全局并行关系缓存，不在流式渲染中执行全历史平方级配对。
  配对不使用call_index／provider_call_index、普通message_sequence或本页唯一候选代替真实调用身份；结果沿声明原ModelCall关联。仅声明使用中性状态，已有结果而缺时序仍保留真实outcome，completed不直接等于成功。原合并入口保留detail正文同时接受同binding的新执行元数据，不为此引入第二状态树。
  跨消息沿当前view逻辑顺序，组ordinal不得用于全局排序；只取得回执时保留其独立Item，补入调用合行后仍能按result URI定位真实回执，不新增锚点别名或页外占位调用。
  沿原计时消费者区分Item elapsed_ms、attempt实际运行区间和Turn duration_ms；工具详情不以声明／提交间隔补运行耗时，call／result合行不重复计时，并行工具耗时不相加成过程耗时。不新增批次统计字段或计时状态树。
  详情若解释两个执行的先后，沿完整真实区间说明实际方向；B先于A执行仍按A、B声明行序展示，不将不重叠时间关系称为串行调度或结果依赖，不新增持久关系字段。
  明确关系及计时只描述本次invocation／attempt；工具返回后台job／资源引用不意味着后台对象结束或成功，不用后台生命周期延长调用区间或覆盖结果，不新增跨服务订阅／状态树。
  按design第2.1节的渐进展示对照消费公开来源与执行摘要；组信息／执行证据后到仅更新原行，不重建批次容器、折叠选择或焦点。工具内部参数并发／子请求／多块回执不扩展为调用行或新的执行子图。

## 5. 操作边界、上下文与存储升级

- [x] 5.1 全量迁齐fork／rewind／replay／compaction／context selection到真实Item边界和原能力核验；删除通用content_part操作锚点与Part级稀疏索引合同，不损坏仍有真实用途的Provider block来源。
- [x] 5.2 在原owner登记目标格式及唯一新库结构，创建隔离新Home／fixture验证当前格式直接启动；不让普通业务reader自动改写既有数据，不为开发残留增加兼容reader。
- [x] 5.3 核支持来源：有可核验release／用户额外授权时，按design的原owner转换流程迁齐canonical、所有view／checkpoint／final／封存selection及索引；无支持来源则保留原件并记录本项不适用的证据，不推测清理或借用旧v29→v30授权。
- [x] 5.4 对实际支持的转换核备份／WAL、事务失败恢复、中断重入、重复启动、完整引用及封存Provider请求等价；新canonical可以改变字节和hash，但原请求审计不能被悄悄改写，目标plan hash不得冒充原plan hash。
- [x] 5.5 对有支持来源的阅读记录只在目标Item对应唯一时转换；未支持格式明确恢复受限并保留原记录，不静默丢Part坐标或删除其它客户端数据。未授权真实Home转换不计为已执行验收。

## 6. 集中验证真实产品与性能

- [x] 6.1 先完成相关Python静态检查、生成协议一致性及真实前端／Workbench类型检查；完整迁齐后运行 `bun run --cwd src/clients/web build`，不以转译成功替代类型检查。
- [x] 6.2 运行对应最少定向后端检查：真实writer往返、history摘要／详情／活动分页、工具未知结果、final多块、无Turn、fork／压缩／请求编译及必要转换；全部带进程外timeout／内存保护，失败先归因，不裸跑全仓。
- [x] 6.3 经真实Workbench→Gateway→Backend验收会话打开、刷新、三层折叠、Item URI／搜索跳转、分页保位和两个独立客户端位置隔离；保持现有统一WS，不向项目根注册工作区，正式／临时产物各用既有规定目录。
- [x] 6.4 复用六会话和窄拖拽场景，核箭头靠右、信息隐藏顺序、Composer可操作、键盘焦点及目标保位；保留必要截图，不新增一套前端样式规范。
- [x] 6.5 用固定128 Turn且含混合块／大工具结果的fixture保留首次与20次未筛选暖样本，核history p95≤200ms、已有内容开关／保位单次≤200ms、网络展开p95≤500ms且单次≤1500ms、首读≤3000ms、Composer每次≤2000ms；完整计到响应解析或真实内容可读，保留原时长、p50/p95/max、request_id、规模、字节、命令、源指纹和退出码。
- [x] 6.6 复用真实writer／工具执行入口与少量前端合同场景，对照同批[A,B]的先后执行／真实重叠、B先完成、重试及跨ModelCall重复ID、取消／进程丢失／缺起止、中段分页与部分view；特别阻塞A的canonical提交／完成镜像后启动B，证明镜像延迟不会制造并行；核调用／结果跨页时只批量取得所选view的轻量执行摘要、不额外加载正文或结果行。补不同声明组在同一可比较范围内实际重叠、A-B／B-C重叠但A-C不重叠，以及跨不可比较范围不判并行，避免把配对scope与执行比较scope混用。补同binding的迟到running／不可得摘要及换view排除结果，核终态不倒退且不跨view补回状态。核单调用canonical数组和Provider还原、默认摘要不含正文、实时与清空流内存后的刷新一致，不能只手填helper时间证明完整入口已取得证据。
  同一条真实往返场景核reasoning＋text＋两个同名不同参调用恰为四成员／两次声明；页中只见B时仍保留ordinal=3、size=4，不由组大小推工具数／整批终态。通过实际序列化和原恢复入口核声明来源贯通及重放幂等，补非法completed复合调用在原写入边界被拒绝；复用已有场景，不为每个字段单开重复测试。
  复用执行摘要场景覆盖身份／running已明确但started_at缺失，随后取得终态及真实结束观察：实际序列化、生成协议、原快照和冷历史保留可用摘要，不整包丢弃、不退回已声明，不补时钟或推断并行；沿原工具行消费，不另建缺时间专用状态分支。
  该场景同时核公共source.message_group的完整对象／无组缺省、生成客户端解码，以及B先回执后call／result仍各自保留原消息来源；显式组加owner／ModelCall可确认同批，仅draft的model_call_id／provider_call_index不能替代该证据。合行与刷新不改G的成员或重建数组，不将仅helper通过当作公开入口验收。
  在已有跨ModelCall／轻摘要场景中核两次call_index=0不会串结果、缺来源的唯一可见候选不被猜配、detail正文与新执行摘要同时保留，以及已知失败结果但无时序时仍显示失败；不按字段重复增加测试。
  上述真实入口同时核result-only且声明不可见时仍返回自身tool_outcome；页外结果有marker但无tool_execution时调用摘要仍有真实结果，排除该结果后不泄漏。隐藏原组末成员不破坏另一可见调用的精确关联；summary禁读正文，查询范围随本页工具而非整轮调用规模扩大。核payload→manifest→公开序列化／生成客户端的marker语义，不以手填DTO替代writer验收。
  复用分页／定位场景核不同消息组重复ordinal不全局重排，以及先读result再补call后的去重和result URI定位；不为此全量加载消息组或新增逐调用请求。
  在同一重叠执行场景中核Item间隔、工具实际区间和Turn耗时各守原语义；完成镜像延迟不拉长工具耗时，缺执行证据不抹掉结果或使合法Turn计量失败。不为计时tick、格式文案或每个指标新增重复测试。
  上述先后执行场景同时覆盖B结束后A才开始，核实时及冷历史均按A、B排列、关系按真实方向解释；复用原往返场景，不为每种关系新增独立测试装具。
  复用同一真实执行装具阻塞通知消费者，待重叠／先后两种源端执行完成后再放行；核历史保留源端起止，消费者串行处理或gather汇总不制造／抹去重叠。不能只直接调用complete_tool或手填时间字段来证明实际执行入口已取得正确边界。
  复用后台工具的现有装具核A返回任务引用后B开始、A的后台任务仍运行：调用终态和后台任务状态独立，实时／冷历史不延长A的调用区间或将调用成功当任务成功，前端不据调用先后断言后台工作没有并行。
  复用真实执行往返场景覆盖success／确认cancelled／请求取消后unknown，经公开协议序列化和冷历史读取保持结果及可得实际结束；前端实时与snapshot恢复均不将取消显示成功、不连带结束同批其它调用。取消但缺真实结束观察仍保留结果，不能补造运行区间；不为每层枚举重复写同义测试。
  复用该场景补合法request／runtime双载体：仅一个带执行证据、改变载体顺序、排除带证据载体及非空事实冲突，核冷历史不丢证据、不越view、不重读正文。复用前端交错回执场景核[A,B]始终两调用行及独立result定位；不为每种排列新增重复测试。
  复用流式交错场景核执行证据早于组封存时的draft来源关联及提交收敛；不扣留已知状态、不猜组大小、不改原调度启动条件，不另建一套提前执行装具。
  同一真实消息组装具覆盖一个ModelCall的两条原消息G1[A,B]／G2[C]：调用Item、公开摘要、实时重连、冷历史及Provider重建均保留两组，只有A／B同批；B／C真实重叠不使两组或调用数组合并。复用已有跨组场景，不新增调度分支、批次字段或重复测试装具。
  复用现有前端交错回执场景，核A已展开且有焦点、B先完成、A真实结束后补时，关系说明更新不重建行／组或重置展开和阅读目标；同view同证据的冷历史得出相同结论，仅见B时不强求全组关系。不要为关系文案或每个时间字段新增重复测试。

## 7. 删除双轨与交付复审

- [x] 7.1 全仓复检Part相关直接／间接消费者与测试、旧字段／类型／默认值／fallback；项目通用Part定位全部迁齐后物理删除。保留项逐一说明真实Provider／诊断用途，不按关键词误删外部协议。
- [x] 7.2 从原代码重新推导最小充分实现，删除本次多余包装、校验、状态镜像、持久映射和抽象；确认没有新增message／presentation索引、Gateway转换服务、全历史暖读审计或定时恢复循环。
- [x] 7.3 严格校验OpenSpec；按前置先、本变更后核归档合并结果，特别检查conversation-process-presentation、重命名的历史定位条款、内部envelope与活动分页没有残留Part合同；只勾选有证据的完成项，报告未验证平台／真实数据来源，不自行同步、重启或发布。


## 实施备注与证据

- 2026-11-24 按本次请求复核同批声明／实际执行／冷历史合同，保留既有约束并补充公开来源字段与渐进展示对照、证据后到不重建行或重置焦点、工具内部并发不扩展调用粒度，以及相同view同证据下的实时／历史结论一致性。仅修改既有规划文件，未修改产品代码或勾选实施完成项。`timeout 60 bash -c 'ulimit -d 4194304; exec openspec validate normalize-semantic-items --strict'`退出0，原件为`out/tests/temp/2026/11/23/normalize-semantic-items/artifacts/spec-tool-declaration-ui-review-YkY88iy2.log`；差异空白检查通过。本次未运行产品测试。

- 2026-11-23 核对根 HEAD `a5cad7fff704f3f1a8af6aba97fc58087b80bcae`；开始时只有本变更目录未跟踪，没有其它 staged／unstaged 产品改动。前置 `unify-conversation-process-navigation` 的三层折叠、统一节点／WS、分页与性能预算保留，替换范围以本设计为准。
- `bun run release:policy list` 返回 `[]`；当前没有可核验的发布支持来源。本变更不借用既有 dev v29→v30 授权，不转换真实 Home；5.3 的来源核验完成，5.4／5.5 的转换验收不得冒充已执行。首次核对时的计划为 canonical3／SQLite31／阅读3／cursor3，当时尚未切换版本；下文 SQLite31 只记录早期思考索引切片的实施证据，最终集成版本以第1.3项及 design 的 schema34 为准，不能以版本登记替代格式切换与消费者验收。
- 直接和间接入口已枚举：写入沿 `checkpoint/message_codec.py`、`codec/groups.py`、`message_commit/`、`execution/item_commits.py`、`runtime/stream_accumulator.py`；协议还原沿 `mapping/itemized/canonical_langchain.py`、`carrier_dedup.py`、`prefix_frames.py`、`message_reasoning_merge.py` 及原 Provider 编译器；执行证据沿 `orchestration/message_stream/tool_calls.py`、`ToolResultCompletion` 和原 Saver；历史沿 `checkpoint/projection/message_projections.py`、`storage/catalog/{message_groups,turn_projection_reads,turn_activity_merge,internal_history}.py`、`migrations/history/`、`rollout_history/{reader,cursor}.py` 与 `mapping/turn_response_parts.py`；协议沿 `schemas/internal_v2/turn.py`、workspace public.proto 和现有 SDK／TS 生成；前端沿 responseParts／chatResponseGroups／transcriptNodes、turnProjection／turnTimeline／turnBodyRetention、history hooks、transcriptItems／useTranscriptNavigation 与 Workbench Opener；操作锚点沿 domain parts、storage/catalog/parts 和 checkpoint 操作 owner。Provider 原 block／part refs 与项目通用 Part 坐标分别处理，不按关键词批量替换。
- 第一条代码切片已统一普通／带工具 assistant 内容拆分和 LangChain／summary-source 重建，删除工具专用展开与重复还原；保留单回执、用户 root、invalid 声明、签名、媒体和原 str/list，修正仅凭 ModelCall 合并不同消息及纯思考消息漏索引。仍需迁齐后续版本、历史、finalization、公共协议与前端，因此第2～4节不提前勾选。
- 定向检查原件在 `out/tests/temp/2026/11/23/normalize-semantic-items/artifacts/`：`codec-group-identity.log` 83项通过；后续源变化对应 `codec-final.log` 76项通过、`codec-singleton-identity.log` 95项通过、`codec-sealed-sources.log` 8项通过；`codec-invalid-tool-binding.log` 4项通过（含真实lifecycle binding与invalid声明共存）。这些是重叠范围的分轮证据，不能相加作为独立用例数。Ruff 与差异空白检查通过；后续增量证据继续记录在本目录。
- `codec-roundtrip.log` 中38项通过，另1项在工作区启动 fixture 阶段缺 `BOXTEAM_BACKEND_GENERATION`，未进入本次链路；未修改环境或以成功掩盖该项。后续检查曾发现强制所有单成员补 group 会破坏原子流式身份，已改为仅在 payload 不能表达原空列表形态时保存单成员组，原身份断言保持，复验95项通过。测试编写阶段缺必要参数／调用错误 owner 的失败原件也保留，最终通过记录单独存放。
- 实时封存已沿原 accumulator 为实际多成员写入完整 ProjectionGroup，Provider 调用 index 与内容 block index 分域，结果保留独立消息来源；`execution_output` 直接提取已选输出成员，不再逐个重建不完整消息组。对应原件：`semantic-stream-group-roundtrip-v1.log` 5项通过；`semantic-stream-group-consumers-v2.log` 24项通过；`semantic-execution-output-group-v1.log` 2项通过。此前184项通过、1项失败的 `semantic-stream-group-consumers-v1.log` 暴露受保护carrier测试逐成员重建旧预期，修复后核完整组及保护字段，未放宽安全断言。
- 前端工具切片已接入原ModelCall／真实调用ID和invocation／attempt配对，移除call_index及本页唯一候选猜配；实时投影传递真实ModelCall。调用声明使用中性状态，实际结果与执行时序分开，跨页摘要可独立结算调用。主时间线、子Thread及无Turn execution共用 `mergeCanonicalPart`，保留已加载正文同时接入执行元数据，同binding迟到running／null／未知摘要不抹掉已知结果，换view不带入旧证据。主时间线旧unknown类型补丁实现已删除。
- 全仓检索确认后端不再产生 `reconciled_tool_message`，已删除前端选择“更权威attempt”的排序／兼容分支；同一scoped调用出现多个attempt明确报错。原有效生命周期排序、参数更新不变性仍保留，旧重复attempt验收改为拒绝场景，未将新ModelCall的合法独立调用当作重复。
- `semantic-tool-ui-v7.log` 为7个受影响前端文件的161项定向检查通过（1.67秒，退出0），包括原消息流、主／子Thread、无Turn、正文保留和组件消费；早期失败原件保留，已按证据修正取消结果的未知标记及测试初始binding，未放宽身份／顺序断言。所有测试使用 `timeout 90 bash -c 'ulimit -d 4194304; exec bun test <该日志列明的7个文件>'`。源码指纹单独保留在 `semantic-tool-ui-v7.sha256`；这些局部证据不代表Part、版本切换、final多块及真实产品验收已完成，第3～6节尚未完成的整项继续保持未勾选。
- 静态核验已取得：Web全量源码及测试 `bun run --cwd src/clients/web tsc --project tsconfig.json --noEmit`、真实Workbench的216份sourceTransform源码类型检查、`bun scripts/generate_protocol.mjs --check --staging-root <本任务artifacts>/protocol-check-v1`、手写Python改动Ruff及差异空白检查。协议检查退出0；生成Python不手工修改。早期静态命令误纳入生成文件／SQL的输出不作为产品失败或通过依据，随后按确切文件类型重查；旧根入口编译器缺Bun类型与直接用Bun运行JS编译器触发内存保护的记录保留。完整构建与最终类型检查的增量退出结果在后续记录，不借早期源码的通过代替最终版本。
- 上述工具切片最终检查已收齐：`semantic-tool-ui-typecheck-v7.log` 的Web真实类型检查退出0；`semantic-tool-ui-build-v4.log` 的完整Web／Workbench构建退出0，覆盖最后删除旧attempt挑选分支的源码。原日志及对应指纹均保留；这些是此前实施切片的证据，不表示本次规划补充已完成产品验收，整体任务仍未完成。
- 本轮按用户要求补齐工具声明／执行关系规格，明确结果marker独立于时序的writer→安全manifest→公开投影路径，以及本页工具驱动的跨页关联、result-only和隐藏组末的view边界；只修改既有规划文档，未将相应实施任务勾为完成。`spec-tool-relations-review-v2.log` 记录 `openspec validate normalize-semantic-items --strict` 退出0；本轮未新增或重复运行产品测试。
- 本轮继续完善同一合同：design第2.1节给出声明／执行／刷新三类关系的最小对照，明确拆分不改变调度；历史与任务补齐合法request／runtime载体的安全证据合并及view隔离，展示场景补齐非相邻回执合行与独立定位。仅修改现有规划文件，未修改产品代码或新增完成勾选。`spec-tool-carrier-relations-review-v1.log` 保留带外部timeout／内存保护的严格校验原件，退出0；产品行为及整个变更的验收仍以未完成任务为准。
- 思考安全投影切片已将原 `reasoning_blocks` 收束到 canonical Item 主键，登记SQLite31；原消息组提交与无消息locator的独立思考共用codec及投影写入方，删除final专用思考补读。普通／无Turn历史、活动坐标和直接Item详情使用相同分类，不再把已知extension整体误当密文。正文、摘要保持原始空白及独立截断标记，只请求摘要时不交付正文；同源两种安全视图保持同一Item身份，匿名但不同来源不合并。fork前缀裁剪按Item归属删除该安全索引，不再按消息locator误删独立思考。没有新增正文表、来源别名或自动30→31迁移，也未转换真实Home。
- 该切片原件仍在同一artifacts目录：`semantic-reasoning-safe-v8.log` 39项通过（3.61秒），覆盖映射、真实写入及冷读、混合安全视图、两种final往返；`semantic-reasoning-activity-v3.log` 32项通过（12.37秒），覆盖普通／保护型无Turn思考、分页、view及正文回补；`semantic-reasoning-storage-v7.log` 10项通过（1.80秒），覆盖初始化连接与SQLite备份恢复。新库结构合同用例已在`semantic-reasoning-schema-v4.log`中通过；同轮另一项装具时间倒退失败已改为使用实际追加时刻，后续冷读及v8检查通过，未放宽产品计时断言。以上范围重叠，不能相加为独立用例数。
- `semantic-reasoning-static-v8.log`记录手写Python Ruff和差异空白检查退出0，`semantic-reasoning-spec-v8.log`严格校验退出0，最终相关源码指纹保留在`semantic-reasoning-final-v8.sha256`。本轮未改前端；此前前端构建证据不代替尚未实施的Item公共合同、媒体、阅读格式和真实浏览器验收。第2～6节的完整任务继续保持未完成。
- Fork额外补验尚未通过：直接pytest的`semantic-reasoning-fork-v8.log`在装具阶段要求正式matrix期限保护，未进入业务；改用 `PYTEST_ADDOPTS='-k test_fork_keeps_generic_delta_parent_writes_and_excludes_seed_pending' timeout 150 bun run test:matrix -- --suite=integration-execution-history-delta` 后，`semantic-reasoning-fork-matrix-v9.log`记录4项prefix通过、4项full-copy失败、144项未选。失败统一在冷恢复消息列表缺少已接受的来源排队消息，源／目标调度权断言已通过；该场景没有reasoning Item，不能据此声称是新安全索引造成，也不能将其记作通过。保留测试预期及全部原件，后续按full-copy历史、checkpoint/view与队列边界归因，不重复无变化运行。
- 最终相关源码的真实HTTP复验已完成：`semantic-reasoning-http-final-v9.log` 为 `tests/integration/backend/sessions/test_session_history_loading.py::test_history_loads_rollout_summary_and_tool_details` 1项通过、32.34秒、退出0，经后端启动与公开历史接口核思考正文、独立摘要、工具详情、final和保护边界。中间HTTP诊断失败原件保留；此通过不覆盖其它尚未迁齐的历史／公开Item合同或上述full-copy失败。
- 上述full-copy失败已按既有上下文契约归因并复验：未领取的排队消息不进入checkpoint模型执行上下文；full fork仍保留canonical历史、Turn／producer与正文，不复制源调度权。`semantic-fork-context-boundary-v10.log`记录正式matrix复验8项通过、144项未选，pytest耗时23.14秒，runner退出0；删除源后冷读目标也已覆盖。旧失败原件保留，不再将该已解决场景列为未通过。
- 活动统计已沿原查询owner在分页前取得整轮／当前view的权威count与时间范围；点读和分页不将统计缩成页内数量，纯encrypted presence不计入活动，也不成为下一项的计时前驱。`semantic-activity-scope-stats-v13.log`记录33项通过、11.47秒、退出0；该证据不代表公共Item／Part迁齐完成。
- 工具源端观察切片已沿原callback binding冻结开始、沿原active attempt冻结完成，保留独立异步durable-start门控；不在消费者或complete_tool入口重新取时，不新增计时表。`ToolStarted.started_at`贯通正式生成协议、实时投影及恢复；无可信来源保持不可得，`completed_at／completed_seq`仍描述镜像发布。
- `tool-boundary-source-v3.log`记录真实工具执行及延迟通知消费等41项通过、10.97秒；最后生命周期投影调整后，`tool-boundary-final-projection-v1.log`补验12项通过、6.34秒。真实装具分别执行重叠／先后调用，经过生产processor、SQLite／JSONL、冷历史与公开snapshot协议，验证源端区间不受消费延迟影响。`tool-boundary-runner-v1.log`验证重复调用认领拒绝（1项通过），`tool-boundary-start-gate-v1.log`验证原durable-start门控（3项通过）；`tool-boundary-contracts-v1.log`记录105项相关投影及协议合同通过，范围重叠不相加。Python检查统一使用外部timeout与`ulimit -d 4194304`。
- 该切片前端reducer的`tool-boundary-web-reducer-v2.log`记录81项通过，`tool-boundary-web-types-v2.log`真实Web类型检查、`tool-boundary-web-build-v1.log`完整Web／Workbench构建均退出0；`tool-boundary-static-v3.log`记录手写Python Ruff通过。协议生成一致性、Buf lint／build／format及基于根HEAD的breaking检查分别保留在`protocol-tool-boundary-check-v1.log`、`tool-boundary-protocol-format-v1.log`、`tool-boundary-protocol-breaking-v1.log`，均退出0；该HEAD不是release迁移来源。这些均为局部实施证据，第3～6节整项和真实产品验收继续保持未完成。
- 最终复核补齐`tool-boundary-workbench-types-v1.log`：216份实际sourceTransform源码类型检查退出0。`spec-tool-relations-final-v1.log`保留本次工具声明／执行／历史合同的严格校验原件；对应规格及当前受影响源码指纹分别保留在`spec-tool-relations-final-v1.sha256`、`tool-boundary-final-source-v1.sha256`。原件统一位于上述artifacts目录，未新建总结文档；本次文档核对未重复运行已通过的产品测试。
- 阅读节点切片已移除Item source.part及所有节点创建／恢复消费者对part_ordinal的依赖，正文／摘要／保护视图与工具输入／输出以slot区分；统一responseNode替换原partNode，无别名。折叠祖先按完整节点键匹配，并从原组件派生实际组标题与内容成员；未新增展示状态owner。阅读格式由原浏览器owner登记为3，旧格式明确报错并保留IndexedDB原记录及其它视图，没有迁移真实Home或清整个库。4.3完成；4.4尚需后续公共Item协议与历史缓存合同迁齐，保持未勾。
- `reading-item-slots-v1.log`记录外部timeout／内存保护下的64项通过（5个文件，1.219秒），覆盖同Item不同slot、真实组件DOM成员、流式结算保位、无Turn、补读确认部位消失、旧格式原件保留与客户端展示态隔离。Web类型检查初轮只有新增测试缺少readBrowserView泛型，补齐后`reading-item-slots-types-v2.log`退出0；`reading-item-slots-build-v1.log`完整Web／Workbench构建、`reading-item-slots-workbench-types-v1.log`216份实际变换源码类型检查均退出0。仅验证此切片，真实Gateway产品及完整性能门仍未执行。
- 公共协议切片已将内容数组统一为response_items／display_items、内容身份统一为id=source.item_id；删除旧DTO、部件坐标、转发别名并物理迁移原mapper／前端模块。Python、Node和Web协议由正式生成器产出，旧字段号及名称reserved；Trace／Provider自身part来源保持。历史镜像沿原Thread缓存owner由2升3，旧条目局部失效，不清草稿／阅读记录／其它Thread。3.1完成；媒体与final成员展示仍归尚未完成的2.4／3.2／4.2。
- 该切片的`item-public-final-backend-v4.log`记录148项通过、2项未选（42.07秒），覆盖公开描述、映射、真实消息组及活动投影；此前`item-public-contracts-v2.log`中的两项导出故障注入已通过，不将该轮其它已修复失败算作全轮成功。`item-public-web-v3.log`记录9文件152项通过、0失败。`item-public-types-v3.log`、`item-public-build-v1.log`、`item-public-workbench-types-v1.log`分别记录真实Web类型检查、完整构建与216份Workbench变换源码类型检查退出0。`item-public-http-v1.log`记录真实公开历史／上下文续读2项通过（57.63秒）。这些均为本切片证据，不代表整个变更或真实Gateway产品已验收。
- 正式协议生成、一致性、OpenAPI导出和格式检查分别保留于`item-public-protocol-v2.log`、`item-public-protocol-check-v3.log`、`item-public-openapi-v2.log`及`item-public-protocol-format-v1.log`，退出0。基于根HEAD的`item-public-protocol-breaking-v1.log`退出100，恰有本变更明确授权的9处消息／字段删除；不以恢复旧别名掩盖breaking，根HEAD也不是release支持来源。旧生成器误入的154个upstream文件已逐个核对变换后精确还原，嵌套仓库保持干净。
- Item操作及轮廓删除切片已移除生产response_outline_json读写、通用Part锚点／索引和复合轮廓展开；活动请求仍沿原owner按真实Item分页。`item-operation-final-v7.log`记录真实写入、旧操作列删除及重启定位3项通过，`item-operation-pagination-matrix-v7.log`记录有界批量读取等8项通过；本轮检索确认response_outline_json仅剩显式v29→v30迁移源码，原受支持来源保留，不成为新库生产读写路径。3.6完成，最终迁移与发布验收未完成。
- 本轮依新规格复审实际发现：同一执行的终态摘要缺started_at会抹掉已知开始边界。已在原mergeCanonicalItem保留同binding的可得边界，真实running状态不依赖时间字段；没有新增状态树／fallback。`item-execution-merge-v2.log`记录20项通过（251毫秒），`item-execution-merge-types-v2.log`真实类型检查及`item-execution-merge-build-v1.log`完整Web／Workbench构建均退出0。初轮测试装具遗漏生成协议必填的可空字段、误用null的类型失败原件保留，修正装具后复验，未放宽产品断言。
- history cursor原owner已统一发行和读取v3，继续只使用逻辑Item续点，旧版本／非整数版本及Part附加坐标明确拒绝，不增加旧版reader或转换真实Home。`item-cursor-v3-backend-v1.log`记录51项通过（2.11秒）；`item-cursor-v3-lanes-v1.log`记录冷execution、有界续页与两lane先后耗尽4项通过（5.58秒）。`item-cursor-v3-http-v1.log`中的线性HTTP窗口已通过，around用例的旧复合计数断言失败：fixture现有独立文本、两份不同来源思考、一次调用和一次回执，正确计数为5，旧预期为3。已修订为真实语义计数并补默认摘要与详情Item逐项对照，后续结果另记；3.4仍需final大组预算及完整模式验收，不提前勾选。
- `item-public-final-ruff-v2.log`记录当前111个手写Python变更检查通过，`item-public-mjs-syntax-v1.log`记录10个变更MJS解析通过；纯临时日志及源指纹继续保留在同一artifacts目录，未新增总结文档。本轮仍未同步dev／release、重启开发服务或迁移真实用户数据，整体目标保持未完成。
- around的实际语义计数与双向续读复验已完成：`item-cursor-v3-http-v2.log`记录1项通过、42.64秒、退出0；同时核默认无正文摘要、真实五个活动Item及前后分页边界。对应测试Ruff在`item-cursor-v3-http-ruff-v2.log`退出0。`item-cursor-v3-openapi-v1.log`公开schema校验退出0，但保留既有Gateway重复Operation ID警告，不将其归因于Item协议或宣称全仓无警告；`item-cursor-v3-spec-v1.log`严格规格校验退出0。
- 本轮最小实现复审补齐终态先到、开始观察迟到的反向顺序：原merge入口先按既有规则选择状态，再合并同一attempt缺失的真实时间边界，不因补时间退回running，也不引入第二事实源。`item-execution-merge-v3.log`仍为20项通过（286毫秒，104个断言），`item-execution-merge-types-v3.log`真实Web类型检查与`item-execution-merge-build-v2.log`完整Web／Workbench构建均退出0；前一版日志保留，不代替最终源码的增量复验。当前236个变更文件的最终指纹见`item-cursor-v3-source-v2.sha256`，命令与退出结果见同目录`item-cursor-v3-commands-v1.txt`。
- 非文本输出切片贯通原writer→typed manifest→默认摘要／定点详情→实时与历史展示：媒体及未知块保留独立Item，默认不读正文，显式读取沿原预算且不切坏JSON或引用；final角色和Item／body节点保持。复审同时修复ChatTurn memo遗漏tool_execution／tool_outcome，避免同revision的新执行证据被正文复用挡住。未新增媒体服务、持久映射或连接，未改真实Home；整体仍10/37，final聚合事实删除、真实Workbench验收及性能门保持未完成。
- `item-content-backend-v1.log`记录89项通过（23.64秒），包含真实writer、冷历史、混合／纯媒体final和同view单Item补读；`item-content-web-v2.log`记录52项通过（4文件、794毫秒）。`item-content-types-v3.log`、`item-content-build-v1.log`、`item-content-workbench-types-v1.log`、`item-content-ruff-v1.log`、`item-content-protocol-check-v1.log`、`item-content-openapi-v1.log`及`item-content-spec-v1.log`均退出0。前两轮类型检查只暴露新增测试把内部block直接当wire snapshot的类型错误，已改正fixture且保留失败原件；OpenAPI既有Gateway重复Operation ID与构建既有翻译缺口仍如实保留。准确命令、退出码及源指纹在同一artifacts目录的`item-content-commands-v1.json`、`item-content-source-v1.json`，未同步或重启dev。

- 原输出终态切片完成2.4：删除追加final替代消息、最后文本来源推断及其唯一调用方；非摘要model.end保留原message_id，按原checkpoint／canonical manifest读取同一组，final指向原组末输出。原AIMessage首次写入保留完整请求用量，组级usage只属于anchor；终态不改原正文、provenance或checkpoint。普通Turn和无Turn完成均支持已提交原输出的metadata-only终态，保留view可见性、归属及原幂等入口；无Turn不新增final字段。未新增缓存、持久成员列表或旧函数别名。
- 真实完成／中断竞争检查发现并修复原外层停止编排在自然完成后仍取消Job的问题：底层已裁定completed时直接交付结果，不再等待或取消正发布完成镜像的原Job。`final-original-product-v7.log`为真实Runner主Thread连续执行／子Thread隔离及内部完成与中断两种裁决的3项通过（20.35秒、退出0）；只替换外部模型，未把模拟输出当作真实上游验收。
- `final-original-final-v6.log`为7个受影响后端文件262项通过（44.46秒、退出0），包括真实writer、原组多块／纯媒体final及冷读、签名／密文、请求用量、默认摘要和定点补读、可见性拒绝、元数据终态幂等，以及模型切换后晚到摘要不覆盖原身份。原messages checkpoint和JSONL正文未被finalization复制的断言保留。准确命令与退出码见`final-original-final-v6.json`；所有pytest均由进程外timeout与4GiB数据段限制保护。
- `final-original-ruff-v7.log`记录117个手写Python变更检查通过，`final-original-spec-v6.log`严格规格校验通过；差异空白检查及upstream嵌套仓库干净检查通过，源码指纹见`final-original-source-v7.json`。最小实现复审移除了已无消费者的final构造／反查函数、测试中的无用正文映射及runner的旧块副本；已提交组沿原批量reader读取，不另建查询owner。该轮仅后端与既有文档变化，不重复前端构建，不代表6.3～6.6真实Workbench及性能验收完成。整体11/37，未同步、重启或迁移dev。
- 早期`final-original-*`失败原件均保留：新增测试对来源元数据、冷读owner scope及内部turn_status入参的装具错误已修正；内部竞争测试先因pytest插件未显式加载而未进入业务，后续显式加载后暴露上述两个真实收敛问题并修复。`final-original-interrupt-v4.log`的2个新装具入参失败已在`final-original-internal-v5.log`及最终v6中通过，不把含失败的整轮记为成功；最终产品入口使用独立v7全绿记录。保留既有第三方langchain-community弃用警告。

- 声明／writer验收补齐：`declaration-contract-v1.log`记录704项通过（17.14秒，退出0），涵盖domain单调用基数、消息流与真实工具证据；新增合法hash下0／2调用拒绝、1调用保留参数数组，以及A参数未完整时B沿原prepare/start入口可独立启动。既有真实writer交错片段测试核B先到仍恢复A/B顺序、ordinal覆盖全部内容成员、冷历史／snapshot公开序列化保留来源。没有增加等待屏障、执行入口或额外状态。
- `writer-roundtrip-matrix-v1.log`记录19项通过（15.98秒，退出0）：同一混合消息带／不带工具时，签名思考、文本、图片、音频、视频和未知JSON块经真实Saver／JSONL／冷读无损还原；扩展原交错声明用例覆盖Provider失败／中断／正常完成，已完整B不因A不完整而丢失声明。原附件真实准入／Provider请求／冷读用例同时通过。空消息和原输出终态复用此前`final-original-final-v6.log`中对应Saver往返证据；不为同字段再建平行测试。2.5／2.6完成，整体13/37，版本切换、长final统一续页和真实产品验收仍未完成。
- 两次检查的准确命令、退出码及源码指纹分别保存在同目录`declaration-contract-v1.json`、`writer-roundtrip-matrix-v1.json`；`declaration-roundtrip-ruff-v1.log`记录本批4个手写Python测试文件静态检查通过。本批只补合同验收、复核原实现，无新增产品状态／抽象／兼容分支，不宣称整个OpenSpec已完成。
- 长final续读切片移除独立512成员查询，final与过程成员共用原Item选集及续点；final身份与过程统计分别消费，不新增成员缓存或查询入口。真实Saver／冷历史场景写入600个输出块，三页完整取回600个final及6个过程Item，每页最多256项；定点补读直接取得目标Item，摘要不物化整条原消息。`final-item-paging-backend-v1.log`记录125项通过（35.38秒），`final-item-paging-web-v1.log`记录4文件67项通过（950毫秒）；完整3.3／3.4及真实Workbench验收仍未勾选。
- 本切片完整Web／Workbench构建已退出0，原件为`final-item-paging-build-v1.log`及同名`.exit`；`final-item-paging-ruff-v1.log`和`final-item-paging-spec-v1.log`分别记录相关Python静态检查及严格规格校验通过，差异空白检查通过。构建仍报告既有3751条上游翻译缺口；本轮未同步、重启dev或转换用户数据，整体保持13/37，未将局部通过当作全部完成。

- 用户已明确授权退出未发布 v1/v2 canonical 离线迁移支持。本切片删除原转换包、专用 staging/report、历史终态物化入口和测试／矩阵入口；冷执行观察移除两条旧导入排除子查询，正常 Turn 与无 Turn execution 仍走原事务 owner。版本字段缺失不再默认认作格式1；旧版／未知版查询和初始化均明确拒绝，保持既有 SQLite／JSONL 不变。基础存储、contribution、history 的遗留迁移例外已在本变更 delta 中明确替换；其它有序 SQLite 结构迁移与冻结来源 fixture 保留。没有转换、清理真实 Home，也未同步／重启 dev。
- `retire-legacy-runtime-v1.log` 记录77项通过（39.36秒，外部 timeout／内存保护，退出0），覆盖普通 Turn／无 Turn execution 终态事务、冷观察、历史分页、准入以及旧版／未知／缺版本拒绝且保持原数据。准确命令、根 HEAD 与本轮源码指纹在同名 `.json`，退出码在 `.exit`；原件仍位于上述任务 artifacts 目录。此切片不代表整个 normalize-semantic-items 已验收，未完成的产品／性能门继续保持未勾。
- `retire-legacy-ruff-v1.log`、`retire-legacy-spec-v1.log`、`retire-legacy-whitespace-v1.log`分别记录当前全部手写 Python 改动 Ruff、补齐存储／contribution delta 后的严格 OpenSpec 校验和差异空白检查，均退出0；准确命令和退出码在`retire-legacy-static-commands-v1.json`。本轮未修改前端源码，复用此前同批完整类型检查与构建，不重复长跑。复杂度复审确认旧 importer 与正常终态入口的中转层已移除，没有新增兼容 reader、恢复账本或查询 fallback。

- 规格实际合并复审修复session-turn-history delta从其它capability复制导致的悬空修改／删除，补入真实基线的11个修改条款及独立工具历史合同；前置与当前delta保留原验收场景，退出旧格式的场景改为明确拒绝。安装版OpenSpec的真实合并函数按前置→当前在临时副本合并11份规范，无警告；`spec-actual-merge-v3.log/.exit`及`spec-merge-v3/receipt.json`为原件，两份change严格校验和差异空白检查通过。未归档或改写主规格。
- 真实Workbench→Gateway→Backend的128 Turn／六会话／248px验收发现并修复summary writer正文在payload.content但映射读取其它键的问题；原映射直接读取真实content，保留文本与reasoning_items两种实际载体，没有兼容fallback。真实accumulator→writer→冷历史新增两项往返检查；`summary-item-cold-v2.log`两文件78项通过（13.34秒）。
- `semantic-product-history-v2.log/.json/.exit`记录正式integration-web-progressive-history整项通过（含正式构建／准备266.54秒）。首次读取550.52ms、20次暖历史p95=161.53ms／max=179.88ms，缓存折叠max=41.2ms、网络展开p95=116.3ms／max=122.8ms，prepend保位max=147.61ms、append保位max=44.94ms；248px无溢出、箭头靠右且耗时可见，无页面错误。覆盖六个真实fork会话、两BrowserContext阅读隔离、内部execution、展开／取消／再定位及有界大结果；原截图和完整JSON位于正式run-09810a8f187e产物。尚未用该场景证明真正搜索结果→Item URI的完整点击链路，6.3不提前勾选；Composer已有可见可输入断言，完整每次延迟预算尚待核，6.5不提前勾选。
- `semantic-tool-history-v1.log`记录真实工具执行证据与工具结果搜索14项通过（38.89秒）；`semantic-current-web-state-v1.log`为阅读owner、真实组件、responseItems／消息流／时间线等7文件89项通过，保留React act警告。当前真实Web类型检查`semantic-current-web-types-v2.log`退出0，正式产品场景已完成最终Web／Workbench构建；`semantic-current-ruff-v1.log/.json`为161个手写Python文件静态检查及SHA记录。旧根命令缺Bun类型的v1失败不是产品类型回归，正确工作目录命令v2通过，不改配置掩盖。
- 当前格式新库／隔离Home及阅读格式拒绝已沿真实产品冷启动、旧原件保留和独立客户端检查取得证据；5.4无实际支持转换来源，依用户明确退出未发布v1/v2迁移支持记为不适用，不伪造执行了真实数据转换。上述证据结合此前600成员final统一分页、cursor3所有lane和活动统计切片完成3.3～3.5、4.4、5.2／5.4／5.5、6.1／6.4及7.3，未勾选其它尚缺完整证据任务。
- `semantic-fork-cursor-v1.log`的26项internal cursor检查已通过，另8项在setup失败，未进入fork业务；其中旧source_case未设置Backend代次，另一个fixture要求正式matrix watchdog。source_case现显式提供独立测试代次；压缩前缀场景只使用文本模型，不再无关启用PTY／Browser实例。`semantic-fork-compacted-v2.log`进一步确认旧插件测试Gateway缺workspace_plugin_bindings，未通过且不作为fork产品结论；后续改用必要依赖与正式matrix复验，不改产品检查或伪造watchdog身份。

- 压缩／fork补验已贯通：`semantic-fork-lineage-v2.log/.exit`为正式matrix的7项通过，覆盖共享assembly失败重试、各Provider请求独立结局、导入internal lineage后的三种后继fork。`semantic-fork-compacted-v8.log/.exit`为真实压缩前缀场景1项通过；原摘要／final两个锚点分别验证inclusive/before选择、上下文／展示不同顺序及分支下一次模型请求。该测试实际需要Terminal和Browser参与来源快照，已恢复原插件范围，未把禁用插件的中间失败当成功。
- source_case测试装具按当前单一启动链路补齐Backend代次、本机控制凭据、真实内部HTTP监听和原WorkspacePluginBindings owner；后端启动时可回读自己的已挂载配置。未放宽产品鉴权／启动校验、未伪造快照／watchdog，也未修改业务实现。中间v3～v7分别保留插件缺失和控制面凭据诊断，最终v8通过；删除临时诊断探针／重复assert，没有增加另一套配置交接实现。
- 新增真实UI搜索→Item验收暴露P2：导航从搜索引用派生Item锚点后，同时传了anchor_message和anchor_item_id，公开history按原互斥合同返回422。修复原导航选择器：明确message／tool定位时不再自动派生独立Item请求；阅读DOM仍以同一真实Item定位。未放宽后端合同或在API层过滤参数。`semantic-item-navigation-v1.log`为3个相关前端文件68项通过；完整真实点击与最终构建继续复验，不把该局部检查视为最终验收。
- `semantic-item-opener-v1.log`记录新增MJS局部变量重复声明，已改正并通过node --check；`semantic-item-product-v2.log`保留上述真实422失败及截图，位于正式run-2eae5a597223。`semantic-rewind-search-v1.log`因集成闸门等待耗尽90秒外层期限，只有1项已通过，退出124；不记作产品回归或整轮通过，后续只在名额空闲时复验。

- 真实产品补验 `run-acc06c39b378` 的搜索／Opener 与长历史两个节点均 setup/call/teardown 通过；前者核搜索命中→真实工具 Item、Gateway 规范 Item URI、重复定位和手动折叠后重新打开，后者核128 Turn、六个真实 fork 会话、两个 BrowserContext、子 Thread／无 Turn execution 及248px窄布局。整个 pytest session 退出1，因为另三项 rewind 搜索在 setup 被已退出进程遗留的测试 workspace-owner 阻断；不把该 session 称为全绿。正式构建收据在 `out/tests/harness/python/web_workbench/runs/run-acc06c39b378/artifacts/workbench-build.json`，构建退出0。
- 同 run 长历史的未筛选样本：首读887.28ms；20次暖历史p50=139.45ms／p95=158.00ms／max=160.00ms；20次缓存开关p95=40.90ms／max=41.40ms；20次网络展开p95=124.20ms／max=183.40ms；prepend保位max=96.20ms、append保位max=43.74ms。七次Composer可操作延迟为697.13／300.99／366.60／393.40／404.83／396.22／329.17ms，均低于2000ms。完整样本、字节、request_id、规模、源码指纹和截图在该正式节点的 `rollout-history-progressive-result.json`、`scenario-source.json` 及产物目录；页面错误为空。结合此前媒体、分页、阅读与实时合同证据，完成3.2、4.1／4.2／4.5／4.6、6.3／6.5，不据此宣称原生Electron／移动端验收通过。
- rewind 搜索残留已只读核实原PID1156633不存在，将原测试workspace及owner marker整体归档到原失败run的 `state-explicit-after-timeout/`，原件保留，凭据为 `semantic-search-stale-owner-archive-v1.json`。定向复验 `semantic-rewind-search-v2.log/.exit` 为3项通过、3.52秒、退出0。正式压缩／fork前缀最终复验 `semantic-fork-compacted-v9.log/.exit` 为1项通过、91.76秒；该版移除了测试装具重复手动启动插件，只由正式配置交接启动，没有修改产品启动边界。
- fork冷恢复补验发现真实P2：Provider canonicalizer清除流式part/index时丢失来源，导致fork新head重复还原流式final与checkpoint final。现于原canonicalizer清理前保留真实 `content_part_refs`，沿已有typed去重入口消费；重复规范化保持幂等，原消息不变，不按文本、邻接或相同ModelCall猜配，不追加final聚合事实。`semantic-stream-provenance-v1.log/.exit`为109项Provider检查通过（7.42秒），`semantic-stream-carrier-regression-v1.log/.exit`为72项checkpoint来源／carrier检查通过（5.59秒）。
- `semantic-fork-tool-evidence-v5.log/.exit` 为正式matrix两种崩溃点恢复通过（2 passed、3 deselected，51.01秒；run-3b4991e814ce）。原有完整消息相等、原始canonical Item相等、源删除后冷恢复和真实续写断言均保留；新增核A/B同组、C独立ModelCall、原声明顺序和真实执行范围／起止／outcome。fork按有效上下文排除源view未选中的stream shadow，允许展示carrier和相邻项elapsed变化；新增工具比较按真实调用与执行身份核事实，而非要求两个不同view的整个展示DTO相等。v1的singleton组错误和v3/v4的过强展示DTO比较失败原件保留，v2的真实重复消息失败已修复；不放宽原消息／canonical断言。结合原fork lineage／rewind／compaction证据完成3.7／5.1，6.6尚待剩余细项复审。

- 最终来源坐标补验修复相邻思考块收敛或混合媒体时的位置错配：仅在原内容与refs保持一一对应且类型一致时恢复显示坐标；原始refs仍作为typed来源保留，不能把思考坐标借给正文。`semantic-stream-source-final-v2.log/.exit`为最终源码156项通过（7.61秒），覆盖Provider、格式校验、API模式、内容映射、checkpoint来源与carrier去重。
- 实时工具reducer删除伪造的unknown执行／调用ID，缺必要身份明确报错且原状态不变。`semantic-tool-identity-web-v2.log/.exit`为107项通过（2.72秒）；v1因从web目录运行而未加载根i18n preload，不记作产品失败。`semantic-tool-identity-types-v1.log/.exit`的真实Web类型检查与`semantic-tool-identity-build-v1.log/.exit`的完整Web／Workbench构建均退出0；3751条既有上游翻译缺口仍由构建报告。
- 真实执行最终补验`semantic-execution-chain-v2.log/.exit`为13项通过（8.73秒）。沿原Runnable／工具回调、processor、持久化和冷历史复用四种调度装具，覆盖A/B重叠、A先B后、B先A后，以及A-B／B-C重叠但A-C不重叠。C来自另一真实登记ModelCall，A/B保留同组及ordinal，C不被合入该组；全部执行证据保留同一真实可比较scope。v1的测试装具把两个ModelCall都登记为attempt=1，被既有唯一合同拒绝；helper现允许显式传第二次真实登记序号，没有放宽产品唯一约束。
- 同文件的其余9项覆盖交错声明、未完成参数、完整／中断／失败、success／cancelled／unknown、阻塞结果持久提交、默认摘要禁止正文补读、原始与公开快照序列化、冷历史及所选view排除结果。当前前端没有并行徽标、先后关系说明、全历史区间配对或后台任务状态推断入口；跨不可比较范围和后台工作不据调用区间推断、两两关系不作传递及不新增重试调度，是本期按规格保留的实现边界，不宣称新增或验收了关系展示系统。已有同ModelCall多消息组、跨ModelCall重复调用ID、终态迟到摘要与换view用例分别验证各自真实owner，无需为这些组合建立第二套执行装具。
- 附带检查`semantic-execution-source-v2.log/.exit`为12 passed／1 failed（55.86秒，退出1）：工具执行12项通过；H4在其原持续任务重复触发整体对象比较失败，HTTP结果多request_id／task_revision等字段。该场景此前的真实来源、final原Item、生成分支与私密正文边界已通过；不放宽持续任务断言、不将整轮记作通过，也不扩大修改持续任务DTO。
- 最小充分实现复审确认：沿原codec拆分及消息组重建、原Item投影／cursor、原reducer与节点owner完成替换；复用现有manifest、索引和批量摘要查询，不另建并行关系表、消息／展示索引、Gateway转换服务或后台恢复循环。全仓旧通用Part接口、Part坐标和已退出支持importer直接／间接调用检索无悬空消费者。保留Provider流式part及content_part_refs、Trace诊断DTO、stream类别part_kind、明确旧SQLite转换及其fixture／拒绝测试；它们不是canonical Part定位别名。
- `semantic-delivery-ruff-v2.log/.exit/.json`记录172个变更手写Python文件Ruff通过。差异空白检查及旧接口检索已通过；本轮没有提交、同步、重启或转换真实用户数据。

- 末轮全仓复检修复资源身份浏览器验收的漏迁消费者：删除对已不存在part_id的取值，按真实ModelCall＋Provider原call ID核关联，按canonical Item ID及完整source核每个展示来源。8个调用／结果Item保持各自身份，4个调用行按声明顺序合行，参数拒绝为3条失败行，成功为1条；不再期待旧的7行展示。新检查核每行原参数、错误结果、8个唯一真实Item、实时／刷新完全一致、原模型请求及工具结果顺序、owner／request_id和pageErrors为空。
- `semantic-resource-identity-web-v4.log/.exit`为上述真实Workbench→Gateway→Backend页面场景1项通过（107.70秒，退出0，包含正式Workbench构建）。v1截图表明旧脚本在live自动展开阶段点击导致关闭，现等待真实终态再按aria-expanded操作；v2／v3表明合法request／runtime载体可分别保留raw／scoped原ID，现核完整调用身份及精确canonical Item来源，不强制改写任一载体。未更改产品行为、扩大等待期限或放宽调用scope／Item身份断言；所有原始外层日志及退出码保留。
- 结合已有真实writer往返、final多块／600成员分页、无Turn、fork／rewind／压缩／请求编译、13项实际工具执行、107项前端合同、156项最终来源检查及真实页面／性能验收，完成4.7、6.2、6.6与最终清理复审7.1／7.2。没有声称通过上述附带H4持续任务比较，也没有原生Electron／移动端或真实Home转换的验收证据；当前范围内未发现仍未修复的P2。全部37项的证据按本文件所列原件分别核对，不把不同轮次重叠测试相加为独立用例总数。
- 最终页面原件为正式`run-4b78224935c2`的`browser-result.json`与构建收据，phase=complete、live=restored、pageErrors=[]；Workbench构建退出0、57.07秒。`semantic-delivery-spec-v3.log/.exit`严格规格校验退出0，最终差异空白及MJS语法检查通过。最终共享工作树来源与文件SHA保存在`semantic-delivery-final-source-v1.json`；这里记录未提交的准确来源，不将HEAD冒充本次完整源码或release基线。
- H4附带失败已修复：原测试把`/runs`列表摘要与`/checks`完整运行对象整体比较；现沿既有`/runs/{run_id}`详情核完整对象幂等，并严格核重复触发后的列表仍等于唯一原摘要、没有下一页。未过滤完整DTO字段，原执行数量、精确子Thread回报、final原Item及私密正文边界断言全部保留，没有修改产品DTO或持续任务实现。
- `semantic-h4-run-contract-v1.log/.exit/.command/.sha256`记录本轮受保护的H4真实执行与错误响应合同检查：37 passed、51 failed，53.28秒，退出1；H4通过，51项均来自已有错误响应测试对旧无reason文本的精确预期，与当前统一错误出口附带安全reason的行为不一致。该错误出口及其测试本轮均未修改，不将整轮表述为通过，也未为此扩展到另一条业务链路。`semantic-h4-ruff-v1.log/.exit`记录本轮测试文件Ruff通过；来源指纹及差异空白检查通过。以上原件继续位于本任务冻结的artifacts目录，保留旧失败证据，没有重复运行已通过的Web验收或重启服务。
- 按用户分工，错误传递及上述51项失败留给另一个Thread；本轮只处理其它附带问题。修复此前OpenAPI导出反复报告的通配代理重复operationId：`/api/v1/{path:path}`保留原七种HTTP方法和转发实现，仅从Gateway接口文档排除；具体Gateway和上下文接口继续导出，工作区业务结构仍归Backend。未新增各方法wrapper、operationId生成器或第二份接口定义。既有路由测试新增真实重新生成检查，将重复ID警告当失败，并核通配代理仍可路由、具体接口仍在文档。
- `semantic-proxy-openapi-v1.log/.exit/.command/.sha256`为路由登记与流代理36项通过（5.94秒）；`semantic-adjacent-openapi-v1.log/.exit`为正式OpenAPI导出校验通过且不再出现重复ID警告。`semantic-adjacent-ruff-v1.log/.exit`仍报告原代理文件的两处TRY004及一处B008；`semantic-proxy-ruff-baseline-v1.log/.exit`证实根HEAD已有相同三项，本轮未引入、不通过改异常类别或抑制规则消除。新增测试文件Ruff检查`semantic-proxy-test-ruff-v1.log/.exit`通过。差异空白及受影响源码指纹核对通过，没有修改另一Thread的错误代码或测试。
- 相邻持续任务／生成链路复核：`semantic-adjacent-tasks-v1.log/.exit/.command/.sha256`为5项通过、1项setup错误（112.20秒）；通过项覆盖event/state上下文只消费一次、编辑配置后仍读冻结原运行、繁忙目标生成任务排队／停用及真实Gateway继续原会话。fork场景初轮直接pytest缺正式watchdog，未进入业务；纠正调用方式后，`semantic-adjacent-fork-v2.log/.exit/.command`经原`integration-task-generation-strategies`矩阵、`PYTEST_ADDOPTS`只选该场景，1 passed／6 deselected（69.82秒），runner退出0，正式run为`run-03e97d908544`，来源指纹前后一致。核生成分支、精确回报、重复触发及原Provider请求均通过；没有放宽期限或改产品实现，不把整个套件／错误传递列为全绿。本轮未提交、同步、重启服务或转换用户数据。
- 扩查修复三个历史读取问题：大于65536字符的工具参数在进入展示DTO前沿原单项上限截断并标记，避免定点读取先发生长度校验失败；Turn的arguments／result与text／content统一计入原页面预算，并与无Turn执行共用原预算owner内的单一函数；混合正文／工具摘要请求不再从同消息的原始记录补入未请求的参数或结果，也不序列化未请求类别的工具正文。canonical参数、Provider还原、Item身份及声明／执行关系均不变，没有新增分页入口、持久状态或兼容分支。
- 工具目录测试的旧`extension_tools`调用已平移到正式`extension_definitions`发布合同，并核来源、schema及原分组，不恢复旧生产接口。`semantic-expand-mapping-v1.log`保留该旧参数失败；`semantic-expand-tool-budget-red-v2.log`保留合法codec输入下arguments／result越预算失败；`semantic-expand-summary-red-v1.log`与`semantic-expand-summary-http-red-v1.log`分别保留原mapper及真实HTTP混合请求携带未请求工具参数的失败。HTTP初轮另有新增测试漏带view binding的422，已修正测试请求，未放宽产品约束。
- 最终`semantic-expand-mapping-v3.log/.exit/.command`为229项通过（5.19秒）；`semantic-expand-history-http-v3.log/.exit/.command`为3项通过（18.06秒），覆盖真实持久化后的大参数Item读取、混合摘要、单项预算和只补读选中工具。`semantic-expand-ruff-v3.log/.exit/.command`中6个受影响Python文件检查通过；上述退出码均为0，均有外部timeout及数据段内存保护。`semantic-expand-final-v3.sha256`记录验证前源码并在验证后逐项核对一致；产物继续在原临时artifacts目录，未新增总结文档。本轮未变更前端／协议schema，不重复既有Web构建；不将定向后端检查称为新一轮完整浏览器验收，错误传递仍归另一Thread。
