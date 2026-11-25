## 1. 公共输入与来源

- [x] 1.1 核当前DerivedModelRequest、准入、原图工厂、任务、工具及日志的直接／间接消费者；按design第4.1节确认参数解释边界和实际模式分支，避免整份DTO／运行时大对象层层透传。共享文件先明确写入方，保留压缩并行改动与有效证据。
- [x] 1.2 用唯一typed派生引用替换公共compaction_reference，task与execution／model call关联沿原身份；candidate／chunk／merge留压缩计划，迁齐冷reader及请求preimage。
- [x] 1.3 在原handler映射提供生产参数化入口，接收来源、提示词、工具允许列表、模型及有限期限／原调用上限；本次文本输出，不新增严格schema、权限票据或示例业务handler。
- [x] 1.4 沿原Saver冻结闭合来源及必要保留，默认active view不选draft，已有适用ready摘要仅作派生投影替换并随计划冻结；同键并发、append／热更新重试及回执丢失沿原门和唯一约束处理，失败临时引用归原owner回收。

## 2. 模型与工具运行

- [x] 2.1 沿原候选owner优先替换已有适用ready摘要，无则使用原内容，不等待或生成摘要；原投影／seal／dispatch复用root、tools及实际未变化前缀，完整保留其余来源及闭合尾部。每次请求含工具结果沿统一容量检查，未知沿原发送、已知超限或上游overflow真实失败留痕；不新增输入token预算或输出限制、不递归压缩，不重写压缩handler显式块计划。
- [x] 2.2 原model_call_graph工厂装配一项完整模型调用Runnable，替换derived_request／derived_result成对可选回调；主／派生准备在各自边界汇入原封存／投影／发送，移除prepared派生布尔开关与公共invocation中的模式判断，结果解码／收敛留各自owner。复用AgentToolNode及图循环，保持同步／异步契约，单次model call不终结整个execution，不新增策略框架或复制主Turn消息pipeline。
- [x] 2.3 工具允许列表在原invocation执行点与现行策略取交集，缺省空列表、信封真实目标及撤销沿原身份；未知／不支持上下文明确报错，不做历史工具保留或逐次兼容审计。
- [x] 2.4 运行显式绑定scope／admission／配置及自己的日志／delta，原工具并发、参数／结果限制和operation机制直接复用；主usage／CSM／view及工具Command不跨入来源Graph。

## 3. 任务控制与恢复

- [x] 3.1 删除同Thread所有派生合并，保留原后台运行1／等待4，原运行task集合增加总运行上限2；无后台允许两个交互任务，满载不登记交互等待，不新增分区计数和队列。
- [x] 3.2 期限覆盖排队至发布，沿原运行超时及查询／出队边界惰性结算；提升复用task不重置。执行装配显式绑定分类与admission，迁齐派生handler沿用agent默认分类的冲突；原模型构造处一次确定adapter实际重试参数，sync／async发送不再按purpose重复推导，不以分类字符串替代真实设置。SDK不形成隐藏重试乘数，不新建期限定时器或逐工具预算。
- [x] 3.3 明确不绑定父ExecutionPause、不进入主输入队列或从其resume；沿SessionStopService、SessionStopPolicy及BackgroundTaskResourceProvider处理interrupt／stop／删除／关闭，显式cancel仍归原注册器，不新增派生暂停API。
- [x] 3.4 原publication gate与fencing裁决结果／取消／到期，先存完整结果再确认task；有效holder仅查询，失效holder只补已有结果或真实终结，不续跑半链，未决operation沿原owner核实。

## 4. 数据与外部接口

- [x] 4.1 按design第9节核主／子Thread的控制库、rollout、正文、日志及Trace真实路径与关联，迁齐主聊天／搜索／fork的选择；不新增派生目录、内容库或存储reader。
- [x] 4.2 原保留／删除owner补齐必要关联，覆盖运行引用释放、来源删除与未决操作顺序、子Thread隔离及合法pruning；无新增GC／TTL，结果失效不重调模型。
- [x] 4.3 实现工作区派生POST／GET有界wait／cancel统一合同，内部与HTTP同准入、资源控制同取消；Gateway显式scope、request_id及realtime沿原入口，响应只给有界引用，复用既有错误／内容协议。
- [x] 4.4 压缩handler接入共同执行，保留其唯一模型、32次预算、分块／合并和候选采用规则；删除公共摘要硬编码，不改压缩阈值与学习策略。

## 5. 集中验收与减法复审

- [x] 5.1 用原确定性模型／工具装具经真实入口组合验同键并发／热更新重试、前缀与尾部、已有摘要替换／无摘要直接发送／超限留痕、工具拒绝及多步／同批配对、主流式隔离；核同步／异步调用绑定、原主会话解码／callback与派生结果归属，以及实际SDK尝试数，无新增摘要调用且压缩显式块输入不变，不为每句规范拆独立测试。
- [x] 5.2 验主执行暂停／恢复、默认interrupt保留与stop取消、用户现有后台策略、总额度及pending提升、排队到期、发布／取消、崩溃已存／未存结果及未决operation；断言真实控制入口，不手工伪造metadata。
- [x] 5.3 经实际Gateway核主／子Thread路径、跨scope查询、普通fork、删除及合法清理；100k历史／有界selection收集至少20个暖控制样本，记录源码指纹、原始时长、p95／max、字节与request_id，遵守既有完整延迟预算。
- [x] 5.4 跑受影响静态检查及必要定向测试，协议或前端确有修改再做真实类型检查与web build；所有测试带进程外期限及内存保护，不默认全仓长跑。
- [x] 5.5 最小充分实现复审：物理删除旧公共字段／发送入口及Thread泛合并，清理旧可选回调签名、prepared派生开关和重复purpose推导；不保留兼容入口，不新增策略注册器、同形DTO、权限票据、版本工具池、分区配额、期限服务、暂停状态机或重复持久化。核两份change分工并严格校验，产品验收后才勾选。
