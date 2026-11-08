## MODIFIED Requirements

### Requirement: MCP 连接与目录以工作区为唯一归属

系统 MUST 在每个工作区维持唯一的 MCP client 连接监督与目录发现链路，不因 thread 或 Agent 重建重复建立同一连接。Backend MUST 经 Gateway 以标准 Streamable HTTP 连接目标插件服务；stdio 子进程由 Gateway 托管的自有插件持有，Backend 不直接创建。Gateway 范围插件共享运行进程不合并工作区逻辑连接、凭据或资源；无法隔离工作区的服务必须使用 workspace 实例范围。完整分页目录经过验证后整体发布；变化通知、显式刷新、重连及配置替换 MUST 收敛到同一路径，不发布半目录。不具备变化通知的 Server MUST 只在可观察刷新边界承诺更新，不在模型请求准备时联网刷新。

受支持的待授权状态 MUST 按 Server 发布 readiness，保持同一目录中的本地控制和其它就绪 Server 可达，不因预期授权需求整体阻断启动或必需资源冻结；未知目录不发布虚构工具，配置／协议故障仍明确报错。

#### Scenario: 同进程多工作区使用同名 Server

- **WHEN** 两个工作区配置相同 server_id 并分别调用
- **THEN** 连接、凭据引用、目录、状态和操作只属于各自工作区，不因 ID 相同跨工作区复用

#### Scenario: 分页目录加载中途失败

- **WHEN** 第一页读取成功而后续页或 schema 验证失败
- **THEN** 系统报告目录不可用或刷新失败，不发布已读取部分作为新有效目录

#### Scenario: 单个 Server 首次需要授权

- **WHEN** Server 经协议确认需要受支持授权，尚不能读取其目录
- **THEN** 唯一 owner 保留该 Server 真实未授权状态，并发布完整本地控制和其它可用成员，不将整个父目录标记不可用

#### Scenario: 原生 HTTP 和 stdio 均经过 Gateway
- **WHEN** 工作区分别连接外部 HTTP MCP 与自有 stdio MCP 插件
- **THEN** 两者均经明确 Gateway 路由进入同一工作区 owner；仅后者在所属插件创建子进程，Gateway 不建立另一工具目录


### Requirement: 热替换候选准备完成后才能整体切换

配置变更 MUST 只替换受影响的 Server；需要重启一个插件以应用时，MUST 明示该插件全部服务的中断范围，先核旧实例退出再启动新实例，不能并行准备第二个同绑定 runtime 或停止兄弟插件；同一候选的所有变更 MUST 在初始化、完整目录验证与冲突检查成功后统一可见，并与 active 配置提交边界保持一致。候选未生效时模型 preparation、API 和调用 MUST 不观察到新目录配旧 active 配置。MCP client 连接生命周期 MUST 由 Backend 原监督上下文创建和关闭；实际服务进程／stdio 管道 MUST 经 Gateway 原生 owner 和所属插件管理，不能因关闭 client 就推断进程退出。

提交 MUST 复用现有配置 applying journal、claim 和 active promotion，并将候选准备与 publication 纳入同一受控准入协议；不得在 candidate_applier 返回前提前公开新目录，再将之后独立发生的 snapshot commit 声称为原子切换。

唯一协调链 SHALL 是 Backend 原配置 journal／claim → 原 MCP 候选准备 → 必要的 Gateway 单插件生命周期操作 → 原 MCP owner 完整目录验证 → 原 active promotion／publication。Gateway 只报告插件进程实际状态，不保存候选、active 或目录提交阶段。破坏旧运行前失败时保留可保持的旧运行；旧实例已停止或发布已经开始后失败，由原 journal 记录实际阶段及不可用／需人工处理并结束本次应用，不自动恢复旧进程、续完候选或重放操作。recovery_required 只表达诊断，不隐含新恢复器；原候选清理仅释放本次拥有的连接／引用，仍须核实其真实结果。

插件 enabled 的关闭 SHALL 独立于 MCP 参数替换，停止新准入及进程不得等待候选目录／授权准备。配置替换不改变 enabled，不以旧 apply claim 代替当前插件启动资格；准备、破坏性停止、启动及 publication 均由原 owner 按当前插件 revision／期望串行核验。关闭导致旧候选失效时按原冲突／失败结算，不覆盖新 enabled 或重新启动。已关闭插件的参数只作离线校验及配置提交，不初始化 client／目录，后续显式开启再完成运行期验证。

#### Scenario: 配置候选准备中关闭插件
- **WHEN** 已开启插件正在准备 MCP 参数候选，用户以新 revision 关闭该插件
- **THEN** 关闭撤销新准入并沿原期限停止，旧候选不等待就绪、不重开进程或发布工具，不能以旧 active 恢复 enabled；后续关闭态参数修改也不启动

#### Scenario: 多 Server 候选部分失败

- **WHEN** 同一候选的第一个 Server 准备成功，第二个失败
- **THEN** 系统不发布第一个的候选目录；旧运行态可以保持时保留旧 active 并报告整体应用失败

#### Scenario: 单实例 stdio 替换

- **WHEN** 变更的 Server 无法同时启动两个实例
- **THEN** 系统先由原配置 owner 核对有效 apply claim 并将停止原连接的意图写入原 journal，再明确关闭其新执行准入、展示短暂不可用并请求 Gateway 按原实例停止；由实际进程 owner 核实关闭后才启动候选，停止或启动失败记录真实阶段及 recovery_required 并结束，不尝试恢复旧进程、不双起或循环重试

#### Scenario: 破坏性准备前 apply claim 已失效

- **WHEN** stdio 替换即将停止原连接，但原 apply claim 已到期、丢失或被其它 fencing 身份替换
- **THEN** 原 journal recorder 明确拒绝，停止意图和破坏动作均不得继续，不将原 active 或连接伪报为已经替换

#### Scenario: Gateway 实例启动不能提前发布候选目录
- **WHEN** 插件进程已 ready，但 Backend 的完整目录验证或配置 active promotion 尚未完成
- **THEN** 进程状态和业务配置状态分别报告，原调用不观察半份新目录；失败只清理本次拥有的连接／引用及专属候选实例，不停止其它工作区或共享服务，Gateway 不另行提交目录或持有第二候选 journal


### Requirement: 重启恢复不得伪造会话或重放外部副作用

Backend 重启 SHALL 从已确认配置经 Gateway 重新读取原实例状态、重建 MCP client 连接及新 connection generation，并沿原 journal 判定未完成授权／替换／等待操作的事实；不增加自动恢复候选或通用对账状态机，不顺带停止独立服务。插件显式重启使用新 instance_id，旧执行句柄不得迁投；旧 socket 和不可恢复 handle MUST 不被视为恢复成功。需要人工处理或无法确认的状态 MUST 明确显示，历史调用与结果必须保留可核对关系。结构化、多模态结果及协议错误 MUST 沿现有结果合同保真处理。

#### Scenario: 崩溃发生在连接切换提交期间

- **WHEN** 进程在候选连接就绪和 active promotion 之间崩溃
- **THEN** 恢复根据持久提交阶段核实生效配置与连接，不按内存假设宣布成功，不重新执行历史业务调用

#### Scenario: 既有可选人工确认在执行中断后不恢复旧调用

- **WHEN** 显式配置了原人工确认机制的执行正在等待确认，Backend 中断后重启
- **THEN** 原执行 owner 按既有 execution_lost、checkpoint／control inbox 和结果合同收口，保留原 call ID 及已确认结果；旧确认不恢复调用、未知副作用不自动重放，同一会话仍可接受新执行
- **AND** 本变更只回归上述既有行为，不新增审批存储、终态事务、检查点修复、消息重组或执行续跑机制；插件实例存活与原执行丢失分别报告

#### Scenario: MCP 返回协议业务错误和媒体内容

- **WHEN** 工具返回 isError、结构化内容或图片等媒体引用
- **THEN** 模型及结果存储保留真实错误归属和内容类型，不压成伪成功字符串或丢弃媒体

#### Scenario: 已确认的协议错误不报告结果未知

- **WHEN** SDK 收到原调用的明确协议错误，并将原错误交还调用者
- **THEN** 同一调用的 Activity 以已确认失败结算，保留原错误码与协议数据的既有交付；不发布 outcome_unknown 或待核实副作用，不复制授权 URL 等自由正文到 Activity
- **AND** 原 operation lease 按真实回复释放，同连接的兄弟调用仍可继续；取消、超时或断线尚无确认结果时仍保持结果未知，禁止自动重放

#### Scenario: 插件存活不恢复旧执行
- **WHEN** Backend 崩溃重启后 stdio 服务仍存活，且原调用没有可确认结果
- **THEN** 新 client 可以读取服务实际状态，但原 execution 按原丢失／未知结果规则收口；不得重放原写操作或用新句柄补造成功


## ADDED Requirements

### Requirement: 插件迁移不得新增人工工具审批机制

人工工具确认 SHALL 仅复用原执行 owner 已显式配置的可选机制，普通会话及插件管理不新增审批请求、入口、等待状态或轮询。原等待事件／快照已明确进入既有确认流程时才沿原读取合同处理；插件安装、运行与配置权限校验和 MCP OAuth SHALL 各沿原 owner，不转换成人工工具审批，不因本次迁移默认开启确认。

#### Scenario: 普通插件使用与 MCP OAuth 不产生审批轮询
- **WHEN** 普通会话读取插件状态、调用已就绪工具，或 MCP 进入 OAuth 待授权状态
- **THEN** 没有新增人工审批请求／入口／轮询；OAuth 只显示原授权状态及操作，不创建工具审批记录，工具调用仍遵守原业务资格和结果合同

### Requirement: 私有服务输入只能经原 owner 在启动准入时交付

工作区私有输入 SHALL 继续保存在原 private-input owner，公开配置只包含绑定 workspace／server／input_key／定义版本的引用。解引用 SHALL 由原 owner 执行，经认证 Gateway 准入仅交精确服务实例；Gateway 不持久化实际值，不访问工作区数据目录。普通 DTO、事件、日志、命令参数、模型输入和浏览器持久数据不得包含秘密。

同名服务、其它工作区、新来源或不匹配定义不得借用原引用。插件 instance_id／连接代际与秘密定义身份分别核验；合法重连不创造新的秘密账本。移除旧手动／Registry variant／SDK launch 引用链前必须迁齐原配置 writer、消费者和测试；旧输入不得通过 fallback 启动 Backend 子进程。

#### Scenario: 同名服务的秘密隔离
- **WHEN** A1/A2 使用同名服务及不同私有输入，随后 Backend 重启或用户显式重启其中一个插件
- **THEN** 原 owner 只向对应目标实例交付自己的输入，公开投影无秘密；输入缺失或绑定不符明确失败，不借另一工作区、同名旧引用或当前 UI 目标
