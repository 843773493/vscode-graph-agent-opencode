## Context

当前`NodeDebugService`按session管理Node Inspector进程、断点、配置方案和动作；现有调试目标已通过`tools.custom`/`invoke_extension_tool`接入，但目标OpenSpec仍把16个能力描述为直接Provider工具。`NodeDebugSessionStore`仍在Session节点的`debug/node/`持久化，Node调试HTTP API也只接受`session_id`。新版上下文方案已经把SessionThread定为执行与工具binding owner，把Provider工具面固定为少量直接工具和`invoke_extension_tool`信封，因此需同时迁移调试目录与资源identity，不能只改工具名称。

本变更需要把已有 Node 调试能力映射为 Agent 工具，同时保留未来 adapter 的边界。当前不引入 VS Code 扩展或 debugpy 依赖，也不把调试端口、线程和 frame 标识交给模型。

## Goals / Non-Goals

**Goals:**

- 保留DebugMCP风格的16个调试目标名及各自参数schema，但只把固定`invoke_extension_tool(tool_name, arguments)`送入Provider `tools`；16个目标在ExtensionToolCatalog中经信封执行。
- 将工具调用的精确`(workspace_id, session_id, thread_id)`、model-call目录binding、tool call identity与配置依赖由受信`ThreadRuntimeBinding`/运行时容器注入，不要求模型传入任何owner或Inspector内部ID。
- 保留现有Node Inspector生命周期、执行控制、普通/条件/命中次数断点、不暂停Logpoint、变量检查和表达式求值；验证日志点不是会暂停的普通断点。
- 将 `runtime.debug` 接入现有 Workspace JSONC 合并和 schema 校验。
- 使用统一 JSON 文本结果，兼容现有 Agent tool output middleware，同时保持完整调试快照。
- 通过现有内层目标策略支持调试能力禁用和表达式求值人工确认；目标状态变化不修改固定Provider信封、ToolSetRef或同epoch前缀。
- 用纯后端E2E验证信封调用、真实Node fixture、同Session main/child及跨Session隔离、断点控制、变量/表达式和配置覆盖。

**Non-Goals:**

- 本次不实现 Python debugpy adapter、通用 DAP adapter 或 VS Code Debug API 路由。
- 本次不支持模型选择任意产品thread或DAP frame，不支持修改运行时变量；目标thread只能来自发起调用的受信ThreadRuntimeBinding。
- 本次不允许 Agent 通过 Inspector/debugpy 端口连接外部进程。
- 本次不改变 Web 调试面板的整体布局；面板只消费已有或新增的后端快照。
- 本次不把 `runtime.debug` 的 `program`、`runtime` 或 `adapter` 直接加入 Agent 工具输入。

## Decisions

### 1. 固定信封与内层调试目标分离

`app/agents/tools/debugging.py`保留16个`StructuredTool`目标factory及DebugMCP风格参数校验；现有调试方案管理目标也属于同一个内层调试目录。AgentFactory把这些目标注册到ExtensionToolCatalog的`debugging`分组，由固定、始终存在的`invoke_extension_tool(tool_name, arguments)`调用。目标名是信封的`tool_name`值，不是16个独立Provider工具名；不把目标清单/schema拼进信封description。模型通过受控的bundled调试Skill/工具指引获知名称与参数，正文只能作为CSM来源按生命周期方案生效，目标说明本身不授予执行权限。

每个目标保留稳定、全局无冲突的tool identity和各自公开输入schema；重名必须在目录candidate发布前拒绝，不按注册顺序覆盖。工具是否可调用仍由现有denylist、allowlist和`confirmation_required`在内层解析；`evaluate_expression`不绕过人工确认。目录/权限变化只改变下一激活边界的ExtensionCatalogBindingRef及必要user-role指引，不产生Provider ToolSet hard rebase；已sealed调用按产生它的binding解析，执行点重新校验最新权限，结果与原tool_call_id配对。固定信封即使没有可用调试目标也留在Provider工具列表。

原方案“16个直接Provider工具”与固定前缀/信封合同冲突，正式废弃；`invoke_extension_tool`旧名在实施时直接替换为`invoke_extension_tool`，不保留模型可见别名。DebugMCP兼容性限定为内层目标名、参数与结果语义，不声称Provider顶层工具协议仍与DebugMCP相同。

### 2. Add a narrow Agent-facing facade over NodeDebugService

工具 factory 不直接拼装 Inspector 命令，而是调用一个面向 Agent 的调试 facade。该 facade 负责：

- 把 DebugMCP 的 `fileFullPath` / `workingDirectory` 转换为当前 workspace 内的安全相对路径；
- 在当前受信thread内按显式`debugConfigurationId`→该thread活动方案→无方案时由已校验的`fileFullPath`/`workingDirectory`创建方案选择启动目标；ID只查当前thread目录，未命中统一返回`debug_configuration_not_found`，不查询其它thread、不退回活动方案；方案中的`configurationName`才解析Workspace launch profile；
- 保留兼容的两个必填路径字段，但在选中已有方案时先用当前有效Workspace profile解析方案的实际入口和工作目录，再把调用方两个路径规范化为相同的Workspace相对身份；任一不一致，或显式`configurationName`与方案解析出的profile不一致，返回`debug_launch_parameter_conflict`并指出冲突字段，不自动覆盖方案、不吞掉参数。显式方案ID查找失败仍优先返回`debug_configuration_not_found`，且上述全部preflight先于方案激活、旧进程停止及启动副作用；
- 保留`add_breakpoint`及`add_logpoint`的正整数`hitCondition`，在信封内部按目标schema拒绝未知字段和非法取值，不静默丢弃；
- 把工具动作映射为现有 Node 调试 action；
- 对 `list_variable_names` 和 `get_variables_values` 生成稳定的 scope 结果；
- 统一包装成功/失败 JSON；
- Agent动作使用`ToolInvocationContext.require_tool_call_id()`与封存目录binding写入审计；Web/API动作以受信principal/request_id标识来源，不伪造tool_call_id。

首期 facade 只实现 Node Inspector adapter。adapter 选择必须来自已解析的配置 profile；除 `node_inspector` 外的 adapter 返回明确不支持错误。

### 3. 调试资源由精确SessionThread拥有，内部标识不暴露给模型

`NodeDebugService`以精确`(session_id, thread_id)`作为运行时/断点/活动方案/审计索引，入口先用workspace及SessionThread catalog解析并验证owner，绝不凭session查找“最近一个”调试进程。Agent tool factory不捕获session单例；每次调用从受信`ThreadRuntimeBinding`取session/thread及原model-call扩展目录binding。main和child即使属于同一Session，也可各自启动Node进程、拥有独立动态Inspector端口、断点、方案、状态与动作时间线；重复start只替换当前thread的进程。Node Inspector端口/WebSocket仍只在实际debug owner私有，模型结果只含脱敏状态，不含可用于跨thread控制的句柄。

`NodeDebugSessionStore`及配置registry改从受检thread catalog/path resolver取得真实thread节点，并在该节点的`debug/node/`保存归属manifest、无owner的可移植方案正文和必要的持久审计；manifest、运行状态及动作审计DTO显式携带并校验`session_id`、`thread_id`，方案正文不含这两个字段，不能由调用者拼接Session/child目录。调试domain owner提供静态接线的确定性迁移步骤，在itemized共享maintenance gate和同一迁移journal下按冻结Session→main thread映射定点迁入旧Session节点`debug/node/`数据；这不是可安装plugin或第二迁移协调器。预先枚举并校验登记的方案/manifest、ID/revision/lineage、bytes/hash与目标路径，失败不半发布、不删除原件，普通runtime不读旧目录或扫盘补洞。活Node进程不在checkpoint/fork中复制：旧数据迁移前按精确owner真实收敛旧进程；本次仅同Workspace公开fork按下述模式复制方案正文，在目标thread重新校验并按需映射本地方案ID/lineage，不停止source thread进程，也不让target附着source Inspector连接。

本次公开fork/copy只在同Workspace：`context_fork`复制source capture时活动方案的当前正文，`history_prefix_fork`不复制调试方案（历史anchor没有相应时点的方案快照），`full_rollout_copy`复制capture时全部已保存方案的当前正文；migration-only `materialize_thread_copy`不自动复制调试方案。上述复制均不带active指针、进程、端口、动作审计或历史方案revision，target没有隐式活动方案；复制到target的方案按target-local ID映射并记录source lineage。跨Workspace的可移植正文格式仅为未来授权export/import保留，本次不开放跨Workspace复制API，也不能借Gateway federation的send/read/wait grant绕过这一限制。debug owner在source `SessionReadGuard`内对已登记方案manifest/revision与文件bytes/hash做一致性快照，校验前后revision不变，再把所选正文与hash列入itemized的`SourceCopySnapshot` artifact清单；漂移就使本次capture失败，不用当前source补读。target未发布staging中先冻结有效Workspace debug配置revision/hash并写入fork journal，再按同一配置快照逐一重验方案入口、工作目录、每个断点路径、profile存在性/adapter/runtime与权限；发布前配置revision漂移、方案缺失或语义不兼容均使整个fork失败，不跳过方案或静默改用默认profile。写入target方案manifest和ID/lineage映射后，才能随fork journal一起发布；崩溃只按冻结artifact与journal恢复或清理，不形成第二copy coordinator。

普通Session调试API按权威catalog解析main thread，当前Session的child调试入口须显式给出受检child thread并验证归属；两者最终调用同一个thread-qualified服务端口。Web/SSE/trace响应需携带实际product thread identity用于投影和隔离，但不能把模型输入中的`threadId`解释为product thread或DAP thread。调试start/stop/restart、Web操作及资源创建必须在SessionLifecycleGate下取得可恢复的operation admission/既有execution lease，并由debug owner以typed`node_debug_process`身份进入唯一external_resource_leases账本；两种lease职责不同，前者防删除竞态，后者记录跨Turn占用/恢复，均不替代调试owner判断进程实际状态。Session删除按lifecycle fence排空后定点清理其全部thread调试资源；单个thread删除仅停止/回收该thread的debug进程与资源。`starting|running|paused|stopping`进程是该thread的idle blocker，30分钟计时不得使其进入cold；仅当调试owner核实`idle|exited|failed`终态、结清相关lease且其余blocker为空后才可按原30分钟阈值卸载。进程退出、backend重启或停止失败时恢复owner需核实进程/端口、持久状态与lease，无法确认则保持`reconcile_required`/blocker而不虚报stopped或cold；`LifetimeScope`只释放进程内句柄。任何旧runtime generation callback都不能写入新thread owner。

`node_debug_process`账本占用由debug owner按每次启动唯一`process_instance_id`持有，不以tool_call、Web request或其短期operation lease为holder；启动先持久提交绑定thread、lifecycle generation、launch preimage与一次性启动nonce的`launch_pending` claim，随后才spawn。spawn后须核对nonce、OS进程起始身份、受检进程树/Inspector握手并把PID/端口作为可验证属性登记，再进入运行态；仅PID或端口相同不构成同一process instance。启动中崩溃时按claim/nonce定点恢复：能证明进程不存在则终结claim，能证明仍是原实例则继续接管或按owner策略停止，无法证明则保持`reconcile_required`并阻止idle/删除完成，不猜测成功或杀同PID的新进程。重启/替换先核实并结清旧实例，再为新实例建立新的claim；旧generation callback和旧nonce不能写新实例。stop失败同样保留占用，人工恢复也必须有明确核实结果才能解除blocker。

Node 当前只使用顶层 call frame 做变量和求值。`call_frame_id` 继续作为服务内部字段；不把它配置化，也不新增 Agent-facing `frameId` / `threadId`。

### 4. Resolve configuration through ConfigService

`ConfigService`继续提供规范化debug runtime配置读取。既有Workspace配置族的`runtime.debug`承载`node`、`python`和`launch_profiles`，发行内置默认在`workspace_inline.jsonc`提供loopback、动态端口和Node Inspector默认profile；不另造thread级配置参数。

NodeDebugService读取Workspace已生效的pinned config，而不是自行解析JSONC。`runtime.debug`仍是Workspace级默认adapter/endpoint/launch profile模板，由inline→user→user_local→workspace递归合并；每个thread可有独立的活动/可移植调试方案与进程，但不得另建`runtime.debug.thread`配置层或把方案反写到Workspace JSONC。新增字段全部可选，不因owner变更升级Workspace`config_version`；schema仍禁止未知字段。配置热更新只影响按其reload policy允许的新调试启动，不改正在运行的进程或另一个thread的方案。

`inspector_port: 0` 通过 Node 的动态监听方式实现。固定端口仅作为显式本地开发配置支持，且 host 必须是 loopback；8211 不写入 debug 默认值。

### 5. Extend the existing Node debug state minimally

沿用现有 `NodeDebugStateDTO`、断点、调用栈、求值和动作模型，补充工具所需的条件/logpoint 元数据和工具调用审计字段。普通/条件断点使用 Inspector `Debugger.setBreakpointByUrl` 的 condition 能力。

Node Inspector虽无与VS Code `SourceBreakpoint.logMessage`等价的独立API，现有Node adapter已把`logMessage`、可选`condition`与`hitCondition`编译为`Debugger.setBreakpointByUrl`条件表达式：命中时输出可识别日志并返回`false`，不得暂停或伪装为普通断点。本变更保留这一路径，明确插值/表达式副作用和失败诊断，验证日志只来自当前thread；只有未来确实不支持日志点的adapter才返回`UNSUPPORTED_DEBUG_FEATURE`，不能把Node已实现能力删除。

### 6. Return JSON text for LangChain compatibility

当前 Agent 工具和 ToolOutputMiddleware 已经稳定处理字符串结果，因此信封内目标将成功和可预期失败统一封为`json.dumps` JSON文本：成功为`{"ok":true,"message":...,"state":...}`，失败为`{"ok":false,"error":{"code":...,"message":...},"state":...}`；dispatcher的未知目标、schema/权限错误也必须形成与原`tool_call_id`配对的真实失败。基础设施不可恢复异常仍向上暴露详细诊断，但不得吞掉或伪造`ok:true`，且协议层必须收敛为配对terminal result。HTTP API继续返回现有`APIResponse`，不把Agent格式强加给Web API；模型结果沿用变量/表达式脱敏，不公开内部句柄。

### 7. Test the real backend without browser dependencies

`tests/e2e/backend/agents/test_debug_tools.py`使用隔离fixture workspace与真实Node Inspector，通过固定信封、冻结的ExtensionCatalogBindingRef和Agent目标factory执行，检查指定`debugConfigurationId`、默认活动方案、第N次命中、非暂停Logpoint、未知参数、实际进程状态、变量/表达式、单步、清理及同Session main/child隔离；再对照Provider sealed ToolSetRef与父wire前缀证明启停调试目标没有额外hard rebase。直接调用内层factory只能作为后端单元/契约测试，不能冒充模型信封路径E2E。现有debugging Skill和`test_debug_prompt_flow.py`/`test_debug_prompt_live.py`要迁至`skill_load(name="debugging")`→已封存目录的`invoke_extension_tool`真实模型请求/工具结果链，检查方案选择、thread归属、旧名称/`read_file`加载说明消失及刷新后一致性；确定性ModelStream可作为正式验收，不要求真实Provider。

测试还通过临时 workspace `.boxteam/workspace.jsonc` 验证 `runtime.debug` profile 覆盖和配置 schema；不启动 Web、Gateway 或 VS Code，不运行 `reference_repo` 测试。

## Risks / Trade-offs

- [Node Inspector 无专用logpoint API] → 保留已实现的条件表达式+可识别输出方案，测试命中时记录日志且不暂停，插值失败明确返回错误；不能以无专用API为由删掉现有能力。
- [表达式求值可以执行副作用代码] → 复用内层目标确认策略、强制记录tool call identity，并保持Inspector loopback/thread绑定。
- [调试目标不再作为Provider直接工具] → 用bundled调试Skill/受控工具指引说明16个目标名与参数；固定信封description不随目录改变，工具策略仍可整体或逐项控制内层目标。
- [配置 profile 可能与当前 Node 直接启动参数冲突] → 先定义规范化 profile 解析，未配置时完全回退到现有 Node 默认行为；固定端口和外部 adapter 不默认启用。
- [运行时状态与异步 Inspector 事件存在竞态] → 所有工具动作等待后端 authoritative snapshot；变量 hydration 完成后才返回暂停快照；E2E 保持暂停状态并验证重复读取。
- [错误结果需要同时满足 Agent 和现有工具错误处理] → 使用稳定错误 code 的 JSON 文本并保留异常边界；不返回虚假默认状态。

## Migration Plan

1. 复用现有Workspace`runtime.debug`默认/schema/开发模板并核对合并来源，不新建thread配置开关或改写旧用户配置。
2. 对齐生命周期change的固定`invoke_extension_tool`和ExtensionCatalogBindingRef；把全部调试目标保留在内层`debugging`目录，迁移相关工具提示词/测试，删除`invoke_extension_tool`别名与16个直接Provider注册。默认Node profile沿用动态Inspector端口。
3. 在itemized维护门槛下把旧Session调试方案定点迁到main thread节点；NodeDebugService、配置registry、API/Web/SSE/审计和路径resolver改为thread-qualified。活进程不复制/迁移，按真实owner收敛。未配置`runtime.debug`仍使用有效Workspace默认值；没有Node可执行文件时明确失败。
4. 如需回滚，按明确owner关闭本次thread调试进程，不复活旧Session目录或直接工具兼容入口；已有HTTP Node调试API只作为经catalog解析main thread的产品入口保留。
5. 未来增加 debugpy 或 VS Code adapter 时，只新增 adapter 实现和 profile 校验，不改变 16 个 Agent 工具的输入 schema。
