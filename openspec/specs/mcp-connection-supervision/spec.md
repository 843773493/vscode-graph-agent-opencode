## Purpose

为每个工作区的 MCP 连接提供统一的监督、目录发布、执行身份和受控替换合同，使连接配置能够按真实准备结果生效，旧调用在明确的代际和期限内收敛，连接失败及进程恢复不会静默迁投或伪报成功。

## Requirements

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

### Requirement: 目录语义版本与连接执行代际必须分离

完整定义未变化的刷新 MUST 不推进目录语义 revision；新建或替换连接 MUST 改变执行 connection generation，即使工具 schema 完全相同。调用 MUST 绑定稳定 Server 身份、连接代际及完整 schema 身份；过期通知和 relist 结果 MUST 不影响新代际。当前同名工具 MUST 不接管旧封存调用。

#### Scenario: 连接替换但 schema 未变

- **WHEN** Server URL 或凭据改变，候选连接返回与旧连接相同的工具定义
- **THEN** 目录语义 revision 可以保持，执行连接代际必须改变，旧调用不能静默发送到新连接

#### Scenario: 旧 generation 迟到通知

- **WHEN** 退役连接的变化通知或 relist 在新连接发布之后到达
- **THEN** 该结果被明确识别为过期，不覆盖新目录或连接状态

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

### Requirement: 旧调用必须在有界租约内真实收敛

正常替换 MUST 停止向旧代际分配新的执行资格，但在有界期限内保留切换前已接纳或已封存并保留资格的调用，包括人工确认等待。禁用、断开与执行撤权 MUST 在执行点拒绝尚未开始的调用；进行中调用按明确策略取消或收敛。资源关闭只有真实核实后才能报告；取消、断线或超时不能证明远端未执行时 MUST 返回结果未确认，不自动重试副作用。

连接生命周期、封存资格和实际调用占用 MUST 继续复用 ExternalResourceLeaseLedger 的 mcp_connection 和现有 typed resource-use 合同，状态证据由实际连接 owner 提供；owner 内短期 generation／handle 保留 MUST 不成为第二套持久租约账本，也不得按参数名称推测资源使用。跨轮次使用 MCP 资源继续依靠既有连接管理与服务提供的资源 ID／引用，实际资源状态和远端会话恢复能力由 MCP 服务负责。本地 MUST 保留既有调用租约、审批等待保护与结果未知记录，不将本地占用事实视为远端会话持续或可恢复的保证。

工具显式声明跨 execution 持有／释放原 MCP 逻辑会话为**延期设计 TODO**，不属于本轮必做实现与验收；本轮 MUST NOT 新增通用持有／释放工具、用户入口或连接固定机制。只有后续确认具体工具必须沿用同一逻辑会话且现有机制不足时，才定义持有、释放、配置替换和会话失效行为；如需持久占用，MUST 复用既有资源账本，不建立第二套账本。延期不改变其余连接代际、调用结算和恢复合同。

系统 MUST 在模型发送前为冻结目录的精确连接保留有界资格，并在模型响应后收窄到真实输出的信封调用身份；既有人工确认 edit 仍可选择原封存目录内的目标。期限沿 Workspace 配置族的 `mcp.supervision.sealed_call_timeout_seconds` 读取，默认1800秒、范围1–86400秒，从取得原 handle 起包含模型生成和确认等待，不因重试续期。资格与实际操作占用 MUST 分别结算，资格过期不能伪报正在执行或结果未知的远端操作已结束。

未进入 MCP 执行的调用 MUST 在完整 canonical ToolResult 提交后按精确 Session／Thread／model-call／provider tool-call 身份结算未来资格，不等待原期限；partial／incomplete 结果和其它调用的回执 MUST NOT 结算该资格。工具资格失效 MUST NOT 阻断原 execution 后续生成普通模型回复，且后继模型调用 MUST NOT 为同一到期 execution 重新发放资格或重置 deadline。

#### Scenario: 拒绝或本地改写后结算未用资格

- **WHEN** 原信封调用经拒绝、直接应答、编辑为本地工具或准入失败提交完整 canonical ToolResult
- **THEN** 只结算该调用的未来资格，同轮兄弟调用继续保留自身资格；重复结果不结算其它调用，正在执行或结果未知的实际 operation 仍独立保留

#### Scenario: 禁用后原执行仍可回复

- **WHEN** 已取得资格的模型请求返回前连接被禁用，随后工具拒绝结果已提交
- **THEN** turn 和 model_call 策略均保留原 tool_call_id 的明确失败且不向 Server 发请求，原执行可在后继模型请求中解释失败；失效目标不取得新资格，未用资格在后继请求前已经结算

#### Scenario: 资格到期但原连接仍运行

- **WHEN** 原模型调用的资格到期，工具尚未开始，而原连接仍可用
- **THEN** 系统释放未来执行资格，返回与原 tool_call_id 配对的明确失败，不向 Server 发送请求，也不把连接报告为已停止

#### Scenario: 原 execution 到期后继续模型调用

- **WHEN** 原 execution scope 仍打开且原工具资格已经到期，执行需要生成下一次模型回复
- **THEN** 系统允许普通回复，释放原工具和 runtime 引用并保留到期身份直至 scope 关闭；下一模型调用不重置 deadline，不重发工具资格，输出工具调用时仍明确配对失败

#### Scenario: 跨轮次继续使用服务资源引用

- **WHEN** 后续 execution 使用 MCP 服务先前返回的资源 ID／引用
- **THEN** 本地沿既有连接管理和调用链请求实际服务，由服务核实资源与会话状态；无效或过期时明确返回实际失败，不自动固定原逻辑会话，不以本地账本宣称远端状态可恢复

#### Scenario: 原 Turn 在替换后继续工具循环

- **WHEN** turn-bound 目录的执行已在切换前取得原连接有界资格，工具结果之后还需下一次模型调用
- **THEN** 后继调用仅继承同一原执行已保留的精确 handle 与原期限，不改写冻结目录、不延长期限，也不向新执行或其它 thread 发放旧代际资格；原 execution scope 关闭后释放其未来资格，结果未知的实际操作仍独立保留

#### Scenario: 人工确认跨越连接替换

- **WHEN** 旧 model call 的工具正在等待确认，连接已被替换
- **THEN** 用户确认后只可在原保留资格和期限内调用原 handle；资格失效时返回与原 tool_call_id 配对的明确失败

#### Scenario: 用户编辑待审批调用

- **WHEN** 用户针对原检查点的精确调用提交编辑决定
- **THEN** 原模型输出、canonical 调用和已封存请求保持不变；编辑内容作为明确执行决定进入同一工具执行链路，并继续检查原目录资格和最新权限
- **AND** 决定接收回执不能表示执行已成功；取消后或检查点失效的决定不得恢复执行

#### Scenario: 审批等待中取消执行

- **WHEN** 用户取消仍在等待人工决定的 execution
- **THEN** 原 scope 释放所属全部待用资格，迟到决定不能恢复调用；已发送且结果未知的外部操作仍保留原独立占用，不因取消被误报完成

#### Scenario: 执行中断线

- **WHEN** 已发送的远端写操作在获取结果前连接断开
- **THEN** 返回 outcome-unknown 或同等明确结果，保留原调用身份，不向新代际自动重发

#### Scenario: 原请求取消或等待超时

- **WHEN** 已发送的调用被取消或等待超时，无法核实远端最终副作用
- **THEN** 系统向原 RPC 请求发送有界取消通知，不取消同连接的其它请求；回执明确通知投递事实与结果未知、禁止自动重试，并在实时结果及冷恢复中保持原调用配对，不将通知发送成功等同于远端回滚

#### Scenario: 进度发布保留原调用归属

- **WHEN** Server 返回进度或本地进度写入失败
- **THEN** 数值进度只更新原调用的 Activity，不转发 Server 自由正文；写入失败明确终止原等待并报告结果未知，不被 SDK 静默吞掉，不影响同连接其它调用

#### Scenario: 退役连接清理失败

- **WHEN** 新代际已经切换成功但旧连接排空或关闭失败
- **THEN** 配置结果保留真实已切换事实并展示清理失败，不伪报资源释放，也不将已发生的切换说成从未应用；`/config/reload-status` 的 `mcp_cleanup` 从原 owner 投影精确 Server／连接、阶段、释放核实与异常类型，原资源状态通道通知 release_failed。后续撤销旧代际准入不清除既有清理阶段与错误类型；未核实关闭的失败引用继续阻止同 Server 冲突替换，其他 Server 仍可更新；普通响应不包含异常自由正文、路径或凭据

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
