# 目录用途

完整工作台的业务搜索贡献，连接原版 SearchService 与原文件查询 owner，并按完整资源身份限制查询和本地模型。

# 可修改内容

原版搜索服务的业务分支、file/text Provider、查询条件及后续 Quick Access 范围接线。

# 不可修改内容

不复制上游搜索 UI、不维护文件索引或业务状态，不解析公开 URI grammar，不改写固定 upstream。

# 规范

使用唯一文件投影及现有 Gateway 传输；查询发起前限制根、extraFileResources 和模型。取消和 dispose 释放实际操作；不支持的选项明确拒绝。原生虚拟容器沿原服务，业务资源不进入浏览器文件 handle worker。

Quick Access 的 Session／Thread／文件名称及消息、工具结果、LLM 请求记录和 Trace 正文只通过共享 Gateway 联邦客户端查询；本层保留候选投影、原 Quick Input 展示及接受动作，续页／重试传回冻结范围和单个 owner 分支。Trace 沿精确 Thread 打开原事件详情，不拉全量日志、不查询未选运行日志来源。文件根元数据由原有界加载器读取，原 QueryBuilder 保留各根配置；领域查询不再接回前端跨目标队列。普通文件搜索自动继续各根原 cursor，Quick Access 明确单分支继续；两者复用同一后端文件 Provider，原 fuzzy scorer 只负责名称展示评分和高亮。

Quick Access 分支只按后端 `state` 区分取消与失败：`cancelled` 显示取消、保留已有候选及原 code／reason／request_id，原 `retryable` 控制单分支明确重试；`failed` 与 `deadline` 保持原失败和超时语义，不从消息文本推断状态。

`workspace ` 只调用原 Gateway 登记发现 API，沿原导航 cursor 有界续查；浏览、范围选择和取消不启动、连接或激活目标。工作区切换、连接与启动分别注册一个具名 Action／Command，Gateway 菜单和 Quick Access 共用它们；执行前固定并重新核验完整 owner，最终交原导航 runtime 和 Backend 完整组确认，不新增业务状态机。

运行日志与 Gateway 自有日志使用同一联邦客户端的 `runtime_log` 查询；每个 Gateway 的每种明确来源各一条分支，同时选择时保留两者。工作区只筛选关联进程，共享文件不按工作区重复查询。原行引用仅作具名诊断定位动作，不新造资源 URI，接受后交给原 Gateway 诊断视图精确读取。

`#` 的明确工作区范围必须等于原 IWorkspaceContextService 的完整窗口工作区集合，确认后才调用原 WorkspaceSymbolProviderRegistry。部分范围和没有 Provider 时显示具体原因，不查其它目标后过滤，不恢复旧用户宿主。原排序、接受、resolve 和取消继续由原 SymbolsQuickAccessProvider 持有。

明确范围沿原 getWorkspaceSymbols 的可选失败回调保留成功候选并报告具体原因；取消后不发布迟到错误。原无范围查询、公开符号命令和未传回调的消费者保持原宽容语义，不复制聚合或维护第二份符号索引。分支重试与增量发布仍须沿原 Picker 迁齐。
