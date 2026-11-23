## Context

### 当前代码事实

本设计针对工具目录与执行装配的实际依赖，动机见 proposal。2026-11-22 检索得到：

| 入口／代码 | 已有边界 | 当前耦合或需要保护的行为 |
|---|---|---|
| `AgentService.list`、`ConfigService` 的 inspection／来源读取 | 已按配置 snapshot 读取，不编译图 | `available` 是本地配置判断，不能被解释为上游探测成功；不要为拆分新增 SDK 校验 |
| `AgentToolSettingsService → AgentToolCatalog → AgentExecutionService.list_tool_definitions` | 目录已有独立 HTTP API、策略投影和缓存 | 目录仍进入执行工厂；`tools_inspection_session` 和随机 Thread 只是合成身份；custom 工具又在 catalog 层按配置补写，普通 custom 的参数可能为 `{}` |
| `assemble_agent → assemble_deep_agent` | `tool_catalog` 分支跳过模型与最终图编译 | 仍预载大工厂、获取执行依赖、构造 CSM／文件 backend 等对象；模式条件散布在归属、Skill、订阅、产物读取和返回类型中 |
| `_acquire_step_agent` | 精确 SessionThread、admission、generation key、single-flight、scope 均已存在 | 凭据 revision 属于 generation key；builder 绑定 snapshot 后建 Skill／资源，再调用装配；不得另做执行缓存或每请求重建图 |
| `prepare_agent_model` | 已不创建工具和图 | 一次准备所有候选、路由和 instruction producers；正式执行在工具 factory 前构造 SDK，`override_model` 路径也可能先做无用的配置模型准备 |
| `ConfigLlmProbeMixin`、`provider_model.py` | 已有发现／测试、本地校验、单模型构造 | 保持独立，不统一成“先构建 Agent 再测试”；跟随进行中的配置继承改动，不复制旧 defaults／overrides 解析 |
| `build_workspace_skill_catalog`、`start_workspace_skill_catalog_publication` | catalog 与后续订阅已有两个入口 | build 同时登记／发布 AGENTS 和目录资源；管理查询不能把整个 builder 当纯读取，更不能借用某个活跃 Thread 的 CSM |
| `AgentSummarizationCompactor.check` | 已不构建完整图，不执行上游压缩 | 为 cutoff 检查准备了 Agent 全候选／路由／指令；实际规划还依赖 model profile、模型类型的 token 估算与第三方切分规则，不能直接换算法 |
| context projection、checkpoint reader、`ThreadResidencyTracker.snapshot`、runtime status | 已可直接查询持久事实或驻留状态 | 查询不得改接 `acquire`；cold 是有效状态，不需要先加载图再回答 |
| 工具能力测试 | 已有 `tool_test_tools.py` 与独立测试服务 | 保留专用隔离 sandbox；实际执行某个工具允许加载其实现，不等于必须启动 Agent |
| 插件／MCP／调试贡献与资源列表 | 已有插件 bindings、MCP 状态、调试 capabilities／配置、终端／浏览器资源 API | 保持定义／状态查询与 initialize／start／create／attach 命令分离；不为本次拆分重做插件或资源 owner |

上批真实入口延迟证据见 `../unify-failure-reporting/design.md` 的“快速失败延迟的进一步定位”，原件位于 `out/tests/temp/2026/11/22/unify-failure-reporting/artifacts/latency-diagnosis/`。无 profiler 的一次冷样本中，错误 DOM 可见约5378ms，模型准备约3824ms，其中首次 LiteLLM 导入约3629ms；暖样本约928～2067ms。各阶段有包含关系，不可累加；不能把一次样本当 p95，也不能把所有剩余延迟归咎于 Graph。

### VS Code 对照

- `reference_repo/vscode/src/vs/workbench/contrib/chat/common/tools/languageModelToolsService.ts` 区分 `IToolData`、`registerToolData`、`registerToolImplementation`、`getAllToolsIncludingDisabled` 和 `invokeTool`。
- 对应 `browser/tools/languageModelToolsService.ts` 在真正调用时才可能激活工具实现；读取数据不是启动会话执行的别名。
- `common/languageModels.ts` 区分供应商／模型 metadata 查询与 `sendChatRequest`／`computeTokenLength`。
- 借鉴上述“声明、实现、调用”边界；不移植其 Extension Host、审批、自动激活和扩展市场机制，也不假定模型发现或计数永远是纯本地操作。

## Goals / Non-Goals

**Goals:**

1. 工具目录脱离执行工厂；其余已有独立查询保持原 owner，不为统一形式重拆。
2. 同一业务语义复用原查询 owner；工具管理读取当前定义，模型内发现读取原调用的冻结定义，二者共享声明与规则但不混用快照。动态 factory 保持一个权威来源。
3. 模型无关工具的本地构造错误先于 SDK 初始化暴露；压缩检查与显式模型入口不构造未使用对象。
4. 直接传递原工具集合／定义，不建立统一准备协议；实际取得的资源仍由既有 LifetimeScope 管理。
5. 将依赖规则变成少量行为验收和清晰目录说明，限制未来 Agent 为每个场景添加模式参数、兜底或第二缓存。

**Non-Goals:**

- 不建设通用依赖调度 DAG、全项目 Capability Provider、第二 Graph、万能 `inspect/prepare/execute(mode=...)` 服务或全局能力数据库。
- 不把所有管理接口合并成一个“工作区全部能力”巨型响应，不为未来 UI 预造面板、轮询、连接、SDK 或存储格式。
- 不改变运行目标选择、Gateway 透明代理、插件开关／进程生命周期、权限和确认政策、模型回退、上下文封存、消息队列／团队语义。
- 不实现新的精确 token 统计算法，不根据上下文窗口设置输出上限；不重做日志分页、安装器、GC 或凭据库。
- SDK 启动预热策略独立评估。本变更不得以预热掩盖本地失败依赖 SDK 的结构问题，也不顺手删除或扩大现有预载行为。

## Decisions

### 1. 按实际依赖调整，不建设多阶段框架

即使 LiteLLM 明天消除首次导入耗时，每项必做改动仍须减少真实依赖、重复定义或未使用对象；否则不列为重构任务。下图说明现有操作的依赖，不定义新的阶段协议或统一执行管线。只迁移工具目录、调整必要调用顺序，并收窄压缩／显式模型路径；其他独立查询是受保护的现状。

```text
配置与来源 owner ── 已解析 snapshot／非秘密 metadata
                     │
                     ├─ 配置、Agent／模型列表 → 已有查询 API
                     ├─ 工具定义准备 ────────→ 工具目录／schema API
                     ├─ Skill／指令来源快照 ─→ 已有资源查询和详情
                     ├─ 显式模型发现／测试 ─→ Provider 专用入口
                     └─ 真实执行 admission
                          ↓
                        模型无关工具构造／本地校验
                          ↓
                        所需凭据与模型候选
                          ↓
                        原必要模型／CSM 绑定与中间件／唯一 Graph

checkpoint／projection ─→ 历史、上下文、用量查询
residency／业务 owner ──→ 状态查询（不 acquire）
显式压缩请求 ──────────→ 最小压缩规划依赖 → 原 owner 安排压缩
```

这里的前置校验仅针对模型无关的声明及动态 factory，直接移动原调用即可。依赖模型／CSM 的少量工具保留在原必要绑定位置，目录复用它们的参数声明；不要求全部工具提前构造或全部依赖提前验证。真实 admission、配置版本和凭据代次校验保持；不增加贯穿模块的 mode、Prepared 对象、binder 协议、待绑定队列或阶段状态机。下表中的“允许”仅表示该操作的实际需要，不是要求无条件加载：

| 操作 | 声明／既有快照 | 工具 factory | 选定凭据／SDK／外网 | Thread 执行绑定及图 |
|---|---|---|---|---|
| 配置／Agent／模型列表 | 是 | 否 | 不构造 SDK、不发现模型；只沿既有非秘密可用性规则 | 否 |
| 管理工具目录／精确 schema | 是 | 无独立 schema 时必要的本地构造 | 不解密模型 Key、不构造模型、不启动 MCP | 否 |
| Skill／指令 metadata | 原 accepted revision 或已有有界本地发现 | 否 | 无外部内容下载、不启动插件；Gateway metadata 沿原有界控制面传输 | 否 |
| 上游模型发现 | 是或本次草稿 | 否 | 仅所选 Provider，沿原总期限／分页合同 | 否 |
| 单模型快速测试 | 是或本次草稿 | 否 | 仅所选模型；无 Agent fallback／隐式重试 | 否 |
| 历史／上下文／用量／驻留状态 | 原持久事实／owner snapshot | 否 | 否 | 否 |
| 显式压缩检查 | 原消息与压缩策略 | 否 | 仅原算法必需的选定模型信息；必要时单模型对象，无上游生成 | 不建立 Agent 图；安排动作仍走原 Thread 准入 |
| 正式执行 | 同一代 snapshot | 是，实际绑定实例 | 原候选和回退所需凭据与 SDK | 是，原精确 SessionThread |
| 工具能力测试 | 专用隔离测试输入 | 所测工具 | 仅该测试实际需要 | 不创建正式会话图 |

“配置有效”“schema 可读取”“服务已连接”“工具可执行”“上游实测通过”是不同事实。沿用已有 availability／状态／观察回执表达，不增加一套全局状态机；不能因读取成功就把未测试的模型或未连接插件标为可用。

现有前端／服务接入地图如下，工作区路径均位于 `/api/v1`，经入口 Gateway 显式路由至所选目标；不复制新 API：

| 查看什么 | 复用的实际入口 | 明确独立的动作 |
|---|---|---|
| Agent／模型声明 | `/agents`、`/config`、`/config/sources` 及来源详情 | 配置保存／应用、`/config/llm/discover`、`/config/llm/test` |
| 工具目录／参数 | `/tools`、`/tools/{tool_id}` | `/tools/selection`、原工具测试入口、正式工具调用 |
| Skill／AGENTS 来源 | `/resources/local-skills`、`/resources/skill-sources`、`/resources/gateway-skills`、`/resources/agent-spec` 与 `/resources/navigation` | `/resources/skill-sources/refresh`、编辑来源、运行中的 skill_load／untrack |
| 上下文／历史请求 | `/context` 的原 read／search、原会话历史和 LLM 请求详情 | fork、rewind、手动压缩和实际 model call |
| 运行状态 | `/runtime/status`、原 Thread residency 状态 | drain／cancel／force、Thread 的真实执行准入 |
| 插件／MCP／调试／终端浏览器 | 已有插件 bindings、`/mcp/servers`、`/debug/node/capabilities`／贡献与配置、`/resources` | initialize／连接操作、debug start、资源 create／attach／release |

任务、消息队列、附件、Trace、记忆和资源清理继续归各自 owner。检索本次删除符号的全部直接／间接消费者，迁移真实依赖；不把这些模块或所有 Graph 调用列为全面审计／重构任务。

### 2. 工具目录归现有 catalog，执行服务不再充当查询依赖

实施时沿原 reader 返回 `(ConfigSnapshot, definitions)`，设置服务在同一 `use_snapshot` 下解析默认 Agent、策略和 DTO；HTTP 路径及响应合同不变。目录构造只取得原 factory 实际需要的依赖，不获取 checkpointer、SessionService 或执行 lease。当前无资源 factory 不增临时 scope。

#### 已有读取模块与 CSM 的边界

本变更不是为下列数据新增读取系统。大部分入口本来已经存在：

| 问题 | 直接复用的模块 | 与 CSM 的关系 | 本 change 的处理 |
|---|---|---|---|
| 配置、Agent、模型声明是什么 | ConfigService、AgentService、原配置／模型 API | 配置可生成 instruction producer，但 CSM 不解析配置或构造模型 | 保持读取，收窄实际执行组合的依赖 |
| 有哪些工具、参数是什么 | AgentToolCatalog／AgentToolSettingsService、公开 schema exporter、McpCatalogOwner publication | 管理定义不是 CSM 的 Thread 控制状态；模型内 search_tools 消费原冻结目录，发现后的活动定义继续走既有 canonical／activation 派生 | 解开 catalog 对 AgentExecutionService 的反向依赖，不新增并列 catalog |
| 本地／配置／Gateway 有哪些 Skill 来源 | ResourceNavigation.local_skill_metadata、ConfiguredSkillSources.entries、GatewaySkillSources.metadata | 来源读取在 CSM 上游；CSM 不扫描 Skill 根、不创建 watcher | 直接复用三个入口，不强行统一为一个“有效目录”服务 |
| Skill metadata／正文和遮蔽如何派生 | `business/resource_derivations/skills.py` 的 SkillFacetLoader／SkillCatalogLoader；现有 accepted 捕获与 publication | 已有纯派生层输出 metadata／activation／catalog，CSM 消费其冻结结果 | 保留同一派生规则，不在 UI 或查询层重新实现优先级／frontmatter |
| 本 Thread 追踪了哪个来源、观察／已应用版本是什么 | 唯一 Saver 的 `load_context_source_control_states` 及原控制状态读路径 | CSM 协调状态，持久事实由原 ContextStore 持有；已有 selection 次序也必须保留，不能只比较内容 revision 推断完整 pending | 内部消费者直接用原读端口，不创建第二状态库或为读取重建 CSM |
| 模型实际收到什么、历史上下文是什么 | SessionContextQueryService、SessionContextAssemblyQueryService、原 checkpoint／消息投影读取 | 已 seal 的事实包含 CSM 参与准备后的结果，不靠当前 CSM 重算 | 保持既有查询；前端只补查实的原协议缺口 |
| 当前运行时是否驻留 | ThreadResidencyTracker.snapshot、WorkspaceRuntimeLifecycleService.status | 是运行时 owner 状态，不是 CSM 状态 | 保持读取，不 acquire，不新增查询缓存 |

```text
原配置／文件／Skill／MCP owner
├─ 现有读取模块与 API ─────────────────────→ 当前定义／来源 UI
└─ 原 accepted／语义派生／ResourceRegistry／激活快照
      ↓
    CSM：精确 Thread 的 snapshot／tracked、observation、pending／delta 协调
      ↕ 原控制状态端口
    唯一 Saver／ContextStore：控制事务与 assembly seal
      └─ 现有 context／assembly 读取 ───────→ 已应用／历史上下文 UI
```

CSM 不是全项目读取入口、通用能力目录或文件服务。它不拥有 watcher 或独立持久 writer；`observed` 不等于 `applied`，只有原 seal 事务推进已应用事实。原 `context_sources/tool_definitions.py` 对已发现工具的正文恢复与失效处理继续复用原 activation，不能用管理目录最新定义替换，亦不登记第二个 CSM 控制 owner。

查询的“独立”指不依赖完整 Agent 装配，不表示每类数据必须新增一个 public API。已有 API 不提供的内部控制字段也不因本 change 一律对外开放；确有当前消费者缺口时在原同域读合同补最少字段，不另建 `CSMQueryService`／`CapabilityQueryService` 或 `/capabilities/all`。

#### 查询逻辑的统一入口与现有实现

本次审查的主线是查询复用；校验、schema 与性能规则只是它的约束。统一入口按“同一领域、同一视图语义”确定，不按“都叫 list／read／search”合并，也不要求全部请求经过同一个大服务。

| 查询需求 | 已有查询实现 | 统一方式与本次改动 |
|---|---|---|
| 配置／Provider／Agent 当前声明 | ConfigService snapshot、AgentService.list／get | 设置页与内部消费者复用原配置解析与 Agent 投影，不新建 ModelQueryService／AgentCatalog。当前 Agent.get 复用 list 是已有小目录实现，本次不另建索引或详情缓存 |
| 当前 Agent 工具目录／详情 | AgentToolSettingsService.list／get → AgentToolCatalog → 目前仍依赖执行服务 | 这是必须迁移的查询链：catalog 直接读取共享声明／必要 factory 和原 MCP publication；设置服务保留策略投影，删除执行服务目录入口与 custom 补写。list／get 保持同一目录事实，详情不独立重建全部执行依赖 |
| 模型在本次调用中发现工具 | create_search_tools_tool → 原 search_tool_catalog，输入是原 Saver／activation 恢复的 FrozenToolCatalog | 已有搜索、精确 ID、排名、分页和预算；保留该查询核心。不得改为调用当前管理目录，也不为 UI 另复制同名工具搜索。未来同视图消费者需要该搜索时直接复用原核心，不在本次新增搜索 API |
| MCP 当前完整目录／连接状态 | McpCatalogOwner.publication_snapshot／原状态接口 | 直接捕获原已发布值，不重新 list_tools、连接或从每个 callable 再导出全目录；不为管理页另建 MCP inventory |
| Skill 来源清单／单文件导航 | ResourceNavigation.local_skill_metadata／navigate、ConfiguredSkillSources.entries、GatewaySkillSources.metadata | 三者的发现范围和来源事实不同，保持现有查询入口；Skill API 与 UI 使用原 DTO。Gateway metadata 的有界请求可以保留，不在新聚合器重扫来源或下载正文 |
| 某代有效 Skill／激活正文 | 原 accepted 输入 → SkillFacetLoader／SkillCatalogLoader；原 generation publication | 优先级、遮蔽和正文派生复用原函数。来源清单不是有效目录；已有消费者确需该视图时才在原域提供投影，不新建长期运行的“有效 Skill 查询实例”或查询 ResourceRegistry |
| 当前来源追踪状态 | 原 Saver.load_context_source_control_states | 直接查询精确 Thread 控制事实，不创建 CSM 来回答，也不拿当前来源内容代替 applied／selection 状态 |
| 会话上下文／搜索／封存请求 | SessionContextQueryService.read_context／search_context；其内部 SessionContextAssemblyQueryService | 保留组合关系。原 freeze_read_context、pagination 与 projection 已是共享读取链，HTTP、工具及内部消费者继续消费它，不再添加一套历史扫描／分页器 |
| 会话信息摘要 | SessionInformationService.get_information | 已组合原会话、执行、Trace 和资源事实；复用原摘要入口，不因“查看全部能力”再造会话信息聚合服务 |
| 工作区搜索与跨目标搜索 | WorkspaceSearchService 的具名 Provider → 原领域 owner；Gateway search 的既有范围／调度服务 | 已有分层合同，继续复用原 Provider、匹配、分页与 Gateway 调度。GatewayContextQueryService 的原传输／inventory 职责保留，不另做联邦 fan-out 或全局业务索引 |
| 运行／驻留状态 | WorkspaceRuntimeLifecycleService.status、ThreadResidencyTracker.snapshot | 继续读取原状态 owner，不 acquire。注意当前 status.loaded_agents 为固定四项，不能视为实际驻留目录；这是查实并登记的现存投影缺口，后续应在原 owner 修正；不据此扩大本次实现范围或新建 Agent 查询库，也不假称已修复 |
| 终端／浏览器资源列表与详情 | 原 TerminalResourceOwner／BrowserResourceOwner；现有 /resources 聚合 | 保留现有 HTTP 对两个 owner 的组合，查询归属、资源状态与生命周期仍由各自 owner 提供；不把资源查询迁入 AgentToolCatalog，不再新建面板专用资源服务 |

这里的“同源”不要求管理目录与模型上下文每一时刻返回相同条目：当前目录、配置候选、原 generation 和已封存调用各有真实版本。重复实现是对同一视图另写来源读取、筛选、派生、匹配、分页或缓存；已有不同视图及无业务规则的 HTTP／工具协议投影不是第二事实源。

#### 查询应统一的规则

1. **同域查询复用原业务入口。** 浏览器经原 API／Gateway 调用，后端模块直接依赖原业务 reader；不要求进程内绕 HTTP，不为每种客户端创建 QueryService，不拿执行服务作查询转发器。只有当前消费者确有需要且已有接口不足时，才在原域补最小方法。
2. **明确范围与视图。** 原 Gateway／workspace／Session／Thread 身份按已有 DTO、URI 和 locator 解析；当前目录、有效状态和已封存历史不能互相冒充。查询无结果只返回原范围无结果，不自动换工作区、扩大范围或切 live；跨目标沿现有显式范围与 Gateway search。
3. **发现、列表、详情、搜索分工明确。** 列表使用原 metadata／summary；详情按稳定 ID 或已有资源 URI 读取所需内容；搜索调用原 owner 的匹配／索引。允许小目录详情复用有界缓存列表，不强制每类都补齐 list／get／search，也不新建全局索引。只有需要引用或重新打开的对象才沿已有资源合同提供 URI。
4. **派生与聚合只保留一份业务规则。** Skill 覆盖、工具策略、上下文投影、搜索匹配继续由原 owner／原派生函数实现；API、前端和模型工具仅做各自协议与展示投影。已有 SessionInformationService、/resources 聚合与 Gateway search 各有实际对象，直接复用，不叠加“全部能力聚合”。
5. **缓存、快照与更新仍归原 owner。** 同次读取捕获所需配置／publication 事实，按原版本复用；不承诺不同 owner 的全局原子快照，不新建全域 revision、长期查询 session 或第二缓存。历史保持原封存版本；前端展示缓存不拥有业务定义，更新继续沿原 owner 与统一 realtime 通道。
6. **读取不借执行激活。** 不因查询获取 Agent lease、创建会话、启动插件或写 checkpoint；原范围内受预算限制的磁盘读取、Gateway metadata HTTP、状态观察允许保留。显式刷新、上游模型发现、推理测试及压缩检查是各自已有动作，不能把它们偷偷塞进普通列表。
7. **边界复用已有预算与失败语义。** 原 owner 管理分页、revision／cursor、limit、字节与总期限；聚合保留来源、部分失败及原续点，不重扫所有数据后另做分页，也不强行统一所有领域的游标格式或响应 DTO。

现有查询约束已散布在 session_context_query、business/search、gateway/search 和各资源 owner 的 AGENTS.md／spec。本 change 将复用规则与调用链写清，不另造全项目查询基类、万能 Repository、Provider 注册中心或 QueryBus。落地时更新受影响的原目录说明，避免给 Agent 留下“以后再统一”的平行入口。

#### 查询接线与迁移判定

```text
浏览器／Workbench ── 原 HTTP API ──┐
后端内部模块 ─────────────────────┼─ 原领域业务 reader／已有聚合
同视图的模型查询工具 ─────────────┘      └─ 原来源 owner／派生／分页

工具设置／详情 ── AgentToolSettingsService ── AgentToolCatalog
                                               ├─ 原共享声明／必要 factory
                                               └─ 原 MCP publication

模型 search_tools ── 原 search_tool_catalog ── 原调用 FrozenToolCatalog
封存上下文查询 ───── 原 context／assembly reader ── 原 Saver 已提交事实
```

图中的原业务 reader 按领域分别存在，不是要新增一个中间服务。模型工具只有在查询相同视图时才使用相同 reader；`search_tools` 保持冻结视图。HTTP 只做原路由、鉴权、DTO 和错误映射，不能复制领域筛选／派生；进程内消费者不为统一入口改走 HTTP。

实施按三类判定，不以“模块多”为由扩大重构：

| 现状 | 本次必须执行的动作 | 完成判据 |
|---|---|---|
| 查询已由原 reader 独立提供 | 直接接入／保留，原查询与搜索组合不变 | 不新增同视图 service、索引、分页器或聚合缓存；未受影响实现无需重写 |
| 查询借完整执行装配取得结果 | 将该读取逻辑移回原查询 owner，迁齐所有消费者并删除旧入口 | 工具 API、详情和内部 reader 均不再依赖执行服务目录方法、目录 mode 或合成身份；查询失败也不会退回旧装配 |
| 同视图在两处读取／补写／派生 | 保留原权威来源及一份规则，第二处改用其结果并删除独有重复逻辑 | custom 不再补写空 schema，原 MCP publication 不再从 callable 重建；不能只将重复实现藏进两个新 wrapper |

当前目录更新后的代表性验收必须同时观察两条已有查询：管理页返回新版本，而先前已封存调用的工具发现仍读取原冻结定义；只有原 cursor 绑定的策略、readiness、查询和预算等输入也未改变时，续页才继续成功。这证明视图边界正确；不能要求它们都返回最新值，也不能要求旧 cursor 在策略变化后仍无条件成功。search_tool_catalog 已把这些输入纳入 snapshot key：失配仍按原 ToolSearchConflict 拒绝，不清空 cursor 自动重搜。冻结定义可读不授予当前执行资格；停用、连接替换与租约失效仍按原使用准入拒绝。查询 owner 的失败沿原错误合同返回，不通过另一 reader、当前活跃 Thread、旧缓存或完整执行工厂伪造成功。原 owner 自己已明确的历史／离线可查询状态不属于失败兜底，仍按原合同展示。

#### 工具目录实际需要迁移的依赖

- `AgentToolCatalog` 成为管理定义读取的唯一业务入口，继续实现现有 `ToolCatalogReader`；在容器中注入工具准备依赖，而非 `runtime_catalog=agent_execution_service`。
- `AgentToolSettingsService` 保留 API DTO、工具选择事务、硬限制和能力联动职责。它在同一配置 snapshot 中完成定义读取与策略投影，不能中途重新读取新配置混合结果。
- 必要共享函数优先放在 `app/agents/` 现有文件内，只负责实际共用的构造／定义导出，不获取整个 AppContainer，不构建模型／CSM／Graph。原容器与 agent_runtime 装配位置直接提供所需依赖，不新增仅转发参数的 runtime 文件或服务；纯 DTO 转换继续放 `services/mapping/tool_catalog.py`。
- 将原 `_tool_catalog_cache` 迁到现有 catalog owner 并移除执行服务中的目录方法与缓存。目录缓存只保存脱离 callable 的定义数据，不缓存临时工具、Thread 对象、scope 或凭据。返回值不能被消费者原地修改后污染后续请求。
- 保持 `/api/v1/tools`、`/{tool_id}`、selection 等现有协议及设置界面；schema 应为真实完整参数。删除现有 catalog 对 custom 工具的第二次补写和空 `{}` 伪定义，不在两层各生成一次。
- 管理目录保留禁用／隐藏工具，按稳定 ID 唯一列出；模型工具面仍按执行／可见性规则投影，扩展仍走固定信封。不能为了目录全面而改变正式请求工具集合或放宽执行资格。

合并 custom 补写时必须同时保留元数据，而不是仅替换 parameters。custom 的 origin、kind、group_id 和 group_name 继续从 ParsedCustomToolSpec 及 metadata_for_custom_tool／catalog_group_for_custom_tool 取得；内置与 MCP 使用各自原受信来源身份。不可让 tools_to_catalog_definitions 的默认 builtin 分类吞掉 custom，亦不可仅按 callable 的任意 metadata 推断来源。配置明确给出的 description 保留原管理展示覆盖；未给出时使用真实工具描述。管理层覆盖不擅自改写已冻结调用的描述。上述定义与分组在原聚合／投影位置一次形成，不保留 _custom_definition 再补一份目录。原分组及描述测试迁移后仍须证明实际 API 返回正确。

选择这一方式是因为已有业务 owner 与读协议足够；保留 execution service 的代理方法会继续形成间接依赖，增加一个全局工具服务又会形成重复 owner。

### 3. schema 与可调用实现共享来源，按真实差异处理

| 来源 | 查询如何取得 schema | 正式执行如何取得实现 |
|---|---|---|
| 已有 Pydantic 参数模型／函数生成定义 | 复用原参数模型和公开 schema 导出，不复制 JSON | 原 factory 使用同一模型／函数声明绑定依赖 |
| 调试工具 | 复用现有 registration 的 `input_model`，统一导出规则 | 原 debugging factory，保持 SessionThread 调用身份 |
| MCP | 原 `McpCatalogOwner` 的一次 publication 中的 `inputSchema`／adapter 定义 | 原 execution handle／封存 dispatch；目录读取不连接／启动服务 |
| 任意配置 factory，schema 只能由对象产生 | 构造真实工具并校验返回类型、名称和公开 schema；实际取得资源才纳入短期 scope | 同一 generation 一次构造后直接使用；独立目录请求与执行各有自己的对象，不跨调用缓存 callable |
| compact／skill_load 等绑定模型或 CSM 的工具 | 从它们唯一的参数声明导出，不建立假 CSM／假模型或 `require_execution_context` 空实现 | 在真实执行绑定阶段用原模型／CSM 注入；相同参数声明用于真实工具 |

少量运行时绑定工具可以按现有 debugging registration 的方式把参数声明与 builder 放在一起；不得给每个工具增加一套手写 schema、resolver 类、Provider 注册中心或 manifest 字段。schema 同时包含名称、描述及来源元数据；MCP 的字典 schema 与本地 Pydantic schema 继续走原公开导出规则。

动态 factory 的必要构造不是兼容兜底，也不等于执行工具。项目内 factory 的构造阶段只能创建对象／闭包及明确受 scope 管理的本地资源，不得调用 LLM、启动终端／浏览器／调试器、连接 MCP 或写会话事实。真实业务 I/O 仍在工具调用 owner 内完成；不新增任意 Python factory 的沙箱承诺。

将 `CustomToolFactoryContext` 的 SessionThread 依赖收敛到现有 `ToolInvocationContext.thread_binding`：目录构造时无绑定，调用时必须取得真实绑定；迁齐 browser、debugging、connections、web、session_history 等消费者，删除重复 `context.session_id` 来源及其相等校验。用于目录构造的其他身份只保留真实 Agent／Workspace；禁止发明 session_id、thread_id 或依赖某个当前活跃会话。调用资格仍由现有 invocation/admission 校验，不增加第二套授权包装器。

模型／CSM 相关工具在原必要位置创建实现；其独立参数声明可先验证，真正依赖绑定对象的检查仍就地执行。模型无关动态工具前移后在一次 generation 中只构造一次，并直接传给原消费者。共享函数返回原定义／工具集合；不新增统一 Prepared 类型、多阶段 binder、待绑定队列、阶段状态机或 factory registry。静态声明、真实动态 factory 和 MCP publication 是已有不同来源，允许来源各自的必要处理；它们不能演变成分别维护的查询／执行两套注册算法或贯穿全链的 mode 分支。

#### 已实现能力与需要收敛的入口（2026-11-22 代码核对）

下表是源码核对结果，不代表对应产品验收已通过。已有实现不再作为新增功能重建；检索其消费者后，只迁移真实耦合或重复规则。

| 能力 | 已有权威入口与事实 | 本次处理 |
|---|---|---|
| 工具声明解析 | `policy/custom_tool_spec.py::parse_custom_tool_specs` 已产出 ParsedCustomToolSpec 并检查重复名；runtime 已将类型传给 custom factory | 保留解析器。同次装配传递已解析结果，不在下游重新解释 raw config；不同候选配置仍由原入口分别校验，不新增跨域解析缓存 |
| 内置／custom 工具构造 | `default_tools.py`、`builtin_tool_registry.py`、`custom_tools.py::build_custom_tools` 已负责原构造、factory 返回类型、名称及隐藏注入字段约束 | 复用这些工厂与检查，不新增另一套 preparation registry 或逐工具包装器 |
| 文件／Todo 中间件工具 | `middleware_assembly.py::build_deep_agent_tool_middleware` 已明确供目录与执行共用，且不需要模型 | 复用现有函数和执行侧中间件实例，不再抽一套平行工具中间件工厂，不为导出 schema 二次构造 |
| 公开 schema 与定义检查 | `model_tool_schema.py` 已有 export_model_tool_json_schema、get_model_tool_schema 与 model_tool_schema_validator，后者有精确内容成功回执的有界 LRU | 继续唯一实现；禁止新增 schema exporter／缓存。自定义工具的结构检查与完整 JSON Schema 检查职责不同，不能误删前者 |
| 正式 Provider ToolSet | `RegisteredToolSetProducer` 已按 generation 冻结 schema，并按 selection revision 复用快照；当前使用 convert_to_openai_tool，而非上述 exporter | 收敛普通可执行工具的公开 parameters 约束来源；Provider 所需格式转换保留原库，不要求工具 schema 与 wire 文本逐字相同；保留现有 wire 包装、排序、选择投影和结构化输出专有流程，不重建 ToolSet producer |
| 策略与依赖 | ToolPolicyResolver、resolve_tool_policy、validate_tool_dependencies 已由配置、开关事务、执行使用 | 同一规则继续复用；三个边界分别检查不同有效工具集合，不把多处调用本身认定为重复实现 |
| MCP 发布与冻结调用 | tool_adapter、McpCatalogOwner 发布、restore_frozen_tool_catalog 已复用 schema validator；后者还校验历史正文／来源／schema 身份 | 管理目录读取原 publication，不为暖查询重导出全目录求键；历史恢复的来源／内容检查保持原 owner，不用 live publication 成功回执替代 |
| 工具调用参数 | validate_model_tool_arguments 已供固定扩展信封使用，随后仍通过 BaseTool.ainvoke；事件观察仅过滤注入字段 | 保留公开参数校验、原生工具验证及结果语义；不绕过 BaseTool 私有执行路径来省一次检查，观察链不再校验／转换同一参数 |
| 工具能力测试 | tool_testing 使用隔离 case／sandbox，并直接别名复用公开 exporter，无第二套参数算法 | 保留独立测试执行职责；别名不是第二事实源，不为消灭所有入口而把测试转接到完整 Agent 或管理目录缓存 |
| 模型／压缩生命周期 | build_model_from_provider 已有独立配置检查；正式候选已登记原 generation LifetimeScope | 压缩改用该单模型入口并负责关闭自己创建的模型；不再实现候选资源托管系统，不把必要单模型构造误称为纯 metadata 读取 |

一次受进程外30秒超时与4GiB数据段限额保护的最小探针（退出码0）发现：相同 StructuredTool 使用 `ConfigDict(extra="forbid")` 时，管理 exporter 返回 `additionalProperties: false`，当前 `convert_to_openai_tool` 的 parameters 缺此字段，二者不相等。该证据证明导出路径存在差异，不代表已完成真实 Provider 请求验收。实现必须在原 ToolSet producer 的冻结边界消费共同的公开参数定义，补核原生实际请求；不能只让管理 API 与另一个 helper 相等。Pydantic 的 forbid／allow／ignore、隐藏参数与 JSON Schema 字典均按原合同保持；不得为同源化擅自开启 strict、修改结构化输出 schema 或增加 Provider 参数。若实际 wire／图能力投影变化，按原 GraphBinding 合同核对，不静默沿用不匹配 selector。

#### 统一校验规则与触发边界，不新增统一校验服务

现有全局约束已在 `interactive-latency-budgets` 规定：暖读不反复全量审计、原 owner 持有可复用验证事实、实际变化与归属等必要检查不省略。本变更细化工具链路的落点，不另起检查平台、ValidationService、验证票据、全局 checked 标记或版本账本。

| 检查对象 | 规则／执行 owner | 何时检查与复用 | 不能省略／不能重复的界限 |
|---|---|---|---|
| 请求 DTO／原始配置形状 | 原 Pydantic、配置 schema 与配置 owner | 外部请求、候选载入／应用时；同次内部流转复用已解析值 | 不为把入口统一而改变原接受值或错误响应；不在每个内部 helper 再解析相同 raw 数据 |
| factory 返回、名称和注入字段 | 原 custom 构造器；注册集合由原 registry／producer 检查唯一性 | 每次实际构造／注册；校验通过的同一工具直接使用 | 不用 dict 按名称覆盖来隐藏冲突；不为每个投影再构造 factory；当前 tools_to_catalog_definitions 对 registered_tools 先建 dict 会吞掉同名项，迁移时在丢失原列表前明确拒绝冲突 |
| schema 方言、结构和本地引用 | model_tool_schema_validator | 新定义发布／导出及历史定义消费，精确内容复用已有有界成功回执 | 不缓存可变 validator 或参数验证结果；缓存未命中仍明确校验，不新增网络引用解析或缓存层 |
| 有效工具集合与依赖 | 原 ToolPolicyResolver／validate_tool_dependencies | 配置候选、开关事务、generation 实际工具集合各自形成时 | 这些输入不等价；不因为配置已检查就删除 selection／执行集合的检查，也不复制规则到 catalog |
| 每次调用参数 | 原工具公开参数校验与 BaseTool 执行 | 每次调用，使用该调用实际绑定的 schema | schema 定义已验证不等于本次参数合法；不得缓存“此工具参数已通过”来跳过后续调用 |
| 身份、准入、连接代次、历史封存 | 各自原 admission／MCP／Saver owner | 每次操作／真实使用点，按原租约与事务界限 | 管理读取成功不能代替当前资格，live 同名定义不能代替历史；只检查本次需要的目标，不附加全目录预检 |
| 事件／日志／前端投影 | 原事件、日志、DTO 与错误交付 owner | 序列化／反序列化保留外部边界校验；内部消费已发布事实 | 不重做业务资格判定、工具参数归一化或历史全量完整性审计；不以减少校验为由去掉原对外响应校验 |

统一的是同域规则的权威实现、触发时机和复用边界，不是把所有检查集中到 `app/core/validation.py`。Gateway 插件包、配置 schema、会话生成器等即使使用同一 jsonschema 库，也有不同方言、来源和业务约束，继续由原 owner 使用第三方库；不能为了复用工具 validator 而让 Gateway 导入 Agent／LangChain。只有相同输入、相同契约和相同使用边界的重复工作才删除；不同集合、不同时间、不同信任边界的检查保留，并计入完整交互预算。

### 4. 直接复用单模型构造，执行组合保持原位

- 保留 ConfigService 的模型稳定引用、有效配置与来源解释；沿 snapshot 读取。配置声明和已知能力 metadata 不通过实例化 SDK 推断，不为查看列表解析全部密钥。
- 继续以 `provider_model.py::build_model_from_provider` 为唯一单模型构造入口，发现／测试复用原 Provider 网络和请求规则。候选列表／配置 metadata 与真实 SDK 实例必须分别表示，不能把 SDK 对象塞进前端 DTO。
- 压缩检查不再调用 `prepare_agent_model`，直接使用现有配置解析与 `build_model_from_provider`。后者保持唯一构造入口；原 `prepare_agent_model`／AgentModelSetup 继续服务执行组合，无需为此拆分结果类型、搬动 instruction producers 或新增“按目的准备模型”层。
- 压缩检查只构造其算法实际使用的选定模型，移除构造后未使用的回退候选和路由；正式执行的 primary／fallback 规则不变。已有 `override_model` 调用使用明确提供的模型，不先构造未使用的配置模型，但仍保留工具和执行归属的必要校验。
- API Key 只在实际需要的模型使用边界解析；凭据 revision 仍参与原 generation key。不要为了强行把凭据放到全部工具之后，再创建前后两套 generation key、重绑 admission 或第二缓存。共享准备函数保证本地工具错误不依赖 SDK；现有取得凭据 revision 的步骤允许保留，慢路径证据须分列。
- 不新增 `build_everything(purpose=...)`、通用模型池或按模型名称猜能力。Provider／Model 参数解析只消费当前权威配置解析器；本 change 与配置继承 change 的共享写入先协调。

### 5. Skill／指令：保护已有边界，不再拆一轮

将来源 owner 已接受的 metadata／正文引用读取，与 `build_workspace_skill_catalog` 中资源登记／发布及 `start_workspace_skill_catalog_publication` 的订阅区分清楚：

1. 来源建立与配置重载：WorkspaceFileResourceRegistry、GatewaySkillSources、ConfiguredSkillSources 沿原规则建立稳定来源、接收 revision，不由前端查询创建另一套 scanner。
2. 管理查询：已发布来源读取 owner 的 accepted 数据；`local-skills` 保留 ResourceNavigation 现有固定根一层、有节点／响应字节／期限上限的本地发现，不要求先创建 Thread 或另建一份 accepted 索引。复用优先级／遮蔽／provenance，返回实际存在的 identity、显示 URI、metadata、revision 或原观察信息，不给本地发现伪造 revision。只查详情时才按原有界正文读取；不启动缺失插件、不自动下载外部正文或扩大搜索范围。Gateway metadata 允许原有界控制面 HTTP，不将“无外部内容下载”误解为禁止跨进程读取。
3. 执行 generation：把同一来源事实发布到该 generation 的 ResourceRegistry，构建真实 Skill catalog，再在 generation scope 注册发布／观察订阅。
4. model call：按原激活、sealed snapshot、tracked／untrack 和安全边界消费，目录最新 revision 不能改写已封存调用。

上述大部分职责已经分开：metadata API 直接使用原 owner；`_accepted_skill_sources` 已是零 I/O、零解析的捕获，`resource_derivations/skills.py` 已有 metadata／正文／遮蔽纯派生。不预设再次抽取或迁移这些代码；只有查实的调用方仍进入执行装配时，才让其直接复用已有函数。查询来源清单不必为了返回一个列表就重新派生完整有效 Skill catalog；确需有效项时复用原派生器，不另写选择规则，不创建查询专用 ResourceRegistry、全局 SkillCatalogService、目录镜像、事件通道或版本账本。

单纯查看不登记新的 context lifecycle，不写 root baseline。若现有资源 API 已能返回所需数据，直接使用；只有已存在消费者有明确缺口时才在同域原查询 API 补字段，不能凭未来需求增加 `/capabilities/all`。

InstructionProducer 已有独立构建函数，保持现状，不列为本次迁移任务；向 CSM 的注册仍在原执行位置发生。配置说明、AGENTS、Skill metadata 与已封存请求的内容不是同一种“预览”；界面必须保留当前声明与历史事实的区别，不能重跑当前拼装冒充历史请求。

“只读查询不创建 CSM”不限制已有显式控制操作。`SessionSkillUntrackService` 已在真实 SessionThread 控制准入后，从原持久端口恢复轻量 CSM 并调用同一 untrack mutation；保留这条无 Graph 路径。不能为满足“CSM 只在执行阶段出现”的措辞把它改成先 acquire Agent，也不能在查询服务另写一套 untrack。

### 6. 上下文读取、压缩检查与执行分开

- 历史、上下文组装快照、用量、Trace 详情与 runtime status，继续读取既有有界 projection／checkpoint／owner snapshot。查询不得调用 runtime acquire、修复历史、重建 graph 或调用模型。
- 本次重点是缩小压缩检查的装配依赖，不改切分算法。现有 `compute_summarization_defaults` 消费 model profile，LangChain 近似计数会按 `_llm_type` 调整参数，故不能简单把模型换成一个 context-window 数字或假 BaseChatModel。
- 最小方案允许压缩检查经唯一单模型构造入口取得必要真实模型对象和原 planning helper；不得准备 Agent 全候选、路由、工具、reactor 或图，也不得发送生成／计数外网请求。只读展示已保存压缩记录不需要该模型。
- 未来若第三方提供独立规划接口，再以同一算法抽出纯策略对象；本次不复制第三方默认值／私有方法、制作假模型或另造 token 算法来追求形式上的 SDK-free。
- HTTP 手动压缩仍经过原 Thread idle／write admission，安排在原 model-call 安全点执行。查询与执行的分离不允许把已有 mutation 改成无准入纯读，亦不把目录查询解释为用户请求压缩。

### 7. 直接调整执行顺序，沿用资源所有权

1. 原执行入口取得 admission、精确 SessionThread、冻结 GraphBinding、配置 snapshot、模型选择、必要凭据 revision 和 generation key；不增加前后两套代次计算。
2. 在原 residency single-flight builder 中，先构造并校验模型无关工具，包括动态 factory 返回类型、名称冲突和公开 schema；保留原工具集合直接供后续使用，不增加“准备后再绑定”的中间对象。
3. 随后沿原执行组合构造模型，在必要位置构造依赖模型／CSM 的工具、发布来源和注册订阅，编译原冻结 selector 的唯一图。不能为强制前置全部检查而拆成通用多阶段协议。
4. 成功只发布到原 generation，后续 Turn 按原 admission 复用；查询不 acquire、不延长驻留。模型相关绑定错误仍在实际绑定处报告，不承诺该类错误在模型初始化前出现。
5. 仅对实际取得资源的构造登记原 scope；提前构造新增的失败窗口必须在模型／图失败或取消时释放，目录导出后释放自己的临时资源，不影响共享 owner 或其他 generation。

无资源的普通工具对象无需 dispose、scope 参数或额外退出状态。需要登记资源的 factory 使用现有 LifetimeScope，不统一新增生命周期 SDK。若已有同步构造在 shielded worker 中运行，继续其安全完成／清理约定，不遗弃晚到结果；不要求把所有 factory 搬到 worker，不新增清理线程、轮询或通用取消框架。主错误与确有发生的清理错误沿原错误合同保留。

### 8. 一致性、失效与并发边界

| 事实／结果 | 权威 owner | 触发／失效边界 | 查询不得新增的工作 |
|---|---|---|---|
| 配置与来源解释 | ConfigService snapshot | 原候选应用／重载 revision | 逐次扫父链文件、二次配置合并、全库审计 |
| 工具定义缓存 | AgentToolCatalog | Agent、配置 revision、已有 run mode、MCP publication 内容／代次；只保留当前有效代 | 缓存 callable、跨工作区缓存、每次重新导出全 MCP schema 来求 cache key |
| 有效工具开关 | ToolSelectionStore | 当前请求一致的 selection snapshot；原事务更新 | 把开关混入永久 schema 副本，或让关闭执行删除管理定义 |
| MCP 定义与可用性 | McpCatalogOwner publication | 原 relist／连接发布 revision | 为读 schema 重新连接、用多个时刻的 tools／generation 拼一份目录 |
| Skill／指令当前来源 | 原 file／Gateway／configured source owner | accepted revision、原配置／来源事件 | 查询全盘重扫、轮询、跨 scope 共用 CSM |
| 模型候选与凭据 | 配置／Gateway vault owner | 原 snapshot、凭据 revision、网络代次 | Key 明文进入目录缓存，因重命名或 UI 焦点改变身份 |
| 驻留 Agent | ThreadResidencyTracker | 原 generation key、失效与 unload 合同 | 目录查询 acquire、新建查询 generation、重编译所有已开 Tab |
| 历史上下文与错误 | 原 checkpoint／projection owner | 原提交 revision | 读最新配置重新生成历史、全量读 Trace 补状态 |

原 ConfigService.publication_lock 已与 McpCatalogOwner.publication_lock 共用同一短期屏障，config_reload 在此屏障内一起提交 active 配置与候选 publication；这不是本次新增全局事务。当前管理查询须在该原屏障内取得匹配的 active 配置与 MCP publication 引用，释放锁后用原 use_snapshot 固定配置，完成构造、导出与策略投影。分别调用 get_snapshot 与 publication_snapshot 但未跨两次读取持有屏障，仍可能夹入一次配置提交，不能宣称一致。

原 active 读取由 ConfigService 持有；当前视图不能把外层长期 pinned 的旧配置与新的 publication 拼接。确需历史视图的调用方走原冻结 reader，不进入当前 catalog。捕获阶段只固定原引用，不在 publication lock 内执行 factory、schema 全量导出、网络、await 或清理；选择 snapshot 继续按原事务取得，不把独立 selection owner 并入新的全局锁。

缓存键直接消费捕获值的原 revision／generation，不另建内容指纹。响应可以基于捕获后已被替换、但内部匹配的旧值完成；不能中途补读新 publication 或仅更新状态字段造成混代。后续查询取得新值，迟到结果不得覆盖新键或将旧代标成最新。配置／选择更新仍沿原生效边界，不让在途模型调用改用最新目录。

目录 schema 缓存与连接状态投影要分开：即使定义 revision 未变，连接失效／权限状态仍从当前捕获的原 owner 状态取得，不能从旧 schema 缓存返回“在线”。AgentToolSettingsService 继续使用已有工作区内串行边界处理选择事务，不把 factory 构造放进 MCP 全局发布锁，也不新建跨工作区全局队列。仅因整理目录而增加 API 的聚合版本、状态账本或事件类型没有必要。

动态 factory 若依赖可变外部声明，只能使用其 owner 已发布且带版本的输入；没有版本不能无限缓存其结果并声称实时。当前配置 factory 仍以配置 revision 为有效性约束，外部变化走原显式刷新／配置发布，不新增 TTL 猜测和后台重建。

### 9. 目录规划与删除清单

以下为现有文件的职责调整，不是必建文件清单；优先复用，不机械一函数一文件：

```text
app/
├─ agents/
│  ├─ custom_tools.py 等已有文件    承载实际共享构造／定义导出；不导入 Graph 工厂
│  ├─ model_tool_schema.py          唯一公开 schema 导出与既有有界校验缓存
│  ├─ skill_catalog.py              现有 accepted 捕获与执行 publication，不重复拆读取
│  ├─ instruction_producers.py      已独立，保持原位，不新增拆分任务
│  └─ agent_factory.py              原执行装配，调整工具／模型调用顺序并删除目录模式
├─ runtime/
│  ├─ agent_runtime.py              执行组合和候选模型准备
│  ├─ provider_model.py             本地配置校验和唯一单模型构造入口
│  ├─ context_compaction.py         压缩检查最小依赖
│  └─ graph_factory.py              仅执行图懒加载，返回类型不再含目录列表
└─ services/
   ├─ business/agent_tool_settings/
   │  ├─ catalog.py                 工具定义唯一查询／缓存 owner
   │  └─ service.py                 DTO、选择事务、策略投影
   ├─ mapping/tool_catalog.py       纯公开定义转换
   └─ orchestration/
      ├─ agent_execution_service.py 只驱动执行，保留 residency／generation
      └─ thread_residency.py        原驻留和 scope owner
```

如果共享函数可以直接由现有文件清晰承载，就不新增对应文件；不增加新目录层级。确需新源码目录时补齐四节 AGENTS.md。修改相关已有 AGENTS.md 的装配说明，避免未来 Agent 按旧规则重新引入目录模式。

该树只列本次相关文件，现有 `business/resource_derivations/skills.py`、来源 owner、context 查询和持久控制端口继续原位使用。只有现有文件无法清晰承载实际共享代码时，才新增小型工具定义文件；不得增加仅注入／转发的 runtime/agent_tools.py。不以目录对称或“查询／准备／绑定”对称作为增文件依据。

必须物理移除：`AgentAssemblyMode`、所有 `assembly_mode` 分支及参数、`tools_inspection_session`／随机 inspection Thread、执行服务的目录缓存和查询转发、目录专用的假执行回调，以及迁齐后的 custom 定义补写逻辑。只在测试中有意义的旧 factory 签名也同步调整；不得为了让旧测试不改而保留兼容参数。

保留：冻结 GraphBinding selector／registry、唯一 graph entry、原 ToolSelectionReader／Store、模型／MCP 调用封存、resource activation、scope、错误报告与必要 schema 校验。旧 selector 不能悄悄映射到新图；先核对实际图骨架／schema／能力投影是否变化。纯拆分不产生无意义 revision；确实变化按原 GraphBinding 合同处理，不基于开发残留新造迁移。

### 10. 验证矩阵与性能终点

优先扩展已有测试与装具，测试行为而非文件名、源码关键词或 helper 是否被重命名。为隔离重依赖，少量新进程测试阻断实际 Provider SDK 重依赖（如 LiteLLM）和模型构造／图编译入口；工具所需 langchain_core／langgraph 类型或协议导入允许保留，不能笼统禁止整个框架包并迫使无关模块重拆。不能在同进程已预载 Provider SDK 后声称证明冷路径独立；零上游请求还须在原网络入口观测，不能仅凭图编译计数推断。

| 场景 | 必须证明 |
|---|---|
| 正常工具列表／详情，模型凭据不可用或 Provider SDK 被阻断 | 完整 schema／禁用项可查看；零模型构造、零 Graph、零虚假 SessionThread，配置／选择投影一致 |
| 内置、debugging、custom、MCP、多种中间件工具 | ID 唯一，schema 与真实调用参数一致；不会漏文件／压缩／Skill 工具；hidden 参数不公开，扩展固定信封不变 |
| 动态 factory 返回 str／名称不符／无效 schema | 原本地原因由真实工具 API 与真实会话发送链路报告，模型 SDK／上游调用计数为零；无脏目录缓存／残留 scope |
| 工具准备成功，模型或 Graph 失败；构造中取消 | 已登记资源释放一次；主／清理失败可见；其他 Thread 不受影响；同步 worker 不晚到写入已发布目录 |
| 两个工作区／两个 Thread 的代表性并发 | 查询不 acquire；执行归属隔离，同代 single-flight；下一 Turn／新代正确更新，MCP 在途仍用冻结定义 |
| 配置发布、MCP relist 与查询交错 | 在原发布点可控交错，响应只能为匹配的旧配置／旧 publication 或新配置／新 publication；重构造不占发布锁，后续请求读新版本 |
| 当前目录更新与冻结工具分页 | 仅 live 目录改变时旧查询仍读冻结定义；策略／readiness 等 cursor 输入改变时按原冲突拒绝，不自动重搜或借新定义执行 |
| custom 目录合并 | parameters 真实完整，配置描述覆盖与原分组保持，origin 仍为 custom；按 origin／group 匹配的设置策略与正式执行一致 |
| 模型发现与测试（仅当共享依赖受影响） | 复用已有独立性证据；改到其依赖时补验所选目标、零隐式 fallback 与不构造 Agent |
| Skill／上下文／状态读取（仅被修改调用链） | 保持原 owner 与无 acquire 边界；未受影响者不重建测试矩阵 |
| 压缩检查 | 选定模型 profile 和原计数／切分结果一致；不构建候选路由／工具／图，不上游生成；安排压缩仍走原 idle／write admission |
| 正式会话成功及 primary 失败后 fallback 成功 | 唯一图、工具身份／公开参数／上下文封存／终态不变；中间 attempt 不污染 Turn failure |

计时沿 `interactive-latency-budgets` 与 `unify-failure-reporting`：服务就绪、本机回环、无外部等待、≤512KiB 的完整普通暖 API p95≤500ms、单次≤1500ms，首次未缓存业务读取≤3000ms；正确页面可操作≤2000ms。快速本地工具错误的真实发送链路从点击至错误 DOM 可见，暖请求 p95≤500ms、单次≤1500ms，首次≤3000ms；消息准入／数据库／锁／Gateway／WS／React 均计入，不能只报工具校验耗时。超过512KiB沿原有界响应合同处理，不新增静默截断参数 schema。

暖 API 和暖失败链各至少20个未筛选样本，记录 nearest-rank p50/p95/max、request_id／原执行关联、字节与数据规模；冷样本使用新进程单列。外部模型等待、构建／进程启动单列，不能用 SDK 预热前后的样本混算收益。不另建六编辑器布局验收、全页面巡检或全模块查询测试；用代表性多工作区／Thread 场景验证本次共享边界。

验证脚本带进程外 timeout／数据段限额，产物按正式 tests 路径或本任务临时目录保留。保留原失败样本和源码指纹；失败先定位消息准入、准备、模型或渲染等实际阶段，不加新生产监控、重试或放宽预算。前端协议／代码确需调整时执行真实 typecheck 和完整 Web build，并经过 Gateway／真实浏览器验证，不能只截静态 HTML。

## Risks / Trade-offs

- [动态 factory 无法仅靠声明读取] → 保留一次必要本地构造，实际取得资源才使用 scope；不用空 schema、虚假可用或另一份手写定义掩盖。
- [无 Thread 目录构造暴露原 factory 对 session 的过早依赖] → 迁齐项目内消费者，把业务身份读取放在真实调用边界，移除伪身份而非增加全局当前会话。
- [将工具提前准备后，模型失败产生新的资源释放窗口] → 所有取得资源纳入原 generation scope；定向覆盖失败／取消／并发释放，不新建清理系统。
- [压缩规则依赖真实模型及第三方私有实现] → 优先缩到单模型与原规划，不复制算法；SDK-free 不是本 change 的验收条件。
- [共享配置改动并发] → 使用其权威 snapshot／解析结果，协调相同文件写入，不在本 change 固化旧继承算法。
- [暖失败剩余耗时来自消息准入而非工厂] → 完整链路验收如实保留失败与分段证据；只修属于本业务链路且已确认的原因，不把未达预算写为完成。

## Migration Plan

按 tasks 的纵向切片迁移。先核对查询入口表并将每条链归入“已有独立查询／依赖执行装配／重复查询”之一；随后将工具目录迁回原 catalog，迁齐 API、详情和内部调用方，物理删除执行服务目录入口、旧缓存和 custom 补写。不保留切换开关或失败时回旧装配的路线。再让执行复用共享构造代码并调整调用顺序，收窄压缩／显式模型路径的未使用对象。Skill／上下文／摘要／状态及搜索独立入口保持原位，仅迁齐本次删除符号的真实调用方。每片集中验证同视图复用与当前／冻结视图区别后推进。

该变更以进程内 API 整理为主，不要求用户导入材料、不修改既有 Home／数据库，也不因重构新增迁移或更改配置格式。生效使用正常完整实例生命周期；未经用户要求不重启固定 dev。回退以恢复对应完整代码提交并正常重启为单位，不保留运行时旧模式开关；如发现必须改变持久合同，先按原 owner 明确来源与版本边界，不能顺手写开发环境自动修复。


## 实施证据与未通过项（2026-11-22）

本次按查询入口表完成了三类判定：配置／Skill／上下文／摘要／状态和既有 WorkspaceSearch、Gateway 调度保持原 reader；工具管理从执行装配迁回原 catalog；custom 二次补写、MCP callable 二次导出与执行服务目录缓存已删除。只新增一个明确区分 active 与 pinned 的配置读取方法，没有新增 QueryService、Prepared／binder、查询分页器或长期运行 owner。目录中的普通声明不持资源；执行模型沿原 generation scope，压缩检查只关闭自己创建的模型。复杂度复审保留了不同视图、准入、当前连接代次和逐次参数检查，没有为本次拆分增加生命周期协议或兼容分支。最后移除设置服务的纯转发方法，`service-final-13.log` 的14项服务测试通过。

基线沿本设计前述旧原件及原有装具：原目录 custom 参数可能为空，正式普通 ToolSet 的 extra 策略与公开 exporter 不一致；压缩分区与原 planning helper 对照验证。旧冷失败5378ms只是一次诊断样本，不与本次不同进程／源码环境计算百分比收益。本次没有为收集旧基线重新执行旧 Graph。

证据根：`out/tests/temp/2026/11/22/decouple-agent-capability-preparation/artifacts/`。记录命令、退出码与源码指纹的是 `validation-receipts.json`、各原始 `.log` 和 `run-*/source-fingerprints.json`。Workbench 复用了原已构建的 dist，记录了 main.js 指纹；本次未修改前端／公开 DTO，也没有声称重新构建当前共享前端。

- `run-2/capability-catalog-result.json`：真实 Gateway＋隔离工作区＋Workbench。目录66项、99461字节；冷851.4ms。列表20个未筛选暖样本 nearest-rank p50／p95／max＝28.4／33.3／40.3ms；详情26.8／33.8／35.6ms；工具面板可操作363ms。请求体 request_id 与响应头一致。独立 Backend 阻断 LiteLLM、模型构造及图编译，目录成功；没有执行工厂兜底。
- `run-4/capability-normal-result.json`：两轮真实 Composer 发送，经 Gateway 和模型回放完成 read_file 工具回路；这是集成回放，不是外部 Provider 验收。原 phase 证据确认只编译一次图。`wire-schema-check.json` 核对4份实际模型请求，除固定信封外的普通工具 parameters 均与真实目录完全一致，extra=forbid 保留，隐藏 runtime 未公开；extra=allow 与字典 schema 另由定向单测覆盖。
- `unit-final-7.log`：94通过、4失败。当前目录／配对配置、只读缓存、发布交错、完整 custom／debugging／MCP 参数、压缩分区／清理／取消、override 及工具注册等通过。4失败均为原 ContextStore 测试的“只解析一次路径”计数断言，实际多解析4次；本次仅删除该文件旧 inspection 示例并重命名测试身份，未修改读取算法，不放宽断言。
- `integration-discovery-6.log`：50通过、8失败；冻结目录、分页／策略冲突及上下文恢复通过。8失败在原日志异常记录／diagnostic_sink_error 断言，与共享工作树的错误上报改动不一致；未修改该错误链或测试预期。前批 `unit-1.log` 的3个压缩日志异常断言及 `unit-tools-3.log` 的36个 debugging 日志断言同属该未解决范围，不能声称整套全绿。
- `integration-runtime-focused-9.log`：6通过，覆盖真实主 Thread 两轮复用与子 Thread 隔离、编译失败 reactor／scope 清理和重试，以及 primary HTTP／流失败后的成功回退、未闭合工具组拒绝。此前扩大批次在100秒外部期限退出124（`integration-runtime-8.log`），没有把已输出的8个点当作完整通过；随后缩到这6个有判别力的入口。
- `targeted-final-10.log`：管理目录已取得新 publication，而原封存 search_tools 仍返回原定义；其它输入不变的旧 cursor 继续原分页，测试通过。额外 override 断言最初误读 positional model，修正后 `unit-residency-11.log` 的10项通过，未修改产品逻辑或放宽行为。
- `static-final-12.log`／`static-post-review-14.log`：本次产品与测试文件 Ruff 通过；没有 Python 类型检查器运行证据。旧目录 mode、伪身份、执行服务目录缓存及其测试引用已清除，相关已有 AGENTS 已更新。

**5.2仍未通过：** `run-4/capability-invalid-result.json` 在阻断 SDK／模型／Graph 的新进程中采集首次2353.9ms及20个暖样本，p50／p95／max＝1255.5／1389.3／1427.6ms，暖p95超过500ms。独立原生时钟诊断 `run-5-native/` 首次1701.6ms，暖1067.7／1453.6／1456.8ms，同样未达标，页面无未捕获异常。测量从真实点击事件到新 Turn 原错误 DOM 出现，未以 HTTP 接受或旧错误替代。每份样本保留 request_id、job_id、turn_id；没有筛选、重试取最好样本或调整预算。

`run-5-native/backend-stages.jsonl`／`stage-statistics.json` 显示消息 HTTP 准入暖中位470.1ms，runtime acquire中位212.1ms（包含 skill约6.0ms、assemble约183.3ms，不能相加）；完整错误可见中位1067.7ms。剩余开销已包含消息准入、执行前准备和错误提交／显示，不能宣称均来自工具 factory 或模型。该独立诊断只加临时计时，没有改产品语义。后续最有价值的检查是针对消息准入与执行前后提交做一次完整调用剖析；未取证前不删除持久化或归属检查，也不在本 change 顺手重做整个消息／ContextStore owner。此项保持未完成。

### 实施后审查修复（2026-11-22）

- 本地声明检查原先遗漏压缩工具与运行中间件工具，且文件、Todo、Skill 的隐藏过滤可能先移除冲突项，导致管理读取与执行装配拒绝冲突的时机不一致。现在工具中间件先提供完整声明，执行前检查包含压缩工具的唯一声明及启用的运行中间件，之后沿原规则隐藏工具与省略空中间件。没有新增注册表、模式、兼容路径或缓存。
- 模型构造完成后立即把 `aclose` 登记到原 generation scope，再解析候选能力，补齐候选绑定失败时的释放窗口；清理执行服务中已失效的合成目录注释。
- 本轮原件在上述证据根的 `review-1/`：`unit-fixed.log` 的49项通过，覆盖六类内置工具的可见／隐藏重名、运行中间件无效 schema／重名、隐藏行为、目录与模型池释放；`integration-fixed-path.log` 的6项通过（58.40秒），覆盖主 Thread 两轮复用与子 Thread 隔离、编译失败释放及模型回退。`ruff-final.log` 为本轮静态检查；末轮目录简化另以 `catalog-final.log` 复验。命令、实际退出码及源码指纹保存在 `review-receipts.json`。保留了初次检查发现的可选 `tools` 属性访问错误和一次测试路径错误的原日志，未覆盖为成功输出。
- 本轮没有改前端、公开 DTO、消息准入或持久化算法，未重跑全量浏览器／20样本性能测量；原有5.2延迟未达标项继续保留，不能用本轮定向测试通过替代。

### 第二轮实施后审查（2026-11-22）

- 工具设置的默认 Agent、缺失工具错误与策略投影统一使用 catalog 返回的配对 snapshot，修复外层旧 pinned 配置导致的身份错配。提交沿用原 publication_lock 核对 revision；配置已变化则明确拒绝，不按旧策略写入。factory／schema 错误仍是原查询错误，不伪装为选择请求错误。详情仅投影命中定义；提交后只重读持久选择并投影变更项，不再读取或构造第二次目录，循环外取得 override 字典，避免逐项复制。
- 压缩预览固定配置后直接调用已有 `resolve_llm_model`，只准备并展开首选模型的凭据；工作线程消费已经解析的模型与原 runtime 配置，不在只含首选密钥的 scope 中重新展开全体候选。保持原 profile、分区算法与 scope 清理，不新增单模型配置 API、Prepared 对象或回退分支。
- `review-2/reproduce.log` 的三个工具设置复现与 `compaction-reproduce.log` 的两个回退凭据复现均在修复前失败。修复后 `service-fixed.log` 的24项通过；扩大批次 `unit-final.log` 为75通过、12失败，工具设置、压缩和工具 API 相关项通过，12失败均为未经过本轮修改链路的 context read／search 错误文案旧断言，未放宽预期。`ruff-final.log` 与差异检查通过。
- 正式两项集成测试在业务开始前被测试端口分配器阻断：integration／e2e 共693文件超过691个端口块容量，见 `review-2/integration.log`；未修改无关端口体系。随后显式租用独立端口，以生产 app、真实 Gateway 和隔离工作区运行 Chromium：`review-2-browser-2/selection-result.json` 记录新建会话、真实工具开关关闭／开启、恢复模型可见状态和 list／detail 回读一致；请求体与响应头 request_id 一致，返回仅包含变更项，页面无未捕获异常。Backend 阻断模型构造、LiteLLM 与图编译，此路径仍成功。复用现有 Workbench dist 并记录资源指纹，不代表当前共享前端重新构建或插件能力验收。
- 浏览器原件保留两次脚本问题：首次缺少仓库 PYTHONPATH，第二次误用另一管理入口的 Agent 选择框定位器；修正验证脚本后通过，没有修改产品交互。隔离 Home 未安装配置引用的 `boxteam.developer-tools@0.1.1`，插件初始化因此返回500；关闭时另记录文件监听子进程退出错误。这两项不经过本轮工具设置／压缩链路，未作为成功项；进程租约收尾后两服务端口均无监听，见 `ports-after-close.json`。
- 完整命令、退出码、源码指纹及未验证范围见 `review-2/review-receipts.json`；用户可查看的截图与 JSON 保留在 `review-2-browser-2/`，失败原件保留在 `review-2-browser/`。本轮未改消息准入或执行错误提交链，5.2仍未完成，未重复20样本性能测试，也未同步 dev、重启用户服务或提交代码。

### 第三轮附带问题修复（2026-11-22）

- 按用户要求处理前述附带故障，暂不处理点击发送至错误可见的延迟；5.2 保留未完成及原预算。
- 测试端口块由92压缩到64，容量从691文件增至995；迁齐高偏移、联邦隧道池及其预留声明，保留原池范围、文件互斥与冲突拒绝，不另建动态分配器。没有缩减各场景的隧道数量；端口池完整性检查通过。
- 所有平台的 Backend 统一绑定已有认证 HTTP shutdown，保留 PID／generation 校验、退出核实和超时强制回收。修复 POSIX 整组 SIGTERM 提前杀死文件监听子进程的问题，不吞 `-15` 或修改监听 owner。现有生命周期测试改为从真实绑定入口验证新建／接管进程仅请求一次、再等待退出。
- API、debugging 和工具发现的旧断言迁到统一失败合同：真实安全原因保留，日志仅有界格式化一次，不再要求 raw exc_info；保持状态码、request_id、工具结果配对、原异常、冻结数据及故障修复后的读取验证。共享日志故障装具从已移除的 exc_info 身份判定改为显式启用后的 ERROR 写入失败，确保真实磁盘故障仍被注入。另修复压缩摘要覆盖已提取原因的问题，工具 callback 和活动投影也直接复用同一错误出口，删除原仅含类型的活动摘要函数。
- ContextStore 的4项失败来自测试统计缓存包装方法调用，未发现重复 locator 磁盘读取。测试改为观察真实控制库打开，继续证明每次读取仅核验一次、下一次重查，以及目录缺失／symlink 必须拒绝；未新增缓存或修改存储行为。
- `review-3/unit-final.log`：201通过；`activity-final.log`：67通过；活动消费迁齐后 `activity-compaction-final.log`：59通过；共享诊断装具扩大检查中其余42项通过，旧 callback 断言的12失败已由上述最终活动检查覆盖。`search-final.log`：22项工具发现集成通过。Ruff、差异检查及 OpenSpec strict 校验通过；没有修改前端／公开协议，未重新构建共享前端。
- `review-3-browser/selection-result.json`：使用正式构建链生成的完整开发工具包，沿已有复制入口放入隔离 Home，再启动生产 Backend、真实 Gateway 和 Workbench。terminal／browser／debugging 及工作区插件配置全部 ready，新建会话、工具开关及列表／详情回读通过，页面未捕获异常及 API 500均为零；Backend 仍阻断模型构造和 Graph 编译。停机日志无 ERROR，文件监听 `-15` 不再出现，两租用端口均释放。仅补齐测试前置条件，没有静默取消默认插件。正式终端验收中的浏览器子进程另以退出码0完成六终端显示、字节连接、重载及真实60秒对账场景；不能把该子流程当作整个集成批次通过。
- 未通过项如实保留：最初集成批次总期限240秒退出124，原件不覆盖；其中多工作区路由定向检查证实40289由既有 `bun.exe` 进程占用，未杀进程或改变用户服务。扩大运行重连模块为122通过／83失败；隔离诊断进程只恢复修改前的 shutdown 绑定函数，对照为121通过／84失败，失败集合没有新增，原 auxiliary 退出恢复场景反而通过。余下既存失败包含旧错误断言、未 await 的测试调用和原恢复断言；未借本轮停机修复扩展为整个重连模块重写。对照详情见 `review-3/reconnect-comparison.json`。
- 准确命令、退出码、源码与产物指纹见 `review-3/review-receipts.json`。复杂度复审没有新增产品 wrapper、兼容分支、锁、缓存或持久化机制；仅复用既有关闭／错误出口，保留其他任务的暂存内容，未同步 dev、重启用户服务或提交。
