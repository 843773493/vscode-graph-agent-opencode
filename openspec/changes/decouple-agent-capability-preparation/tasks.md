## 1. 固定查询入口与改动边界

- [x] 1.1 按 design 查询入口表核对 HTTP、Workbench、模型工具与内部 reader，逐链确认范围、当前／冻结视图、权威来源及既有派生／分页；将实际缺口按三类迁移判定写回本 design。检索待删除目录 mode、执行服务目录入口、custom 身份字段和模型准备的全部直接／间接调用方及测试，核对 GraphBinding 与共享写入方，不开展全业务重构。
- [x] 1.2 用代表性现有装具记录工具目录／详情、正式请求工具面、压缩规划和本地失败基线；对每项拟改动确认其在 LiteLLM 消除导入耗时后仍有必要，保留已有查询核心、上下文分页器、WorkspaceSearch Provider 与 Gateway 调度，不新增查询基类或总结文档。

## 2. 工具目录纵向迁移

- [x] 2.1 查询与执行共用原参数模型、公开 schema 导出和实际共享的构造代码；compact／skill_load 等复用唯一参数声明，动态 factory 保留必要本地构造。直接复用 build_deep_agent_tool_middleware、build_custom_tools 和公开 exporter；原 RegisteredToolSetProducer 的普通工具 parameters 同源化，保留 Provider 包装／排序／结构化输出。不引入 Prepared 对象、待绑定队列或通用 registry。
- [x] 2.2 CustomToolFactoryContext 的真实 SessionThread 收敛到原 invocation binding，迁齐 browser、debugging、connections、web、session_history 等消费者；定义读取保持无 Thread，删除重复 session_id 与合成身份。
- [x] 2.3 AgentToolCatalog 直接消费最小定义依赖，迁入原目录缓存；原 list／get 与内部消费者共用该读取链，容器接入原实例，不另建查询服务。迁齐容器、API 和内部调用方，删除执行服务目录方法／缓存及 custom 二次补写，保持完整 schema、禁用／隐藏项和稳定 ID，custom 原 origin／分组由既有 helper 提供，配置描述覆盖保持；验证 origin／group 策略匹配不因映射默认为 builtin 而改变；聚合时在转名称字典前拒绝重名，不让投影覆盖掩盖冲突。
- [x] 2.4 在已有共用 publication_lock 内捕获匹配的 active 配置与一次 MCP publication 引用，不借外层旧 pinned 配置拼当前目录；锁外完成构造／导出，selection 沿原事务捕获，不新增全局锁。按原 revision 失效，有界缓存仅保存可序列化定义，连接状态从原 owner 投影，删除暖读求键的全量 schema 导出，按 design 的检查对象／触发边界表复用 schema 成功回执，保留每次参数、准入、连接代次和历史身份检查，不增加缓存层、校验服务或版本账本。
- [x] 2.5 真实工具 API 定向覆盖中间件、custom、debugging、MCP 的完整参数，以及 custom 描述／分组／策略、禁用、冲突、无效返回／schema 和模型凭据不可用；用原发布点的受控交错验证配置与目录只返回匹配组合且构造不持发布锁；实际取得临时资源者按原 scope 清理，验证失败／取消不泄漏，不给无资源 factory 增设生命周期接口。

## 3. 执行顺序与旧模式删除

- [x] 3.1 在原 generation builder 中将模型无关的工具构造／本地校验前移，动态工具一次构造后直接用于执行；依赖模型／CSM 的工具留在原必要位置，复用参数声明，不以返回待绑定对象实现多阶段框架。
- [x] 3.2 保持原 admission、冻结 selector、配置／凭据 revision、generation key、single-flight、scope、publication 与封存顺序；提前取得资源时，模型／图失败及取消沿原清理机制收尾，覆盖真实新增的释放窗口。
- [x] 3.3 迁齐 runtime／factory／graph wrapper 和测试后，删除 AgentAssemblyMode、assembly_mode 参数／分支、联合返回类型、inspection Session／Thread 与假执行回调；保留原无 Graph 的受信 untrack 路径和必要校验。
- [x] 3.4 用新进程在实际 Provider SDK／模型构造、网络与图编译边界验证无效动态 factory 的真实发送入口独立性，允许必要框架类型导入，不笼统封禁整个 langchain_core／langgraph 包；验证正常执行、两 Thread 隔离、同代复用及 primary 失败后 fallback 成功，不要求模型相关绑定错误提前至模型构造前。

## 4. 收窄实际未使用的模型准备

- [x] 4.1 压缩检查直接使用现有权威配置解析与 build_model_from_provider，只解析所需模型凭据，去掉未用候选／路由／指令；保留 profile、原计数／切分和 idle／write admission，沿原 scope／调用方所有权关闭所建模型，不重复实现已存在的候选托管；不新增模型准备层或假模型。
- [x] 4.2 显式 override_model 使用原实例，不先构造随后弃用的配置模型；保留执行路由、工具和 Thread 校验。prepare_agent_model 继续服务原执行组合，不为此重新拆分 instruction producers 或已有模型查询服务。
- [x] 4.3 定向验证压缩分区／profile 与原规则一致、忙碌 Thread 准入和 override 路径；复用已有模型发现／测试独立性证据，若其源码未受影响不另建验收矩阵。

## 5. 定向验收与复杂度复审

- [x] 5.1 经真实 Gateway、隔离工作区和 Workbench 验证工具列表／详情、本地失败及正常会话操作；验证目录／详情及内部 reader 共用原 catalog、无合成身份；更新当前目录后，同时验证管理读取取得新值而原调用的 search_tools 仍消费冻结定义；仅其他 cursor 输入不变时续页成功，策略／readiness 等输入变化仍按原冲突拒绝，当前执行资格继续生效。原查询失败明确返回，不退回旧执行装配或另一范围；补验证本次改动触及的模型／上下文消费者，其他 Skill／状态查询保持原实现，不重做全页面或六编辑器专项工程。
- [ ] 5.2 工具目录暖 API 与本地失败点击至错误 DOM 各采集至少20个未筛选暖样本，冷进程样本单列；保留规模、源码指纹、request_id／执行关联及 p50/p95/max，遵守既有500／1500／3000ms预算，超预算先归因，未达标如实保留未完成。
- [x] 5.3 集中完成受影响静态分析、定向合同和集成测试；前端或协议消费者有变化时完成真实类型检查及 Web build。测试使用进程外超时／内存保护，保留原始输出和退出码，补核管理 schema、ToolSet 冻结值及真实发送请求的公开参数约束一致，保留必要 Provider 格式转换而不要求 wire 文本逐字相同，覆盖原 extra 策略／隐藏字段／字典 schema；复用已有 schema 缓存与冻结测试，不为验证每层函数调用次数而新增脆弱测试。不默认全仓长跑或重复取绿。
- [x] 5.4 更新涉及文件的已有 AGENTS.md，检索确认旧模式／伪身份／重复定义和悬空调用清零；仅新建源码目录时补四节说明，不新增总结文档。
- [x] 5.5 从修改前重新推导最小方案，删除多余 wrapper、Prepared／binder 框架、重复状态／校验／缓存、无必要生命周期接口与测试；按查询入口表检查没有新增同视图 reader、第二派生／匹配／分页器、联邦调度或聚合缓存；按三类迁移判定逐项复核原 Skill／CSM／上下文／摘要／状态及搜索 owner 未被重复实现，核并发差异，仅补验受影响部分，不自动同步 dev、重启或提交。

实施验收记录：代码与契约检查见 design 的“实施证据与未通过项”。5.2 已采集完整样本，但暖失败 p95 超预算，保持未完成；不能用目录延迟或局部测试替代。

2026-11-22 用户要求先修复已记录的附带问题，暂不处理点击发送至错误可见的延迟。附带修复及验收见 design 第三轮记录；5.2 保留原预算与未完成状态。
