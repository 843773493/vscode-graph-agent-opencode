## Purpose

为当前工作区提供可由模型经统一扩展信封使用的连接查询、授权发起、状态核实和断开能力，明确本地 MCP 连接与上游应用授权的责任，并使用户长时间授权能够安全地恢复到发起任务的精确 thread。

## ADDED Requirements

### Requirement: 连接管理复用现有扩展调用入口

系统 SHALL 在统一扩展目录注册本地 connections.manage，支持 list、connect、status 和 disconnect；status SHALL 支持有界 wait_timeout_ms。该目标 MUST 在待授权 MCP 未连接时仍可由本地 owner 使用，经过既有执行策略、真实参数校验和必要人工确认。模型 MUST 不通过该工具任意新增 Server、改写配置或获取凭据。

#### Scenario: 未连接 Server 发起授权

- **WHEN** 模型根据信息完整的搜索结果经信封 connect 一个已配置连接
- **THEN** 后端返回持久操作 ID、真实状态和产品授权入口，不要求先连接远端才能使用本地控制工具

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
- **THEN** 显示连接已就绪但工具等待激活，不突破冻结策略或循环唤醒模型

### Requirement: 长授权等待必须持久化并精确恢复

长等待 MUST 使用持久操作、deadline、精确 session_id/thread_id 和恢复去重身份，复用既有后台通知与内部执行准入，不长期占用普通工具调用。登记与终态观察 MUST 避免丢唤醒；终态提交后通知原 thread，并只恢复仍有效、通过最新权限与删除 fence 的 continuation。短查询超时 MUST 不取消共享连接操作，取消 thread 等待 MUST 不默认断开共享连接。

#### Scenario: 授权在等待登记之前完成

- **WHEN** owner 已提交成功终态，随后同一操作的等待登记生效
- **THEN** 等待通过读取权威终态获得完成事实，不无限等待下一条通知

#### Scenario: 重复 callback 与进程重启

- **WHEN** 同一授权完成重复通知，且工作区进程在投递或恢复阶段重启
- **THEN** 原 thread 获得可恢复的逻辑完成通知，continuation 至多产生一次逻辑恢复效果，不发往主 thread 或其他工作区

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
