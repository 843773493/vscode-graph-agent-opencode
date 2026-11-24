## 1. 冻结接入合同与现有owner

- [x] 1.1 核对normalize-semantic-items的真实format3、Item／消息组、工具闭合与fork最终合同，列清所有自动／手动压缩、预览、模型路由、来源和请求封存的直接／间接调用方及现有测试；只更新本change对应设计落点，不新增总结文档。
- [ ] 1.2 对照实际配置继承、模型元数据及统一错误合同，确定唯一typed准备输入／结果和预算证据字段；与并发owner协调schema／协议写入，不复制loader、CSM或错误协议。
- [ ] 1.3 核对真实发布支持来源和rollout结构版本，登记候选元数据归属与原数据库结构变更；无基线不推测旧格式兼容，不预占版本号或转换真实开发数据。
- [x] 1.4 按design真实代码缺口表确认Graph入口、BackgroundTaskRegistry／SessionControl、summary_compiler、MessageService、replay和history.md全部消费者；确定唯一派生准入、候选结果与active view权威，避免新增任务／消息状态副本。

## 2. 实际模型预算与只读规划纵向切片

- [ ] 2.1 复用原Provider／Model resolver和冻结投影，完成包含root、tools、sources、历史、附件／媒体的预算；区分输入／总窗口、真实输出预留、I／C交集及R未知、容量状态与计数质量分离，不增加上游输出限制。
- [ ] 2.2 在Workspace既有context域增加compaction策略及schema／内置默认值，校验0.55／0.70／0.85和两种应用策略，沿实际extends解析并接入auto_summarize及下一准备热更新。
- [ ] 2.3 将原手动预览接入只读规划，移除为了查询构造模型／Graph的路径；迁齐真实API消费者，保留scheduled与已应用的区别，不新增并行控制API。
- [ ] 2.4 定向验证同名跨Provider容量、完整schema／媒体计数、unknown、auto_summarize关闭时90%仍可发送／overflow不自动补压缩、热更新和预览无模型调用；核真实配置加载与序列化，不只测手填metadata的helper。
- [ ] 2.5 沿现有projection compiler抽取共用只读计算阶段，预算／能力／最终root与tools计数使用同一冻结计划；禁止先seal再测容量，模型实例和元数据绑定同一配置快照。

## 3. 单一完整覆盖摘要生成切片

- [ ] 3.1 将安全规划迁为语义Item／消息组／工具闭合边界，保留必要新输入、保护载荷和尾部；去掉HumanMessage或整Turn作为强制唯一切点的限制。
- [ ] 3.2 在现有summarization生成链实现冻结输入、逐块全覆盖、有序合并与缩减检查；派生请求使用实际摘要模型预算及独立身份，不触发自身压缩、不注册工具。
- [ ] 3.3 实现一次绝对任务期限、32次总派生调用预算、最多三层合并与一次外部失败重试、冻结模型及在途／已终结补尝试期限区别；完整处理能力不足就明确失败，不做部分来源成功。
- [ ] 3.4 全量迁齐并物理删除suffix-only截断重试和错误字符串伪成功路径；复用原安全投影，禁止静默剥媒体、解密受保护reasoning或合成工具结果。
- [ ] 3.5 定向验证前段关键约束不丢、三块全覆盖、单块失败、空结果／工具输出、可分单项的分段全覆盖／不可分大单项、合并无缩减与预算耗尽；断言真实派生请求输入及最终覆盖manifest。
- [ ] 3.6 将Graph适配与typed派生输入收敛到唯一请求owner，原internal execution增加compaction_reference输入；复用plan／seal／adapter，不伪造checkpoint或父准入，不新建execution_origin／发送器。
- [ ] 3.7 派生请求只读冻结范围与保留资源，使用独立root、空ToolSet、日志／usage及请求上下文；删除对live CSM、当前工具收敛和父Graph的依赖，移除tool_free／visible_text等隐藏试探，SDK重试计入共同预算。

## 4. 候选冻结、持久化与幂等应用切片

- [ ] 4.1 沿原rollout owner新增最小候选记录／读写端口及必要schema变更，正文和保留引用复用原存储；不建立全局库、独立生命周期账本或通用DAG。
- [ ] 4.2 实现真实来源范围冻结、append可复用、范围编辑／删除／rewind失效、实际读取source变化失效；无关source变化只重算最终预算，避免以整体revision一律失效。
- [ ] 4.3 实现最多两份可用候选、S1→S2直接依赖及扁平覆盖，区分S1+B实际输入与A+B扁平覆盖，支持S1未应用／已应用两种真实selection，互斥选择；无新增内容且目标不变不重复，真实窗口缩小时允许一次S2(S1)有界提案，仍不足明确失败。
- [ ] 4.4 在原compaction提交事务原子消费候选、追加summary Item、切换view并登记pending transition；原source／seal事务消费transition，保持旧历史及assembly不变。
- [ ] 4.5 定向验证append竞争、候选重复消费、应用后seal失败与重试、source相关／无关变化及S1／S2竞争；核真实SQLite事务、canonical selection和请求hash，不只测内存状态。
- [ ] 4.6 原Saver用typed intent原子保存compaction view与checkpoint恢复绑定；迁齐Graph通道、MessageService上下文查询、replay、fork／rewind，删除_summarization_event数组二次投影及force／continuation控制路径，旧内存不得复活已排除成员。
- [ ] 4.7 原文回读工具复用canonical provenance及原owner增量行范围索引，保留公开参数和有界读取；删除新history.md写入／全文重写及其依赖，旧原件不擅删、不双读，核重复压缩、失败候选、rewind后事件顺序追加、fork原文缺失及pruning保留行区间的分页行为。

## 5. 后台调度与取消恢复切片

- [ ] 5.1 在原BackgroundTaskRegistry注册版本化compaction handler，复用SessionControl任务／租约／驻留／取消；补derived_request窄恢复语义并迁齐原handler，LifetimeScope仅管理任务内资源。限制每Thread一个在途、工作区一个后台调用及4个待运行Thread，不另存运行状态或复用普通Agent槽。
- [ ] 5.2 接入70%提前触发、重复触发合并、on_demand／next_request与最终阈值复用在途任务；后台失败转首次阻断只使用原预算内剩余一次重试，重复准备不重置；前台不等待后台信号量，网络等待不持写锁。
- [ ] 5.3 沿既有查询／Trace／统一实时通道暴露最小候选状态及真实错误；后台失败不污染当前Turn，不新增常驻连接、审批轮询或每请求完整性巡检。
- [ ] 5.4 完成中断优先、卸载／停机scope释放、晚到结果拒绝、ready恢复与失效lease处置；Thread／Session删除和候选淘汰复用原资源保留／清理机制，不全库续跑冷Thread。
- [ ] 5.5 定向验证后台中正常发送、同范围任务复用、前台并发优先、取消晚到、重启ready／generating和删除竞争；核主Turn／final／execution身份隔离及资源释放。
- [ ] 5.6 实现rollout先幂等提交完整ready结果、后确认原task；验证两步间进程退出、有结果优先于未确认task且只补确认／无结果不盲重放、发布与取消先后裁决，以及ready不阻止卸载、队列满不虚构scheduled、pending提升不重复spawn。
- [ ] 5.7 接入稳定Item／工具／source提交后的合并观察点，覆盖回复后跨阈值但无下一条用户消息的场景；不逐delta计数、不新增定时扫描，描述符只保存非秘密引用。

## 6. 所有模型与上下文入口迁齐

- [ ] 6.1 在唯一model-call入口完成实际模型选择后的统一准备；普通发送、工具循环和无Turn内部调用均走此链，移除middleware按初始模型阈值的另一权威分支。
- [ ] 6.2 接入实际fallback重新预算、候选优先及不足时阻断生成；区分纯provider_changed与真实compaction，正文改变创建新seal／request hash，容量不适合与上游失败分别归因。
- [ ] 6.3 接入手动scheduled请求、消息／附件注入、source／ToolSet变更和中断续跑；保留原可信ingress、工具闭合、用户队列和取消语义，不复制各业务查询入口。
- [ ] 6.4 fork目标沿原物化后在首次执行统一准备，原父view／未采用候选不复制或改动；rewind／编辑沿原owner失效候选，不移动用户anchor或扩大选定范围。
- [ ] 6.5 真实执行入口验证400k→200k、不足候选只模拟不采用，补足后一次提交、强制保留部分超窗、unknown的有界overflow反馈及全部候选不可适配；核每次实际发送正文、模型身份、失败归属和可重建hash。
- [ ] 6.6 手动API新增显式Thread目标且省略保留main语义；模型工具／HTTP汇入同一intent，子Thread前端携真实归属，tool result闭合前不应用；状态沿既有上下文查询／realtime区分准备、ready及实际采用。
- [ ] 6.7 验证旧媒体合法摘要后能力降低、新媒体仍须vision、试过未采用模型不改view、最终source恢复导致预算变化、每次准备至多一次compaction及有界CAS重评；核主assembly／输入消费／前台usage不被派生请求污染。

## 7. 产品入口、性能与最小充分实现复审

- [ ] 7.1 用隔离工作区和既有本地确定性模型装具，经实际Gateway／Backend／Workbench验收：提前生成不中断对话、最终阈值命中无新增摘要等待、无候选阻断及失败可见；截图与运行产物按现有out/tests规范保存。
- [ ] 7.2 在50k／100k语义Item历史、有界active selection与候选范围下，以至少20次真实准备／seal记录完整p50/p95/max与未筛选原样本；验证热路径p95≤500ms、单次≤1500ms，冷读和上游等待单列，禁止全历史扫描或以scheduled响应替代完成。
- [ ] 7.3 对实际修改文件集中执行相关静态分析和类型检查；若修改前端DTO／设置／状态展示，执行真实前端类型检查及 `bun run --cwd src/clients/web build`。所有测试带进程外期限和内存保护，不默认全仓长跑。
- [ ] 7.4 复审并删除重复预算／任务状态、shadow history、第二组装器／发送器、旧预览构建器、旧阈值／suffix重试、offset事件投影、Markdown归档写入及无需求兼容分支；逐项证明新状态／校验／持久化必要性，迁齐全部调用与测试后物理删除旧代码。
- [ ] 7.5 核对实际差异、源码指纹、准确命令、退出码、未验证范围和前置变更归档顺序；严格校验OpenSpec，只有真实验收通过才勾选对应实现任务。
