## Purpose

为当前工作区提供可由模型经统一扩展信封使用的连接查询、授权发起、状态核实和断开能力，明确本地 MCP 连接与上游应用授权的责任，并使用户长时间授权能够安全地恢复到发起任务的精确 thread。

## ADDED Requirements

### Requirement: 连接管理复用现有扩展调用入口

系统 SHALL 在统一扩展目录注册本地 connections.manage，支持 list、connect、status 和 disconnect；status SHALL 支持有界 wait_timeout_ms。该目标 MUST 在待授权 MCP 未连接时仍可由本地 owner 使用，经过既有执行策略、真实参数校验和必要人工确认。模型 MUST 不通过该工具任意新增 Server、改写配置或获取凭据。

首次启动及资源冻结 MUST 将经受支持协议确认的 authorization_required 表达为连接领域状态，保持本地控制成员及其它就绪 Server 在唯一目录中可冻结和执行；不得将该状态变为整个目录不可用或启动失败。远端未授权目标 MUST 无可用执行资格，未知目录不得虚构。未知授权方式、配置错误和协议故障 MUST 仍明确报错，不将任意 401 当作可交互授权。

#### Scenario: 未连接 Server 发起授权

- **WHEN** 模型根据信息完整的搜索结果经信封 connect 一个已配置连接
- **THEN** 后端返回持久操作 ID、真实状态和产品授权入口，不要求先连接远端才能使用本地控制工具

#### Scenario: 首次启动已有 Server 需要 OAuth

- **WHEN** 工作区首次启动，一个已配置 Server 按支持的协议请求授权，尚无远端工具目录
- **THEN** 工作区允许正常进入本地连接控制流程，模型可发现并调用 connections.manage；未知远端不产生伪造工具或 ready 状态

#### Scenario: 多 Server 中仅一个尚未授权

- **WHEN** 一个 Server 处于 authorization_required，另一个 Server 已验证就绪
- **THEN** 同一冻结目录保持本地控制及就绪 Server 可用，未授权状态只阻止相应远端执行，不使整个必需目录不可用

#### Scenario: connect 工具调用重放

- **WHEN** 同一次工具操作因恢复再次提交 connect
- **THEN** 返回同一有效授权操作或其终态，不重复发起授权或生成额外连接

### Requirement: 区分 Server 授权与上游应用授权

系统 MUST 区分 MCP Server 访问授权和 Server 内上游应用授权。前者 SHALL 由本地连接 owner 按受支持标准协议完成，后者 SHALL 使用实际 Server 的已知能力；不支持的方法 MUST 明确返回错误。未知目录不得因借鉴 Composio 而被视为拥有全部应用工具。

#### Scenario: 已连接 Server 要求连接上游应用

- **WHEN** 远端工具明确要求授权一个上游应用，并提供对应受支持管理能力
- **THEN** 系统展示上游授权需求并调用其真实控制目标，不把本地 MCP session 重连伪装成应用授权完成

#### Scenario: 401 但无支持的授权流程

- **WHEN** 远端拒绝访问且未提供可支持的授权机制
- **THEN** 系统返回明确的授权配置错误，不伪造授权链接或连接成功

### Requirement: 连接和授权操作状态必须由实际 owner 核实

连接状态与授权操作状态 MUST 独立表达。连接 ready MUST 以授权核验、协议初始化和目录验证成功为依据；操作失败、过期、取消、连接断开与无法确认 MUST 明确区分。模型与 UI MUST 不以 callback 到达、浏览器关闭或用户点击完成宣称连接可用；断开 MUST 撤销新执行准入并核实实际清理结果。

#### Scenario: 授权 callback 成功但目录读取失败

- **WHEN** OAuth 交换完成，而协议初始化或工具目录读取失败
- **THEN** 系统报告具体阶段错误和不可用连接，不返回 ready 或自动继续业务调用

#### Scenario: 连接已可用但当前目录仍冻结

- **WHEN** 授权成功而当前 turn 级目录尚未到允许激活边界
- **THEN** 原执行显示真实等待激活事实且冻结不变；有效 continuation 可在原 Turn 结束后经独立 internal execution 冻结新目录，不要求用户额外发送继续或循环唤醒模型

### Requirement: 长授权等待必须持久化并精确恢复

长等待 MUST 使用持久操作、deadline、精确 session_id/thread_id 和恢复去重身份，复用既有后台通知与内部执行准入，不长期占用普通工具调用。登记与终态观察 MUST 避免丢唤醒；终态提交后通知原 thread，并只恢复仍有效、通过最新权限与删除 fence 的 continuation。短查询超时 MUST 不取消共享连接操作，取消 thread 等待 MUST 不默认断开共享连接。

`connect`／`status` MUST 仅在显式提供非空且最多4096字符的 `continuation` 时登记长等待；省略时保留原操作及短查询行为。continuation MUST 只表达待继续意图，不作为可执行代码或待重放业务参数。同一 thread／operation 的已登记意图 MUST 不被后续不同意图覆盖。原调用的成功等待回执 MUST 已由 canonical owner 提交并与 task 身份匹配，失败或取消的工具结果 MUST 不产生自动继续资格。

#### Scenario: 只查询状态不登记自动继续

- **WHEN** 模型调用 `connect` 或短 `status`，未提供 continuation
- **THEN** 系统返回真实操作状态，不隐式新增后台等待或独立 Agent 执行

#### Scenario: 等待登记后原工具调用失败

- **WHEN** 后台等待已登记，但原精确工具调用最终只提交失败或取消结果
- **THEN** 系统不把该结果当作成功等待回执，也不提交 continuation 执行

等待 MUST 复用 BackgroundTask 的 typed/versioned handler、checkpoint 和原子 mailbox effect；观察 handler MUST 不重新执行授权交换或业务副作用。现有控制存储事务 MUST 原子保存 checkpoint、稳定 ID 邮箱消息和 checkpoint 内待投递 continuation 引用；可恢复投递步骤 MUST 用稳定幂等 key 提交 RuntimeSourceDispatcher，并在成功后结算，崩溃后可安全重放。邮箱 emit／collect MUST 不被视为 Agent 已准入；邮箱 ack 与内部 wakeup ack MUST 分别遵守既有 canonical ToolResult 和执行提交合同。

原调用 MUST 先提交配对等待结果，原执行按既有终态结束，等待事实由 BackgroundTask 承载，不新增 Job 状态；授权完成、目录发布且 continuation 有效后 SHALL 通过 after_turn 调度独立 internal execution。该执行 MUST 使用 turn_id=None，按既有 activation 合同针对自身真实 execution 冻结目录，不修改旧 Turn、不伪装成旧执行 retry/resume。等待展示、取消及对账 SHALL 复用 BackgroundTaskResourceProvider，活动任务 MUST 遵守现有 idle unload blocker；进程重启后的恢复不代表活动等待可以无条件卸载。

#### Scenario: 授权在等待登记之前完成

- **WHEN** owner 已提交成功终态，随后同一操作的等待登记生效
- **THEN** 等待通过读取权威终态获得完成事实，不无限等待下一条通知

#### Scenario: 重复 callback 与进程重启

- **WHEN** 同一授权完成重复通知，且工作区进程在投递或恢复阶段重启
- **THEN** 原 thread 获得可恢复的逻辑完成通知，continuation 至多产生一次逻辑恢复效果，不发往主 thread 或其他工作区

#### Scenario: 邮箱已提交但内部执行尚未提交

- **WHEN** checkpoint 与完成消息已经提交，而进程在 RuntimeSourceDispatcher.submit 前崩溃
- **THEN** 重启从未结算的投递阶段继续提交相同幂等身份，不能因为邮箱中已有消息而跳过内部执行

#### Scenario: 内部执行已提交但投递阶段尚未结算

- **WHEN** dispatcher 已接受唤醒，进程在记录投递成功前崩溃
- **THEN** 恢复重投得到同一逻辑内部执行，不重复创建执行；邮箱消息仍按其自身消费合同确认

#### Scenario: turn 策略下授权后自动继续

- **WHEN** 原用户 Turn 使用 turn 激活策略并已提交等待结果，随后授权及目录验证完成
- **THEN** 有效 continuation 在原 Turn 结束后通过无用户 Turn 的独立执行取得新目录并继续，原冻结不变，不需要改默认策略或用户再次发言

#### Scenario: 活动等待的展示与冷恢复

- **WHEN** 授权等待仍活动，随后用户查看后台资源或进程重启
- **THEN** 通过已有后台任务资源入口展示和管理；运行中遵守卸载阻挡，重启后按 checkpoint 对账恢复，不重发 connect 或授权交换

#### Scenario: 等待取消或会话已删除

- **WHEN** 授权完成时等待已取消、过期或原会话不再接受执行
- **THEN** 系统不重建会话或恢复该任务，工作区连接按其自身 owner 状态保留或关闭

#### Scenario: 外部写入结果未知

- **WHEN** 原业务调用可能已经产生副作用且结果未确认，随后用户完成重新授权
- **THEN** 系统不自动重发原写入，要求核实实际状态或建立新的明确执行意图

### Requirement: 凭据与授权入口遵循实际工作区边界

连接运行态 SHALL 属于实际工作区；等待关联 SHALL 按精确会话节点存储，路径由统一解析器确定。凭据、OAuth 交换数据和敏感授权地址 MUST 不进入模型正文、schema、日志、SSE 或会话资源；模型只获得非敏感操作摘要和产品入口。实际授权流程 MUST 验证标准 state/PKCE 等绑定，远程回调必须到实际 owner；无可达方式时 MUST 明确报错。Gateway SHALL 保持透明代理和原 request_id。

#### Scenario: 远程工作区授权

- **WHEN** 用户在本地客户端为远程 Gateway 后的工作区发起授权
- **THEN** 页面状态、回调和结果属于远端实际 owner，不使用本地 localhost 冒充远程回调，不把凭据写入 Gateway 业务数据

#### Scenario: 用户检查授权状态

- **WHEN** 用户通过主窗口查看等待与连接结果
- **THEN** UI 从后端读取真实操作状态；会话流程显示授权入口与等待，连接管理按工作区归属展示，失败不以本地展示态伪造成功

#### Scenario: Web 私有授权回调

- **WHEN** 用户打开的授权弹窗返回当前 Web origin 的独立回调页
- **THEN** 页面立即清除私有 query，不加载应用资源或保存到浏览器存储；只交回同源原窗口，原窗口核对精确弹窗身份并向原 Gateway 目标及原操作提交受保护 POST。回调页禁止缓存与 Referer 传递，Gateway 的该入口访问日志不记录 query

#### Scenario: Web 回调不可达或原窗口已关闭

- **WHEN** 配置的回调不属于当前 Web origin 的受支持入口，或用户关闭弹窗、目标切换、操作到期
- **THEN** UI 明确报告未确认事实并释放本次窗口与监听器，不猜测另一 owner、不向新工作区提交旧凭据、不将窗口关闭当作连接已就绪

#### Scenario: 授权服务拒绝访问

- **WHEN** 真实授权服务将 access_denied 交回原 Web 弹窗
- **THEN** 原页面明确显示拒绝且连接未就绪，不向 Backend 提交空 code 或伪报授权成功；用户显式取消时才按原操作提交取消，兄弟 Server 保持原状态

#### Scenario: 原授权期限到期后重新连接

- **WHEN** 等待授权的操作达到其原持久 deadline
- **THEN** 页面从实际 owner 读取并展示 expired 与期限错误，不延长原预算；后续新的显式连接意图使用新的操作身份，重复提交旧意图仍返回旧历史，历史可按 ID 核对

#### Scenario: 页面断开后核实连接释放

- **WHEN** 用户从工作区 MCP 管理入口断开已验证就绪的连接
- **THEN** 页面按原 owner 的完整操作终态撤下 ready；原资源账本保留实际连接 owner 已核实的 stopped，兄弟连接和其资源状态保持运行
