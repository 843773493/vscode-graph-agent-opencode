## Purpose

为每个工作区的 MCP 连接提供统一的监督、目录发布、执行身份和受控替换合同，使连接配置能够按真实准备结果生效，旧调用在明确的代际和期限内收敛，连接失败及进程恢复不会静默迁投或伪报成功。

## ADDED Requirements

### Requirement: MCP 连接与目录以工作区为唯一归属

系统 MUST 在每个工作区维持唯一的连接监督与目录发现链路，不因 thread 或 Agent 重建重复建立同一连接。完整分页目录经过验证后整体发布；变化通知、显式刷新、重连及配置替换 MUST 收敛到同一路径，不发布半目录。不具备变化通知的 Server MUST 只在可观察刷新边界承诺更新，不在模型请求准备时联网刷新。

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

### Requirement: 目录语义版本与连接执行代际必须分离

完整定义未变化的刷新 MUST 不推进目录语义 revision；新建或替换连接 MUST 改变执行 connection generation，即使工具 schema 完全相同。调用 MUST 绑定稳定 Server 身份、连接代际及完整 schema 身份；过期通知和 relist 结果 MUST 不影响新代际。当前同名工具 MUST 不接管旧封存调用。

#### Scenario: 连接替换但 schema 未变

- **WHEN** Server URL 或凭据改变，候选连接返回与旧连接相同的工具定义
- **THEN** 目录语义 revision 可以保持，执行连接代际必须改变，旧调用不能静默发送到新连接

#### Scenario: 旧 generation 迟到通知

- **WHEN** 退役连接的变化通知或 relist 在新连接发布之后到达
- **THEN** 该结果被明确识别为过期，不覆盖新目录或连接状态

### Requirement: 热替换候选准备完成后才能整体切换

配置变更 MUST 只替换受影响的 Server；同一候选的所有变更 MUST 在初始化、完整目录验证与冲突检查成功后统一可见，并与 active 配置提交边界保持一致。候选未生效时模型 preparation、API 和调用 MUST 不观察到新目录配旧 active 配置。连接生命周期 MUST 由其所属监督上下文创建和关闭。

提交 MUST 复用现有配置 applying journal、claim 和 active promotion，并将候选准备与 publication 纳入同一受控准入协议；不得在 candidate_applier 返回前提前公开新目录，再将之后独立发生的 snapshot commit 声称为原子切换。

#### Scenario: 多 Server 候选部分失败

- **WHEN** 同一候选的第一个 Server 准备成功，第二个失败
- **THEN** 系统不发布第一个的候选目录；旧运行态可以保持时保留旧 active 并报告整体应用失败

#### Scenario: 单实例 stdio 替换

- **WHEN** 变更的 Server 无法同时启动两个实例
- **THEN** 系统先由原配置 owner 核对有效 apply claim 并将停止原连接的意图写入原 journal，再明确关闭其新执行准入、展示短暂不可用并停止再启动；核实原实例关闭后才启动候选，失败且无法恢复旧运行态时报告 recovery_required

#### Scenario: 破坏性准备前 apply claim 已失效

- **WHEN** stdio 替换即将停止原连接，但原 apply claim 已到期、丢失或被其它 fencing 身份替换
- **THEN** 原 journal recorder 明确拒绝，停止意图和破坏动作均不得继续，不将原 active 或连接伪报为已经替换

### Requirement: 旧调用必须在有界租约内真实收敛

正常替换 MUST 停止向旧代际分配新的执行资格，但在有界期限内保留切换前已接纳或已封存并保留资格的调用，包括人工确认等待。禁用、断开与执行撤权 MUST 在执行点拒绝尚未开始的调用；进行中调用按明确策略取消或收敛。资源关闭只有真实核实后才能报告；取消、断线或超时不能证明远端未执行时 MUST 返回结果未确认，不自动重试副作用。

跨 execution／Turn 的持久资源持有 MUST 复用 ExternalResourceLeaseLedger 的 mcp_connection 和 typed resource-use 合同，状态证据由实际连接 owner 提供；owner 内短期 generation／handle 保留 MUST 不成为第二套持久租约账本，也不得按参数名称推测资源使用。

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

#### Scenario: 同一连接跨执行被持有

- **WHEN** 工具通过 typed resource-use 声明继续持有 MCP 连接资源，后续执行仍使用该资源
- **THEN** 持有和释放沿既有 ExternalResourceLeaseLedger 记录，连接 owner 提供真实状态；调用 handle 的临时排空引用不产生第二份持久占用事实

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

进程重启 SHALL 从已确认配置重建新连接代际并对账未完成授权／替换／等待操作；旧 socket 和不可恢复 handle MUST 不被视为恢复成功。需要人工处理或无法确认的状态 MUST 明确显示，历史调用与结果必须保留可核对关系。结构化、多模态结果及协议错误 MUST 沿现有结果合同保真处理。

#### Scenario: 崩溃发生在连接切换提交期间

- **WHEN** 进程在候选连接就绪和 active promotion 之间崩溃
- **THEN** 恢复根据持久提交阶段核实生效配置与连接，不按内存假设宣布成功，不重新执行历史业务调用

#### Scenario: MCP 返回协议业务错误和媒体内容

- **WHEN** 工具返回 isError、结构化内容或图片等媒体引用
- **THEN** 模型及结果存储保留真实错误归属和内容类型，不压成伪成功字符串或丢弃媒体
