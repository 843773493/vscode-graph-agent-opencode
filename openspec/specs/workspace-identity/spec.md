# workspace-identity Specification

## Purpose
规定工作区自身 UUID 在 Gateway 登记、Backend 挂载、资源寻址和客户端恢复中的唯一身份合同，消除工作区别名与业务身份的重复映射。明确跨 Gateway 的归属表达、存量引用迁移及独占运行边界，使连接和进程变化不再改变工作区身份。
## Requirements
### Requirement: 工作区自身 UUID 是唯一 workspace_id

系统 SHALL 使用工作区 `.boxteam/workspace-identity.json` 中既有的标准 UUID 作为唯一 `workspace_id`。Gateway 登记、Backend mount、公开协议、HTTP 请求与响应、资源 URI、持久引用和客户端恢复 MUST 使用同一值。有效既有 UUID MUST 保留；只允许既有身份初始化入口为尚无身份的新工作区创建一次 UUID，不得根据路径、连接、端口、Backend 进程或 Gateway 登记重新分配身份。

系统 MUST 移除 `gw_...` 工作区别名、`backend_workspace_id`、重复的 `remote_workspace_id` 及其运行时换算链，不得通过改名为 `target_id` 等字段继续保留第二个工作区身份。连接、进程和导航文件夹的独立标识不受该删除要求影响。注册尚未完成时 MUST 使用明确的连接或注册操作状态，不得生成临时 `workspace_id` 冒充已登记工作区。

#### Scenario: 已有工作区注册与重启
- **WHEN** 已有合法 UUID 的工作区被注册、重启、移入共享 Backend 或在保留身份文件后更换根位置
- **THEN** Gateway、Backend、业务响应及后续资源引用使用原 UUID，不产生路由工作区别名或重新生成 UUID

#### Scenario: 同一文件的 HTTP 与资源 URI
- **WHEN** 用户通过显式工作区 HTTP 入口及该工作区资源 URI 操作同一文件
- **THEN** HTTP 中的 `workspace_id` 与 URI 的 workspace scope ID 相等，并由同一工作区绑定解析到目标文件，不经过工作区 ID 换算

#### Scenario: 新工作区尚未取得身份
- **WHEN** 管理入口已收到原生根参数但尚未完成授权、身份初始化和登记
- **THEN** 系统只报告注册状态；完成后才发布工作区 UUID，不用路径哈希或临时路由 ID 代替

### Requirement: 跨 Gateway 引用必须保留工作区归属

跨 Gateway 的工作区归属 SHALL 使用 `(gateway_id, workspace_id)`，其中 `gateway_id` 是原生目标 Gateway 的稳定身份，`workspace_id` 始终是工作区自身 UUID。联邦投影、活动选择、目录与 Generator 引用、搜索目标、编辑器恢复及缓存键 MUST 保留足以明确该归属的信息；不得用连接 ID、显示名、当前激活项或中继 Gateway 身份替代。

原生 Gateway 内同一 UUID 绑定不同物理根时 MUST 明确拒绝冲突，不得静默重生成 UUID 或覆盖根。不同 Gateway 中出现相同 UUID 时 MUST 按 Gateway 归属区分，不得因 UUID 相同而合并业务状态。同一来源工作区经不同连接导入时 MUST 仍是同一个资源身份；连接的存在与失效不得改变该 UUID。

#### Scenario: 两个 Gateway 暴露相同 UUID
- **WHEN** Gateway A 和 Gateway B 各自声明一个相同 UUID 的工作区
- **THEN** 用户选择、搜索、资源请求与恢复记录按各自 Gateway 区分两者，不能串用内容或凭据

#### Scenario: 同一原生 Gateway 中 UUID 冲突
- **WHEN** 一个已登记 UUID 被用于登记另一个物理根且未通过既有受控根替换流程
- **THEN** 登记在发布前失败并报告冲突，原绑定及业务数据保持不变

#### Scenario: 同一远程工作区有多个连接
- **WHEN** 两条连接发现相同来源 Gateway 和工作区 UUID
- **THEN** 系统保留一个工作区资源身份及各自连接关系，不生成两份投影工作区 ID，也不据此新增自动故障切换行为

### Requirement: 代理必须传递目标身份且保持原授权边界

Gateway SHALL 将选定目标的原生 Gateway 归属和工作区 UUID 显式传递给路由层，并将同一个 UUID 转发到 Backend。现有 HTTP 工作区路径段与等价工作区头 MUST 保持一致；多个载体冲突、目标缺失、归属不符、UUID 无效或无权访问时 MUST 明确拒绝，不得回退到当前激活工作区。Gateway 归属的表达 MUST 与发送方身份和认证凭据分离，不得改变 `X-BoxTeam-Gateway-Id` 的发送方身份语义来选择目标。

原生根登记、替换和移除 MUST 继续由目标 Gateway 按既有管理授权决定。UUID 相同、连接成功、资源可读或持有旧别名 MUST NOT 推导管理权限。中继不得持久保存第二份工作区根绑定。

#### Scenario: 本地与联邦代理使用同一 UUID
- **WHEN** 请求经过本地 Gateway 直达 Backend 或经过中继转交原生 Gateway
- **THEN** 各跳工作区 UUID 不变，目标 Gateway 归属明确，原 request ID 和既有认证、管理权限边界保留

#### Scenario: 路径与头部身份冲突
- **WHEN** 显式工作区路径段与工作区头指向不同 UUID，或目标 Gateway 与资源归属不符
- **THEN** 系统拒绝请求，不执行文件或业务操作，不尝试别名换算或激活项回退

### Requirement: 旧工作区引用必须显式迁移且不保留双轨

系统 SHALL 为已支持的旧版本控制面记录提供显式、可恢复的一次性迁移，将旧工作区别名准确替换为原生 Gateway 归属和工作区 UUID。迁移 MUST 覆盖 registry、root binding、共享 Backend 成员、活动选择、导航目录、Generator、用户视图态及客户端持久恢复引用；迁移范围按实际字段职责确定，不得替换用户正文中相似字符串。

迁移 MUST 校验完整映射、重复项及引用完整性后发布；缺失、冲突或无法确认远端身份时必须保留原状态并报告阻塞项。客户端旧恢复项只能按已确认的迁移结果升级；无法解析时 MUST 明确报告该项不能恢复，不得按当前工作区猜测，也不得清空其他有效恢复项。迁移完成后 MUST 删除旧映射字段、生成器、接口和消费者，正常运行不保留旧别名查询或双读。有效资源 URI、canonical 正文、历史请求 hash、Session 和 Thread 身份 MUST 保留。

#### Scenario: 完整旧登记迁移
- **WHEN** 受支持旧库中的工作区身份均可从原登记和目标身份确认
- **THEN** 所有相关控制面引用一致切换到 UUID 与归属，重启和客户端恢复仍定位原工作区；重复执行迁移不改变结果

#### Scenario: 远端离线或映射歧义
- **WHEN** 迁移不能确认某个旧别名对应的来源 Gateway 与 UUID
- **THEN** 迁移明确阻塞且不发布部分可用的新登记，不读取中继本机同名路径、不生成新 UUID、不丢弃旧引用

#### Scenario: 浏览器恢复旧工作区编辑器
- **WHEN** 客户端加载旧别名保存的编辑器或活动选择
- **THEN** 有已确认映射的项准确升级并正常恢复，其他项逐项报告不可恢复且不跳转到不同工作区

### Requirement: 身份统一必须保留工作区独占与业务存储边界

工作区业务数据 SHALL 继续位于其物理根的 `.boxteam/`，Gateway 全局控制面继续位于其 home；身份统一不得移动会话、检查点或日志到全局目录，不得改变 Session／Thread 归属和现有物理布局。

同一物理工作区的正式 Backend 启动 MUST 继续受工作区内现有跨进程独占机制保护，切换 `BOXTEAM_HOME` 不得绕过正在运行的占用者。原进程退出并释放锁后，其他合法 Backend MAY 按既有授权接管；系统 MUST NOT 新增永久 home 绑定、第二套占用账本或仅以 UUID 命名的全局锁替代物理工作区锁。

#### Scenario: 换 home 启动第二个 Backend
- **WHEN** Backend A 已挂载某个物理工作区，Backend B 使用另一 home 尝试挂载同一根
- **THEN** B 明确报告占用冲突，不能进入可服务状态或改写该工作区业务数据

#### Scenario: 原 Backend 退出后接管
- **WHEN** 原占用者退出并释放锁，另一个合法 Backend 使用另一 home 启动同一工作区
- **THEN** 新 Backend 使用原工作区 UUID 和既有业务数据，不因旧 home 记录而永久拒绝接管
