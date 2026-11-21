# 目录用途

完整工作台的业务搜索贡献，连接原版 SearchService 与原文件查询 owner，并按完整资源身份限制查询和本地模型。

# 可修改内容

原版搜索服务的业务分支、file/text Provider、查询条件及后续 Quick Access 范围接线。

# 不可修改内容

不复制上游搜索 UI、不维护文件索引或业务状态，不解析公开 URI grammar，不改写固定 upstream。

# 规范

使用唯一文件投影及现有 Gateway 传输；查询发起前限制根、extraFileResources 和模型。取消和 dispose 释放实际操作；不支持的选项明确拒绝。原生虚拟容器沿原服务，业务资源不进入浏览器文件 handle worker。

已打开模型的正则查询及结果自动更新共用 OpenModelSearch，在本次计算独占的 worker 中执行原 TextModelSearch，并从同一快照生成预览和上下文；保留未保存正文、整词／多行／UTF-16 坐标和原结果配额。首次结果直接消费已计算命中，不在结果模型重新匹配。取消、身份或模型变化及总期限到达时终止对应 worker，不等待同步正则返回，不持久保存模型副本。结果模型关闭时仍可完成最后一次快照更新；重新绑定、变更或释放结果时取消旧计算。普通字面量仍沿模型原生快速匹配。

编辑器 Ctrl+F 的正则查找、导航和捕获组替换共用该 worker；大批量替换的整篇正则替换也只在 worker 执行。多光标的“选择所有匹配项”沿同一查找模型取得正则结果，保留其原搜索范围及主光标选择。原查找模型按序结算操作，正文、查询、光标变化及关闭立即取消旧计算；保留原装饰、选区、替换命令和撤销 owner，不建立模型正文缓存。构建与开发通过 `findTransform.mjs` 接入同一原版查找链路，普通字面量查找保留原生实现。

额外文件按原 URI identity 去重并保持所选目录和显式 glob 约束：名称沿原文件匹配消费给定身份，正文走同一 Provider 的精确文件分支。目录查询在正文读取、输出计数和分页前排除这些身份，不重复扫描；已打开模型不额外套用磁盘 ignore，零命中也覆盖同文件旧正文。原 onlyOpenEditors 仍只查明确打开文件。

Quick Access 的 Session／Thread／文件名称及消息、工具结果、LLM 请求记录和 Trace 正文只通过共享 Gateway 联邦客户端查询；本层保留候选投影、原 Quick Input 展示及接受动作，续页／重试传回冻结范围和单个 owner 分支。Trace 沿精确 Thread 打开原事件详情，不拉全量日志、不查询未选运行日志来源。文件根元数据由原有界加载器读取，原 QueryBuilder 保留各根配置；领域查询不再接回前端跨目标队列。普通文件搜索自动继续各根原 cursor，Quick Access 明确单分支继续；两者复用同一后端文件 Provider，原 fuzzy scorer 只负责名称展示评分和高亮。

Quick Access 分支只按后端 `state` 区分取消与失败：`cancelled` 显示取消、保留已有候选及原 code／reason／request_id，原 `retryable` 控制单分支明确重试；`failed` 与 `deadline` 保持原失败和超时语义，不从消息文本推断状态。

`workspace ` 只调用原 Gateway 登记发现 API，沿原导航 cursor 有界续查；浏览、范围选择和取消不启动、连接或激活目标。工作区切换、连接与启动分别注册一个具名 Action／Command，Gateway 菜单和 Quick Access 共用它们；执行前固定并重新核验完整 owner，最终交原导航 runtime 和 Backend 完整组确认，不新增业务状态机。

会话重命名、上下文分支与删除由 `sessionActions` 的同一 Action／Command 交原导航对话框、确认与生命周期 writer；导航菜单和 Session 候选按钮不另建 writer。显式 Session／分支快照优先于发起 Thread，发起 child 的分支保留精确 Thread，仍不明确时用原 `session ` Provider 只选择，不先读取 main Thread 或打开详情。命令重解稳定 owner、核验 ready 状态及权威会话标题；原确认前身份失效拒绝写入，已受理操作继续由原 owner 结算，不重放、不把排队回执说成物理删除完成。

管理调用的 `resourceSelection` 明确区分 Session 与 Thread，只选择时返回原领域身份，不执行打开或行内动作。资源关联复用专用 `thread ` 前缀和冻结的单 owner；不新建 Thread 目录或只枚举已打开编辑器，folder 不作为接收者。

Thread 名称与最近候选的分支按钮直接交同一 Session Command，保留候选中的完整 Gateway／工作区／Session／Thread 身份，不先打开候选；消息与 Trace 正文命中不以缺少快照的候选按钮创建分支。

最近历史的接受、侧开、移除和引用动作均先核本次身份有效，再创建组或调用原 owner；失效的旧回调不得继续展示动作。范围选择切换到独立 Quick Input 后仍拥有同一身份约束，取消原选择且不重开失效列表，结束即释放订阅；沿原 CancellationToken，不维护第二套范围状态。

运行日志与 Gateway 自有日志使用同一联邦客户端的 `runtime_log` 查询；每个 Gateway 的每种明确来源各一条分支，同时选择时保留两者。工作区只筛选关联进程，共享文件不按工作区重复查询。原行引用仅作具名诊断定位动作，不新造资源 URI，接受后交给原 Gateway 诊断视图精确读取。

`#` 的明确工作区范围必须等于原 IWorkspaceContextService 的完整窗口工作区集合，确认后才调用原 WorkspaceSymbolProviderRegistry。部分范围和没有 Provider 时显示具体原因，不查其它目标后过滤，不恢复旧用户宿主。原排序、接受、resolve 和取消继续由原 SymbolsQuickAccessProvider 持有。

明确范围沿原 getWorkspaceSymbols 的可选失败回调保留成功候选并报告具体原因；取消后不发布迟到错误。原无范围查询、公开符号命令和未传回调的消费者保持原宽容语义，不复制聚合或维护第二份符号索引。原聚合器公布逐 Provider 结果，增量与最终结果共用原去重和评分；原 Picker 的逐次 token 管输入、关闭及重试取消，失败按钮只重读对应 Provider，不重查成功分支。

默认 Quick Access 的 `search.quickOpen.includeSymbols` 与 `#` 共用明确范围符号查询、原 Provider、评分及单分支重试；默认入口保留原全局符号过滤，附在文件组后。两组结果共用查询 token 和加载状态，文件限定／调用方 delegate／行列定位不启动符号查询。接受和侧开沿原 resolve 与修饰键，并固定发起编辑器组；不复制符号打开链。
