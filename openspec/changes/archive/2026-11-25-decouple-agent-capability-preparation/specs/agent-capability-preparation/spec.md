## Purpose

规定各领域同视图查询复用原业务 reader、派生与分页，保留当前定义与冻结历史的独立事实；将工具目录从执行工厂解耦并删除重复补写。同步调整模型无关工具的本地校验顺序，移除压缩／显式模型路径未使用对象，保持唯一执行链路与交互预算。

## ADDED Requirements

### Requirement: 重构必须减少实际依赖而非增加准备框架

每项必做改动 SHALL 在 LiteLLM 消除首次导入耗时后仍能减少真实依赖、重复定义或未使用对象。系统 SHALL 优先调整现有函数调用顺序、共享原参数声明和工具集合，MUST NOT 为本变更建立统一 Prepared 类型、待绑定队列、阶段状态机、通用 binder／factory registry 或仅转发参数的 runtime 层。已有独立 Skill 派生、指令构建、上下文和状态查询 SHALL 保持原 owner，不因形式统一再次拆分；本次删除入口的直接／间接消费者仍须迁齐。

#### Scenario: 工具实现依赖模型或 CSM
- **WHEN** 少量工具只能在真实模型或 CSM 已存在时构造
- **THEN** 目录复用其唯一参数声明，执行保留原必要绑定位置；不为把所有工具提前构造而增加多阶段协议

#### Scenario: LiteLLM 首次导入不再耗时
- **WHEN** 上游已消除初始化延迟
- **THEN** 独立工具目录、同源 schema、删除伪身份和去掉未使用对象仍有价值；不能仅凭旧基准保留额外准备框架

### Requirement: 能力查询不得隐式取得执行运行时

系统 SHALL 复用已有配置、Agent／模型定义、工具目录、Skill／指令来源、历史上下文和运行状态业务读取入口，本次解开工具目录的完整 Agent 装配依赖，其余保持已有独立读取边界；独立性 MUST NOT 被解释为逐类别新增服务／HTTP API 或并列查询入口。查询 SHALL 使用明确的目标／工作区／对象归属，不依赖导航激活状态或其他编辑器的“当前 Thread”。普通读取 MUST NOT 因查询而创建会话／Thread、取得执行准入、加载驻留图、延长 Thread 驻留或启动工具进程。

#### Scenario: 冷 Thread 仍能查看能力和历史
- **WHEN** 用户打开一个没有驻留运行时的 Thread 的历史、上下文或相关设置
- **THEN** 返回原 owner 的有界事实，Thread 仍为 cold，未编译执行图或产生 checkpoint；没有执行历史时明确显示无记录，不生成假的记录

#### Scenario: 多编辑器并存时查询其他工作区
- **WHEN** 多个 Thread 编辑器分别属于两个工作区，用户查询明确选择的工作区的工具目录
- **THEN** 返回所选工作区和 Agent 的定义与策略，不借用任一编辑器的会话身份，不改变当前执行租约或编辑器焦点

### Requirement: 同一查询视图必须复用已有业务读取链路

本变更涉及的查询 SHALL 按同一领域及同一视图语义复用原业务 reader、派生函数、分页器和既有聚合；浏览器经原 API／Gateway，进程内消费者直接调用原业务端口。MUST NOT 为客户端、模型工具或新设置页各建一套同视图读取、匹配、派生、缓存与事实源，不得新增全项目 QueryService／QueryBus／万能 Repository。已有查询满足需求时 SHALL 保留原实现，仅迁移实际反向执行依赖或重复规则；接口不足时在原 owner 补当前消费者必需方法，不机械要求每类补齐 list／get／search。查询迁移 SHALL 全量迁齐原直接／间接消费者后物理删除旧读取链；查询失败 MUST NOT 改走完整执行装配、另一范围或另建查询实现取得替代结果。原 owner 已明确允许的历史／离线状态仍沿原合同返回，不被视为重复查询。

#### Scenario: 多种消费者读取相同当前工具视图
- **WHEN** 设置列表、工具详情或一个内部模块需要指定工作区／Agent 的当前工具定义
- **THEN** 复用原 catalog 及相应设置投影，不通过 AgentExecutionService 或新建并列 reader，也不为详情另构造完整 Agent

#### Scenario: 已有上下文与资源查询
- **WHEN** 本次修改的消费者需要上下文页、会话信息摘要或终端／浏览器资源列表
- **THEN** 分别使用原 SessionContextQueryService／分页链、SessionInformationService 和原资源 owner 聚合，不再写历史扫描、摘要组装或面板资源存储

#### Scenario: 迁移后的查询 owner 失败
- **WHEN** 已迁入原 catalog 的工具查询发生实际构造或定义读取失败
- **THEN** 原失败经同一查询入口返回，不调用旧执行目录方法、不新建 inspection Thread，也不切换到另一 Agent 或工作区以返回成功

### Requirement: 当前视图与冻结视图必须分别沿原来源查询

管理工具目录 SHALL 读取当前配置和原 publication，模型内 search_tools SHALL 继续使用该调用所属 FrozenToolCatalog 与原 search_tool_catalog。Skill 来源发现、当前有效 Skill、Thread 追踪状态和历史封存内容 SHALL 保留各自事实和版本语义，共享原声明及派生规则，但不得强行统一到最新管理目录或新的查询实例。不同语义的多个查询入口 MUST NOT 被机械认定为双轨；相同视图重复读取／派生才需要收敛。

#### Scenario: 工具目录更新后读取旧调用
- **WHEN** 当前管理目录已有新参数定义而一个模型调用已封存旧目录
- **THEN** 设置展示当前定义，原调用的发现／精确读取仍使用旧冻结目录；原 cursor 的其他绑定输入未变时继续分页，不将 search_tools 改接当前 catalog。策略、readiness、查询或预算等绑定输入改变时仍按原冲突拒绝，不自动清除 cursor 重搜；旧定义可读不代表当前工具仍可执行

#### Scenario: Skill 来源存在但未被选入有效目录
- **WHEN** 一个来源中的同名 Skill 被较高优先级来源覆盖
- **THEN** 来源查询仍可显示其真实归属，有效目录按原 SkillCatalogLoader 派生，Thread 状态按原控制事实读取；不能用一份合并列表冒充三类查询

#### Scenario: 冻结目录未变但工具被关闭
- **WHEN** 一次工具搜索后，当前工具选择关闭了某项能力，而消费者使用旧 cursor 续页或申请尚未准入的新调用
- **THEN** 分页沿原 policy-bound cursor 合同明确报告冲突，执行按当前原准入拒绝；不为保持冻结读取而放宽策略，也不改用 live 同名工具接管；已经准入的在途调用仍按原租约与撤销合同结算

### Requirement: 查询范围与读取成本必须沿原 owner 合同

查询 SHALL 使用明确的目标、工作区与对象身份，保留原稳定 ID、URI、metadata／详情、分页、revision／cursor 和预算语义。无结果 MUST NOT 自动扩大范围、改查 live 或启动执行资源。工作区搜索与跨目标搜索 SHALL 复用原 WorkspaceSearch Provider 和 Gateway 搜索调度，不新建匹配器、全局索引或前端／上下文专用联邦引擎。原 owner 的更新、缓存与失败事实 SHALL 保持权威，不新增跨 owner 全局原子快照或聚合版本；前端可保留原展示镜像。只有需要引用／重新打开的对象才按原资源合同提供 URI。

#### Scenario: 用户明确搜索多个工作区
- **WHEN** 已有消费者明确选择多个工作区进行搜索
- **THEN** 原 Gateway 搜索冻结显式范围并调度原工作区 Provider，匹配与续点由原领域 owner 提供，不因本次查询解耦重新建设 fan-out 或复制业务正文

#### Scenario: 普通列表不需要正文或执行
- **WHEN** 客户端仅请求当前来源 metadata 或现有状态摘要
- **THEN** 使用原有界读取或 owner snapshot，不自动下载全部正文、acquire Agent、建立查询 CSM 或启动插件；已有受预算约束的磁盘／控制面读取允许保留

### Requirement: 来源定义与 Thread 上下文状态必须沿原权威读取

当前配置／工具／Skill 定义 SHALL 读取原来源 owner；精确 Thread 的 tracking、观察版本、已应用版本和选择次序 SHALL 读取原持久控制状态；实际发送过的上下文 SHALL 读取原封存请求／历史投影。CSM SHALL 保持原来源生命周期协调职责，不承担全项目目录、文件发现或第二持久化 owner。读取 MUST NOT 因查看状态构造新的 CSM、重算待提交 delta、注册来源或推进 applied；内存中尚未被原 owner 发布的状态不得被查询层猜测为已持久事实。

#### Scenario: Skill 当前版本新于 Thread 已应用版本
- **WHEN** 来源目录已有新版本而某 Thread 尚未封存应用
- **THEN** 来源查询返回当前版本，Thread 状态／历史查询返回原观察或已应用事实，二者不互相覆盖；不能因内容 revision 相同就忽略尚未应用的来源选择次序

#### Scenario: 已发现工具定义有历史冻结来源
- **WHEN** 管理目录已更新而模型上下文中存在通过先前发现调用冻结的定义
- **THEN** 活动定义按原 canonical／activation 合同恢复，不用管理目录最新正文替换，不登记第二个 CSM 控制 owner

#### Scenario: 显式停止 Skill 追踪
- **WHEN** 用户通过已有受信控制入口停止一个真实 Thread 的 Skill 追踪
- **THEN** 原控制准入后仍复用同一 CSM mutation 和原持久端口，允许必要的轻量状态恢复，不 acquire Agent 或编译图，也不在查询层实现另一套修改逻辑

### Requirement: 管理目录必须完整且与执行定义同源

工具管理列表与详情 SHALL 返回已配置且有定义的内置、custom、MCP 和中间件工具，保留禁用项与模型隐藏项，并按稳定工具 ID 唯一表示。名称、参数 SHALL 从权威声明／公开 schema 取得；custom 来源与分组复用 ParsedCustomToolSpec 及原 metadata／group helper，配置明确给出的描述保留原管理覆盖，缺省使用真实工具描述，不将所有非 MCP callable 默认为 builtin。执行参数、模型公开参数及管理 schema SHALL 遵守同一公开参数合同。系统 MUST NOT 用重复手写 schema 或空对象冒充尚未取得的定义，不得把目录完整性解释为工具全部可执行。

#### Scenario: 工具禁用后仍可查看参数
- **WHEN** 用户关闭某工具的执行能力后再次打开工具设置和详情
- **THEN** 工具仍唯一列出，保留真实完整 schema 和来源，执行与模型可见状态按原联动约束返回

#### Scenario: 文件和压缩工具由中间件提供
- **WHEN** 用户查询包含文件操作、压缩和 Skill 加载能力的 Agent 工具目录
- **THEN** 这些工具与普通注册工具一样有完整定义，不因其来自中间件或依赖执行上下文而遗漏，也不因此创建压缩引擎或执行上下文

#### Scenario: 扩展使用固定信封
- **WHEN** 扩展工具未直接对模型公开，用户在管理目录读取其参数
- **THEN** 管理目录可展示扩展真实定义，正式请求仍遵守固定信封和原可见性规则，不因为管理查询而自动注入全部扩展参数

#### Scenario: 删除 custom 补写后读取管理目录
- **WHEN** 一个 custom 工具配置了明确描述，且按 origin 或 group 存在原策略规则
- **THEN** 列表和详情保留配置描述覆盖及原 custom 来源／分组，参数来自真实声明；设置投影仍命中原策略，不因 callable 映射默认成 builtin 而改变开关资格，也不保留第二份 custom 目录

### Requirement: 已有能力必须复用同域唯一入口

工具定义构建 SHALL 复用现有 custom／内置工厂、无模型工具中间件构建函数、公开 schema exporter 及策略解析器，不重建同形注册与构造路径。管理目录、普通可执行工具的正式 ToolSet 和工具测试 SHALL 消费同源公开参数规则，保留原 Provider 包装／排序、隐藏参数、extra 策略及结构化输出专有合同。ToolSet SHALL 在原 generation 注册处冻结，之后按原 selection snapshot 投影；MUST NOT 为了校验一致性在每个模型请求重新导出全部工具或 acquire 管理目录。不同表示形式所需的 Provider 格式转换继续使用原第三方库，不维护第二份参数声明，不强行把全部工具形式改造成 BaseTool。

#### Scenario: 禁止额外字段的本地工具
- **WHEN** 一个本地工具的原参数模型要求 extra=forbid
- **THEN** 管理目录、正式冻结 ToolSet 的公开参数以及真实 Provider 请求均保留禁止额外字段的约束，实际调用按原规则拒绝额外字段；不能仅证明目录 helper 的 schema 正确

#### Scenario: 原共享构造函数已经存在
- **WHEN** 目录和执行需要相同的文件／Todo 工具或 custom 工具
- **THEN** 直接复用原无模型中间件工厂／custom 构造器和其检查，同次 generation 不为 schema 导出重新构造，不另建 preparation registry

### Requirement: 校验必须按对象与使用边界执行

系统 SHALL 沿 interactive-latency-budgets 的原规则组织检查：同域同契约共用原校验实现，同次内部流转复用已解析值；schema 定义的精确内容成功回执复用现有有界缓存。配置候选、选择事务、执行集合的依赖检查各有实际输入，不因共用规则就合并或删除；每次调用参数、当前准入／身份／连接代次以及历史封存身份仍按原 owner 检查。MUST NOT 新增全项目 ValidationService、全局 checked 标志、验证票据／版本账本，也不得缓存参数验证成功来跳过后续调用。事件／日志投影不得重做业务资格或全目录完整性检查，但外部协议的原序列化／反序列化校验仍须保留。不同领域继续使用原 owner 和第三方库，不将 Agent 工具 validator 强加给 Gateway 插件、配置或其他 schema 消费者。

#### Scenario: schema 不变但调用参数改变
- **WHEN** 同一工具的一次合法调用之后收到非法参数
- **THEN** 可复用 schema 定义检查事实，但非法参数仍在本次调用边界失败；之前的合法调用不能成为跳过校验的依据

#### Scenario: 工具选择改变有效集合
- **WHEN** 配置候选曾通过依赖检查，用户后来提交改变有效集合的工具开关
- **THEN** 原选择事务仍使用同一依赖规则检查新集合并原子提交，执行集合仍按原实际输入检查，不在 catalog 复制依赖算法

#### Scenario: 不变目录的暖读取
- **WHEN** publication／配置版本未变时再次读取工具定义
- **THEN** 原 catalog 复用已有定义缓存，返回当前策略和可用性，不为求键重新导出全目录、重建 factory 或扫描全部历史；当前归属及必要边界检查仍保留

### Requirement: 动态工具定义准备不得依赖模型执行

已有独立参数声明的工具 SHALL 直接复用原声明；MCP SHALL 使用原 owner 已发布的定义。仅当参数确由 factory 动态生成时，定义查询 SHALL 允许一次必要的本地工具构造并校验真实返回值。该准备 MUST NOT 构造模型 SDK 实例、解析模型 API Key、发送模型请求、启动终端／浏览器／调试器或建立 MCP 连接。构造行为 SHALL 与真正调用工具区分，不得以执行一次工具来推测参数。

#### Scenario: 动态 factory 返回错误类型
- **WHEN** 配置中的 factory 返回字符串而非工具对象
- **THEN** 工具 API 报告原类型错误和稳定工具身份，不尝试构造模型或调用上游，不写入成功目录缓存

#### Scenario: 模型凭据不可用但工具可读取
- **WHEN** Agent 的配置快照已有效发布，但模型凭据暂不可解析，用户读取工具目录或详情
- **THEN** 工具定义读取不依赖该凭据，正常返回真实 schema；之后正式模型执行仍按原规则报告凭据故障

#### Scenario: MCP 服务未连接
- **WHEN** MCP owner 已有带 revision 的定义且当前连接不可用
- **THEN** 查询按原 owner 的可见性合同返回其定义和真实可用状态，不重连、不启动插件、不把缓存定义等同于在线可执行；若 owner 没有可查询定义则明确报告未知或不可用

### Requirement: 定义查询不得伪造业务身份或可调用上下文

工具与 Skill 定义查询 SHALL 使用真实的 Gateway／Workspace／Agent 身份，且在没有 Thread 时保持无 Thread 绑定。查询 MUST NOT 创建合成 Session、随机 Thread、假模型或假 checkpoint owner，不得把带可执行闭包的工具对象返回给客户端。Thread 专有工具的调用 SHALL 继续要求真实、受信的调用身份，定义可见性不能授予执行资格。

#### Scenario: 查看仅在执行中有意义的工具
- **WHEN** 用户在尚未创建会话时查看 Skill 加载或手动压缩工具参数
- **THEN** schema 可从同一声明取得，业务存储中没有新增 Session／Thread；实际调用时仍须通过原执行准入

#### Scenario: custom 工具稍后被真正调用
- **WHEN** 已查询过定义的浏览器、调试或会话历史工具被某个真实 Thread 调用
- **THEN** 操作属于当次真实 SessionThread，不沿用查询上下文或其他 Thread 的身份

### Requirement: 实际取得的资源必须在原生命周期内释放

目录构造实际取得的临时资源 SHALL 归当前短期 scope，完成定义导出后释放；正式执行资源 SHALL 归原 Thread generation scope。无资源的普通工具对象 MUST NOT 被机械增加 dispose、scope 参数或生命周期状态。中途异常、模型／图构建失败和取消 SHALL 沿原机制释放已取得资源，不得释放其他调用或共享 owner 的资源；已有同步 worker 不得遗弃晚到结果，主错误和清理错误沿原合同报告。本变更 MUST NOT 为全部 factory 新增 worker、取消框架或清理轮询。

#### Scenario: 工具准备后模型失败
- **WHEN** 工具准备成功并登记了本地释放动作，随后模型构造失败
- **THEN** 原失败传递给执行终态，已登记资源只释放一次，没有成功发布的 runtime，也不影响同工作区其他 Thread

#### Scenario: 查询在同步 factory 构造中取消
- **WHEN** 查询取消时同步 factory 尚未返回
- **THEN** 完成原 worker 的清理并丢弃未发布结果，不留下订阅或目录缓存，不新增每工具清理线程或常驻清理轮询

#### Scenario: 清理本身失败
- **WHEN** 目录准备或执行准备失败后释放又产生异常
- **THEN** 主失败与释放失败均可追溯，不将目录或 runtime 标为准备成功，也不让释放错误覆盖主原因

### Requirement: 模型无关工具的本地校验必须先于模型构造

在原真实执行准入和 generation 识别之后，系统 SHALL 将模型无关工具的构造和本地校验放在模型 SDK／图构造之前，包括动态 factory 返回值、名称冲突和公开 schema。该类错误 SHALL 直接进入原错误收尾，零模型构造、零上游请求、零图编译；同次 generation 的动态工具仅构造一次并直接用于执行。独立参数声明可先校验，真正依赖模型／CSM 的绑定及检查 SHALL 保留在原必要位置，不要求该类错误也先于模型构造。配置／身份／凭据代次校验仍有效，不为前置检查新增中间准备协议或重复校验。

#### Scenario: 首次消息命中本地工具配置错误
- **WHEN** 用户在新进程中向一个包含无效动态 factory 的 Agent 发送消息
- **THEN** 错误通过真实 Gateway／事件通道出现在原 Thread，原工厂原因保留，没有模型构造或上游请求，图编译计数为零

#### Scenario: 有效工具进入正式执行
- **WHEN** 原 generation builder 前移模型无关工具构造后继续正式执行
- **THEN** 已构造的动态工具直接复用一次结果，模型／CSM 相关工具在必要位置构造，实际调用仍走原策略、身份和封存校验

### Requirement: 模型声明与单模型操作必须独立于 Agent 装配

模型／Provider 定义、来源和已知能力读取 SHALL 使用配置 owner 的非秘密结果，不实例化模型、不发现上游、不解密所有凭据。显式模型发现或测试 SHALL 仅验证所选连接／模型所需数据，不准备 Agent 工具、Skill、候选路由或图；单模型测试 MUST NOT 自动使用 fallback、换协议或增加输出限制。配置可用、上游目录可读、推理测试通过 SHALL 保持不同事实。

#### Scenario: 无关工具 factory 错误不阻断模型测试
- **WHEN** 用户对一个草稿 Provider 发起模型发现或对一个模型发起快速测试，而 Agent 的某工具 factory 有错误
- **THEN** 操作只沿所选 Provider／Model 的原验证与请求路径完成，无关 factory 不被调用；回执仍注明实际目标、输入版本、协议和观察结果

#### Scenario: 列表读取不代表上游通过
- **WHEN** 用户打开已配置模型列表且没有执行过上游测试
- **THEN** 返回本地配置与已知 metadata，未验证能力保持未知，不因列表成功把连接、流式或工具调用标为实测通过

#### Scenario: 指定模型失败
- **WHEN** 用户明确快速测试一个模型且调用失败
- **THEN** 展示该模型本次失败，不借 Agent 的 fallback 通过，也不准备其他模型或工具

### Requirement: 模型执行组合不得准备未使用对象

正式 Agent 执行 SHALL 保留原 primary／fallback、路由、凭据和封存规则；单模型消费者 SHALL 复用正式构造与请求规则，不额外构建未使用的 Agent 候选、路由及指令。执行显式提供模型实例时 SHALL 使用该实例，不再为最终会被丢弃的配置模型构造 SDK。压缩检查 SHALL 直接复用已有权威配置解析与单模型构造入口；prepare_agent_model／AgentModelSetup 仍服务原执行组合，不为此另设模型准备抽象或重新拆分已有 instruction 构建函数。

#### Scenario: 压缩规划只使用一个模型
- **WHEN** 压缩检查仅依赖当前选定模型的信息
- **THEN** 不构造之后未被使用的 fallback、routing middleware 或 Agent 指令；正式执行的模型回退能力不受影响

#### Scenario: 执行入口显式提供模型
- **WHEN** 受支持的内部执行入口传入明确的模型实例
- **THEN** 直接使用该实例及既有路由约定，不提前构造随后弃用的配置模型，工具和 Thread 必要校验仍执行

### Requirement: Skill 与指令读取不得产生执行订阅

Skill／指令查询 SHALL 对已发布来源读取原 owner 的 accepted revision，并保留已有固定根范围、有界的本地来源发现；两者复用原优先级、遮蔽、身份和 provenance 规则，不给尚无版本的本地发现伪造 revision。metadata 与按需正文读取 SHALL 使用原有界资源协议，不创建 CSM、不登记新的 Thread lifecycle、不新增订阅、不自动下载外部正文或启动插件；原 Gateway metadata 的有界控制面传输允许保留。来源建立／刷新、执行资源发布、Thread 激活与订阅 SHALL 沿原 owner 和显式生命周期发生，不因查看目录而提前执行。

#### Scenario: 同名 Skill 有多层来源
- **WHEN** 用户查看同时存在工作区、Gateway、网络和内置来源的同名 Skill
- **THEN** 返回原解析规则的有效项及来源归属，保持覆盖事实，未创建 Thread 或观察订阅

#### Scenario: 未进入 Agent 的本地 Skill 目录
- **WHEN** 用户在尚无 Thread 的工作区查看本地 Skill 来源
- **THEN** 沿原固定根与节点／字节／期限上限发现本地条目，不要求先构建运行时、不递归全盘扫描，也不为此新建索引或伪造发布版本

#### Scenario: 明确查看 Gateway Skill metadata
- **WHEN** 客户端请求所选 Gateway 的 Skill 元数据需要原控制面 HTTP
- **THEN** 沿原目标、鉴权和总期限返回 metadata，不下载 Skill 外部正文、不启动插件或建立新的业务长连接

#### Scenario: 来源当前不可用
- **WHEN** 某个配置的 Skill 来源尚无 accepted 内容或当前被原 owner 标为不可用
- **THEN** 沿原资源查询合同展示不可用原因，不临时扫描其他目录、扩大范围或以空列表冒充正常

#### Scenario: 目录更新不改写封存调用
- **WHEN** 一次模型调用已封存旧 Skill／扩展目录，而管理查询随后得到新 revision
- **THEN** 查询展示新观察，旧调用继续消费原封存内容；下次激活按原安全边界更新

### Requirement: 上下文与压缩检查必须保持原计算和准入语义

历史上下文、组装快照、用量和压缩记录读取 SHALL 使用原已提交事实，不用当前配置重新装配来冒充历史请求。压缩检查 SHALL 使用与正式执行同源的模型 profile、计数、切分及安全规则，不构建 Agent 工具、候选路由或图，不发送上游生成请求。必要单模型依赖允许保留，不以假模型或复制默认值改变结果；调用方按原所有权关闭为检查创建的模型，generation 已托管的模型不由查询关闭。安排压缩 SHALL 继续通过原 Thread idle／write admission 和原 model-call 安全点。

#### Scenario: 配置模型已改变但查询旧请求
- **WHEN** 用户查看一个由旧模型执行并封存的上下文或用量
- **THEN** 展示原记录及原模型身份，不用新模型估算替换已记录事实，不 acquire 当前运行时

#### Scenario: 压缩检查需要模型类型调整计数
- **WHEN** 原算法会按模型类型／profile 调整估算和保留窗口
- **THEN** 拆分后分区与原规则一致，不能用统一字符比例或猜测窗口替换；准备范围只包含该规划所需依赖

#### Scenario: 忙碌 Thread 上请求手动压缩
- **WHEN** 用户通过原入口安排压缩而 Thread 有执行活动
- **THEN** 遵守原冲突／空闲调度约定，不因压缩检查已独立就绕过准入直接修改 checkpoint

### Requirement: 缓存与查询必须使用各自权威版本

当前工具定义读取 SHALL 在已有 ConfigService／McpCatalogOwner 共用的 publication_lock 下固定匹配的 active 配置 snapshot 与 MCP publication 引用，释放锁后使用原 snapshot 绑定完成读取。MUST NOT 将两次独立捕获或外层 pinned 旧配置与当前 publication 当作一致输入；factory、schema 导出、网络及清理不得进入该发布锁。策略投影继续使用本次原选择 snapshot，不新增跨 owner 全局事务；配置／目录捕获完成后可以返回匹配的旧值，但不得中途拼入新代字段。定义缓存 SHALL 由原 catalog owner 持有，只存可序列化数据，按实际 Agent／配置／publication 版本失效并有界保留；凭据、callable、Graph 和生命周期对象 MUST NOT 进入该缓存。版本判定 SHALL 复用已有 owner 事实，不在每次暖读重算全量 schema 指纹或读取所有来源。

#### Scenario: 当前目录读取跨越配置候选提交
- **WHEN** 查询捕获输入时，原配置 owner 正在提交新 active 配置及 MCP publication
- **THEN** 查询使用同一原发布屏障下匹配的旧组合或新组合，随后构造不占发布锁；不能返回旧配置配新目录，下一请求能取得新组合，不新增重试／版本账本

#### Scenario: 查询期间 MCP relist
- **WHEN** 一次目录读取捕获 publication 后 MCP owner 发布新目录
- **THEN** 当次响应内部一致，后续请求读取新目录，不混合旧 tools 与新 revision，不启动第二轮隐式重试

#### Scenario: 工具选择改变但 schema 不变
- **WHEN** 用户改变某工具执行或模型可见开关
- **THEN** 下一次查询／执行按原生效边界采用最新选择，完整定义仍可复用，不生成第二份 schema 或为所有编辑器重编译图

#### Scenario: 定义未变化但连接失效
- **WHEN** MCP schema 没有变化而原连接 owner 已发布不可用状态
- **THEN** 目录按原可见性合同返回定义与当前真实状态，不从 schema 缓存沿用旧在线判断，也不为更新状态重新连接

#### Scenario: 调用方修改取得的定义
- **WHEN** 内部消费者修改本次读取结果中的嵌套参数对象
- **THEN** 之后的查询及执行定义不受污染，不从可变共享缓存传播该修改

### Requirement: 查询与正式执行必须复用现有传输和错误合同

工作区能力查询 SHALL 沿现有业务 API 经过 Gateway 透明路由；前端消费原 DTO，后端模块经同一业务读取入口消费，不跨层操作 Python 工具或 Graph 对象。MCP／Skill／运行状态更新 SHALL 复用原 owner 与统一实时入口，不新增常驻业务连接或轮询。失败 SHALL 保留原产生层、原因、稳定归属及当次 request_id，遵守既有脱敏／有界诊断，不用空成功或伪造默认值替代。

#### Scenario: 远端 Gateway 上的工具目录失败
- **WHEN** 明确选定的远端工作区在动态 schema 准备时失败
- **THEN** 原原因与同一 request_id 经入口 Gateway 交付，不被替换为模型不可用或前端网络错误，不自动改查本地工作区

#### Scenario: 其他后端模块读取定义
- **WHEN** 一个不执行 Agent 的后端业务模块需要工具参数或来源
- **THEN** 可调用独立业务读取入口得到与 HTTP 相同来源的定义，无需取得 AgentExecutionService、启动图或维护自己的 schema 副本

### Requirement: 装配拆分不得改变既有 Thread 执行合同

同一精确 SessionThread SHALL 继续由原驻留 owner 按原 generation key single-flight 构建并复用唯一执行图；不同 Thread 不共享捕获归属的实例。每次执行 SHALL 使用当次原 admission，MCP／工具参数／上下文按原规则封存。回退成功、中断、恢复、释放和终态 SHALL 保持原业务语义；拆分 MUST NOT 增加另一执行路径、查询专用 Graph 或长期开关兼容模式。

#### Scenario: 同代连续执行与并发查询
- **WHEN** 一个 Thread 连续执行两个 Turn，期间多个编辑器读取能力和状态
- **THEN** 同代复用一个运行时，目录查询不获取执行 lease；两个 Turn 分别使用自己的 admission，不重复构建工具或把查询状态带入执行

#### Scenario: primary 失败后 fallback 成功
- **WHEN** 正式执行的首选模型失败，原策略允许的 fallback 成功
- **THEN** 保留真实 attempt 明细且 Turn 最终成功，原工具调用与上下文归属不变，不把中间失败写为最终失败

#### Scenario: GraphBinding 无法解析
- **WHEN** 原 Thread 的冻结图 selector 不再可验证解析
- **THEN** 沿原合同明确拒绝执行，不以本次拆分为理由静默改用最新图；工具定义查询仍独立于该 Thread 的图

### Requirement: 能力读取和本地失败必须遵守完整交互预算

在服务就绪、本机回环、无外部等待时，≤512KiB 的普通完整暖 API SHALL 满足 p95≤500ms、单次≤1500ms，首次未缓存业务读取≤3000ms，正确设置／Composer 可实际操作≤2000ms；已有更严格预算优先。快速本地工具错误 SHALL 从真实发送操作计时至错误 DOM 可见，暖请求 p95≤500ms、单次≤1500ms，首次≤3000ms。计时包含消息准入、队列、内部请求、锁、准备、错误提交、Gateway／事件传输和 UI 更新，不以占位、进程 timeout、最后一个 helper 或均值代替。

#### Scenario: 小目录读取夹带模型初始化
- **WHEN** 工具目录最终响应完整但因为模型 SDK 初始化造成单次暖读超过1500ms
- **THEN** 验收失败并保留完整样本，不能通过提前返回 loading 或把初始化时间移出统计来通过

#### Scenario: 本地失败先等待模型
- **WHEN** 无效工具 factory 的原错误最后正确显示，但真实操作耗时超过本地失败预算
- **THEN** 功能原因保真不能代替性能通过，保留消息准入／准备／传输／显示的归因证据，不自动重试直到碰巧达标

### Requirement: 验收必须覆盖真实入口和必要依赖隔离

验收 SHALL 使用真实 Gateway、明确目标工作区和 Workbench 操作，覆盖工具目录／详情、本地失败与正式执行的关键状态转换；定向覆盖压缩规划与显式模型的实际变动。模型发现／测试、Skill／上下文／状态查询仅在其调用链受影响时补验，不重建全模块测试矩阵或六编辑器布局工程。用少量新进程检查证明目录和模型无关工具错误不导入实际 Provider SDK 重依赖、不构造模型、不编译图也不调用上游；必要框架类型／协议导入允许保留，不要求工具完全脱离 langchain_core／langgraph。暖 API 和暖失败各至少20个未筛选样本，记录原始时长、nearest-rank p50/p95/max、request_id 或原执行关联、规模、源码指纹和准确退出码；冷样本单列。全部测试 SHALL 有进程外时间和内存保护，不默认全仓长跑，不新增生产监控或完整性平台。

#### Scenario: helper 通过但真实目录仍走旧路径
- **WHEN** 共享工具准备函数单测通过，但真实工具 API 仍调用执行装配或返回空 custom 参数
- **THEN** 验收不通过，须迁齐权威业务入口和消费者，不能以局部测试或任务勾选代替产品入口结果

#### Scenario: 验证受配置或第三方依赖阻断
- **WHEN** 真实入口未能完成或出现超预算样本
- **THEN** 如实报告原始失败及未验证范围，先归因，不擅改用户环境、筛样本、放宽预算或声称所有行为通过
