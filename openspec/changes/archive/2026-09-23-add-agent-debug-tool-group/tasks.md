## 1. 配置与运行时契约

- [x] 1.1 核对并补齐既有`workspace_schema.jsonc`中`runtime.debug`、Node、Python预留与launch profile严格schema；不得新增thread级JSONC层或静默改变旧字段语义
- [x] 1.2 核对既有`workspace_inline.jsonc`安全Node Inspector默认值和完整`workspace_dev.jsonc`模板，保持loopback/动态端口、有效配置合并来源与既有`config_version`
- [x] 1.3 让ConfigService规范化读取已生效Workspace级debug模板并校验adapter/profile/端口；每个thread的活动方案保存在thread资源而非工作区JSONC，模板热更新不改正在运行的其它thread进程
- [x] 1.4 覆盖debug配置默认值、Workspace覆盖、未知字段、非法端口/timeout、同Session两个thread不同活动方案及模板热更新不串改的focused配置测试

## 2. Node 调试服务扩展

- [x] 2.1 将NodeDebugService运行时、断点、活动方案、动作索引从裸session迁为精确`(session_id, thread_id)`，保留Workspace级debug模板和每thread动态Inspector端口；受检thread catalog/path resolver定位`<thread_node>/debug/node/`，不得拼路径、扫盘或按session猜目标；Web/API独立调试mutation遵守Session生命周期准入
- [x] 2.2 保留并验证现有普通/条件/命中次数断点，`hitCondition`为正整数且在本次进程内计数，重启重置；信封目标参数不退化
- [x] 2.3 保留Node条件表达式实现的不暂停Logpoint、`logMessage`插值/条件/`hitCondition`及可识别输出；Node不得返回伪“不支持”，其它缺少能力的adapter才明确拒绝，补充真实Inspector回归
- [x] 2.4 将归属manifest、运行状态和动作审计绑定实际SessionThread并显式校验`session_id`、`thread_id`；可移植方案正文不含owner，由目标目录/manifest关联。同Workspace复制只按公开fork固定模式，source方案manifest/revision/bytes/hash进入同一SourceCopySnapshot；target发布前校验入口、工作目录、每个断点路径、有效profile/adapter/runtime并按需映射本地ID/lineage，失败使整个fork不可见，且不复制活动指针。Agent动作记录tool_call/ExtensionCatalogBindingRef，Web/API动作记录principal/request_id；同Session main/child不得共享活动状态、断点或动作时间线
- [x] 2.5 调试domain提供旧Session`debug/node/`的静态接线显式迁移步骤，接入itemized共享maintenance gate/journal并按冻结映射定点归入main；校验文件bytes/hash、方案ID/revision/lineage、恢复/失败原件，普通runtime无旧目录alias；公开fork限同Workspace，跨Workspace方案export/import留待独立授权协议，migration-only child copy不自动携带方案；不复制进程/端口且不停止source进程

## 3. Agent 调试工具组

- [x] 3.1 核对并补齐16个DebugMCP风格内层目标输入模型和JSON结果包装，保留`start_debugging.debugConfigurationId`、`add_breakpoint.hitCondition`、`add_logpoint.hitCondition`；严格拒绝未知/多余参数而不静默丢弃。启动顺序为显式ID→当前thread活动方案→无方案时安全路径创建；ID仅在受信thread解析。两个必填路径须在选中方案后与有效入口/工作目录规范化相等，显式profile须与方案解析结果一致；否则在任何方案激活/旧进程停止/新进程启动前返回带字段的`debug_launch_parameter_conflict`，不存在ID先报`debug_configuration_not_found`。目标schema由固定`invoke_extension_tool(tool_name, arguments)`在后端校验，不生成16份Provider tool定义，现有方案管理目标也走该信封
- [x] 3.2 保留启动、停止、重启、继续、暂停和三种单步目标，返回authoritative debug state；补齐`stopping`可观察状态，在进程真实终结前不解除idle blocker或提前报告stopped
- [x] 3.3 实现普通断点、条件断点、断点移除、断点列举和全部清理工具
- [x] 3.4 实现变量名、指定变量值和表达式求值工具，支持 scope 校验和暂停上下文校验
- [x] 3.5 从受信`ThreadRuntimeBinding`/ToolInvocationContext注入精确SessionThread、原model-callExtensionCatalogBindingRef和NodeDebugService；模型不能选择product thread、DAP frame或Inspector连接，旧generation回调不得写新owner，tool result保留原tool_call_id；debug owner按每次启动唯一process_instance_id在spawn前登记独立于短期调用的durable launch_pending claim/nonce，spawn后核对OS进程起始身份和Inspector握手才登记PID/端口，跨Turn保留typed lease及idle blocker。重启/停止失败或无法核实旧实例保持`reconcile_required`，不得以PID/端口单独认领或停止新进程；核实终态并结清lease后才解除

## 4. Agent 注册与策略

- [x] 4.1 将16个目标和既有调试方案管理目标保留在ExtensionToolCatalog的`debugging`分组；Provider仅见少量直接工具与始终存在、schema/description固定的`invoke_extension_tool`，目录空或启停目标也不改信封
- [x] 4.2 AgentFactory按每次调用的ThreadRuntimeBinding接入NodeDebugService和封存的ExtensionCatalogBindingRef；移除16个直接Provider注册/`invoke_extension_tool`旧模型入口，更新bundled debugging Skill为`skill_load(name="debugging")`→固定信封指引，删除`read_file`加载Skill/按Session共享调试状态的旧描述；不把目标清单拼进信封description
- [x] 4.3 调试目标各自遵守denylist、allowlist和`confirmation_required`；`evaluate_expression`执行点重验最新权限/确认，撤权与非法调用返回原tool_call_id配对的真实失败，目标切换不触发Provider ToolSet hard rebase
- [x] 4.4 更新目录、Provider工具schema、tool result、提示词和Web/API测试：16个目标名仅出现在内层目录/指引，固定信封仅接受tool_name/arguments，模型不能传session/thread、端口、DAP/VS Code字段；无旧`invoke_extension_tool`别名或提示

## 5. 纯后端 E2E 测试

- [x] 5.1 核对并扩展既有隔离JS调试fixture和后端E2E资源准备逻辑；Session/main/child经正常创建/catalog路径取得，不把项目根注册为测试工作区
- [x] 5.2 通过固定`invoke_extension_tool`与sealed目录binding验收16个内层目标名/兼容参数schema、指定`debugConfigurationId`与当前thread活动方案/无方案创建顺序、普通/日志点第N次命中、未知参数明确失败；补测路径相对/绝对归一化相等可启动、路径/profile冲突明确错误且旧进程/方案不变、无效ID先报not-found。Provider `tools`不含16个直接定义，启停前后ToolSetRef和同epoch父wire bytes不变；直接`ainvoke`仅作单元/契约覆盖
- [x] 5.3 使用真实 Node Inspector 验证断点暂停、继续、单步、调用栈、变量和表达式求值
- [x] 5.4 验证条件/命中次数断点、Node Logpoint输出且不暂停、插值错误、其它不支持adapter的明确拒绝、非法/未知参数和无暂停上下文错误；同时验收变量/求值/Logpoint输出脱敏及真实失败与原tool_call_id配对
- [x] 5.5 验证同一Session main/child及跨Session两个以上thread的进程、动态/冲突固定端口、断点、方案、状态、动作审计与stop/restart严格隔离；Session产品API只到main，显式child API校验归属；fake clock覆盖`launch_pending|starting|running|paused|stopping`跨过30分钟仍resident、终态且lease结清后重新计时、无阻断后cold、stop失败/重启`reconcile_required`、thread删除定点停止及旧generation callback拒绝。用注入phase barrier在claim提交后spawn前、spawn后登记PID前和stop核实前终止backend并重启，验证nonce/OS起始身份、PID/端口复用不误接管/误杀、同一实例只恢复一次及未知状态始终阻断
- [x] 5.6 验证Workspace debug模板/profile覆盖和旧配置无debug字段时的行为，并在itemized共享maintenance gate/journal下核对旧Session方案→main thread的bytes/hash、ID/revision/lineage、失败/重试/原件保留；同Workspace`context_fork`只复制capture时活动方案、`history_prefix_fork`不复制方案、`full_rollout_copy`复制全部当前方案、migration-only child copy不自动复制。冻结目标有效debug配置revision/hash并逐项重验入口/工作目录/全部断点路径和profile/adapter/runtime，发布前配置漂移或方案缺失使整个fork不发布，目标ID/lineage映射且不带active指针；崩溃恢复不半发布，跨Workspace公开copy明确拒绝，不复制活连接且不停止source进程；history/Web刷新与无旧路径alias

## 6. 验证与交付

- [x] 6.1 运行受影响的 Python 静态检查、类型/编译检查和 focused unit tests
- [x] 6.2 运行受影响纯后端E2E与信封/ThreadRuntimeBinding/fork/删除集成测试；迁移既有`test_debug_prompt_flow.py`和`test_debug_prompt_live.py`为确定性ModelStream下的`skill_load`→固定信封→真实tool result/刷新链，验证thread归属、权限、旧名称与读文件指引消失，并保留规定目录下产物；现有直接目标测试不能代替模型可发现与信封运行证据
- [x] 6.3 运行 `openspec validate --change add-agent-debug-tool-group --strict`
- [x] 6.4 更新任务状态并确认实现与 proposal、spec、design 一致
