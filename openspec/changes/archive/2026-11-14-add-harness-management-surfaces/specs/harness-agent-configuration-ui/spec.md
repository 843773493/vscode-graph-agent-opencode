## Purpose

让用户无需依赖会话输入框或手工记忆配置结构，即可管理模型 Provider、Agent、工具、Skill 和指令来源；所有更改继续进入所属目标的正式配置或业务 owner，清楚展示继承、生效与错误，保护既有执行和秘密输入边界。

## ADDED Requirements

### Requirement: 业务设置必须共用正式配置来源与保存语义

管理界面 SHALL 复用所属目标正式来源、schema、预检、并发基线、保存和生效状态。表单与 JSONC SHALL 表达同一草稿，显示选定层覆盖、继承来源及真实有效状态；恢复继承只删除选定层覆盖。保存失败／冲突 SHALL 保留输入并重读原 owner；保存成功不等于运行中已生效。未接入运行链的字段 MUST 明确标为未支持且不提供会造成生效误解的普通设置控件。

Provider／Agent 表单 SHALL 直接编辑原 `(gateway_id, workspace_id 或 Gateway 范围, source_key)` JSONC 文本模型，仅以稳定实体 ID 定位字段；不为表单创建独立持久草稿或保存队列。共享来源从不同工作区打开时保留各自应用上下文、CAS 基线和回执，不按文件路径合并草稿或自动重基。定义管理 SHALL 从原脱敏 source document／schema 读取，不以成功展开全部 Provider 凭据或创建模型客户端为前置；定义可读与运行可用分别展示。

业务来源 URI SHALL 沿原机制禁用自动保存，只在显式预检／确认／CAS 后写入；客户端原生 Settings 保持原版行为，不全局关闭或强制开启 auto-save。已有即时业务开关沿其实际 owner 提交，不冒充来源草稿或等待无效的“保存”动作。

#### Scenario: 保存成功但需要重启
- **WHEN** 候选来源已保存，而 owner 报告待重启或应用失败
- **THEN** 显示来源已保存、当前仍有效的配置以及待应用原因，只提供原 owner 已支持的显式操作，不自动回滚或显示即时生效

#### Scenario: 表单切换 JSONC 与恢复继承
- **WHEN** 用户在表单编辑后切到 JSONC，再删除一项本层覆盖
- **THEN** 保留同一草稿及撤销语义，提交后显示下层值和来源，不把整个 effective 对象写回覆盖层

#### Scenario: 原生设置与业务来源保存并存
- **WHEN** 用户修改客户端原生 Settings，再在 Provider 表单修改业务来源而未保存
- **THEN** 原生设置按原行为处理，业务来源保持脏草稿且不发写请求；显式保存才进入原来源预检／确认／CAS，不套用原生设置的即时提交

#### Scenario: 共享文件的两个工作区草稿
- **WHEN** A 与 B 工作区分别打开同一用户来源，A 保存后 B 仍有旧基线草稿
- **THEN** B 沿其原 owner 收到 CAS 冲突并保留草稿，不套用 A 的回执或自动覆盖；表单与 JSONC 不再维护第二份 B 草稿

#### Scenario: 继承对象的删除与数组覆盖
- **WHEN** 用户移除本层 Agent 定义、恢复 Provider 继承，或在没有 Provider 数组的高层新建 Provider
- **THEN** 预先说明 Agent 可能重新露出下层同 ID 对象、Provider 恢复将删除整组覆盖、新数组将遮蔽低层列表；只修改选定来源并校验完整 candidate，不复制脱敏 effective、不新增 tombstone 或级联删除其他层。保留继承列表的编辑应定位其实际可写来源

### Requirement: Provider 管理必须独立于会话并保护引用关系

产品 SHALL 提供 Provider 列表、详情、新建、编辑和移除本层定义入口，展示稳定 ID、模型、endpoint、处理方式、配置可用性和当前可确定的引用位置；不新增 Provider 显示名字段，创建后不提供稳定 ID 重命名。默认最小配置只要求已有协议必需参数，不自动推导输出限制、采样或 reasoning 覆盖。新增／编辑不自动发真实模型请求；可选诊断 SHALL 由用户显式执行并说明网络／模型调用。

删除校验 SHALL 沿原 owner 限定在完整 candidate 的当前配置引用（含 Agent primary／fallback）及该工作区的既有 SessionDefaults；共享来源的影响只报告已观察消费者，不承诺全部未知工作区均无引用。MUST NOT 扫描全部 Session／Thread、Job、Trace 或检查点、另建引用索引或跨 owner 事务。历史引用保留稳定 ID，后续访问已删除定义时明确报不可用并要求显式选择，不静默改绑、级联修改或回退另一模型；运行中快照保持原合同。

#### Scenario: 创建最小 Provider
- **WHEN** 用户在本地目标中填写模型、endpoint、凭据来源及处理方式并保存
- **THEN** 经正式来源提交并报告配置状态，不自行添加 max_tokens／max_output_tokens，也不因打开列表或保存而发模型请求

#### Scenario: 删除被引用 Provider
- **WHEN** 用户删除仍被 Agent 使用的 Provider
- **THEN** 显示原 owner 提供的冲突及引用对象，保留有效配置；不自动选择另一个 Provider 或改写运行中 Job

#### Scenario: 默认值引用与历史引用分别处理
- **WHEN** Provider 被当前 fallback 或工作区会话默认值引用，或仅被某个历史 Session 引用
- **THEN** 前者由所属 owner 报引用冲突；仅历史引用不要求扫描或重写历史，后续具体 Session 访问不存在定义时显示不可用并要求显式选择，不自动切换模型

#### Scenario: 删除或重排 Provider 保持剩余密钥归属
- **WHEN** 用户删除数组中排在 B 前的未被引用 Provider A，或明确调整数组顺序
- **THEN** 同一次原来源 CAS 提交保持 B 的密钥仍归稳定 ID 和 endpoint 均未变的 B，不按旧索引借用 A 的密钥；改变 B 身份／endpoint 时要求明确新凭据，不为数组变动要求重新输入所有未变对象的密钥

此恢复规则 SHALL 直接完善原 source restore／preflight，仅允许 `llm.providers[].api_key` 原占位定位同一来源原文旧项，核原占位、唯一 ID 及 endpoint 后还原到新索引。其他秘密路径保持原约束；跨来源占位、重复 ID 或不匹配值明确拒绝，不新增 CRUD 专用 writer、事务或秘密持久状态。表单与 JSONC 适用同一规则。

### Requirement: 秘密与登录必须归属目标且不进入普通展示存储

Provider、插件、MCP 和连接凭据 SHALL 分别沿各自 owner 的秘密输入或既有显式环境引用／登录合同处理，不能因共用 UI 就互换引用格式。界面 SHALL 显示是否配置、来源及错误而不回显真实值；秘密不得进入浏览器持久草稿、普通日志、诊断导出或跨目标复制。不将插件专用秘密写入口冒充全域凭据库，不新增系统密钥链或认证 Provider。

Provider 密码输入 SHALL 仅增加非持久化输入交互：字面量仍由选定 Workspace 配置来源的原 writer 经同一次预检／CAS 保存至既有 `api_key` 字段；`${ENV_NAME}` 继续是原支持的环境引用，不新增 Provider 私有引用格式、秘密数据库或另一写入 owner。输入前先结清原脏来源；确认期间的值只能作为短暂提交输入，不写入可备份的普通文本模型；预检、确认及成功／失败回执保持原脱敏格式。保存请求发出前取消清除输入且零提交；已发出后关闭／断连不能保证撤回，SHALL 沿原 submission key 只读查询 edit 回执，按提交身份、`source_saved` 和文件版本确认结果，不能比较明文请求与脱敏 JSONC、重发未知请求或另存秘密摘要。重开保留非秘密查询身份，明确原结果前不得换键重投；不把结果未知报告为取消成功。原配置 writer 依据原对象身份及 endpoint 恢复秘密占位；对象／地址改变不能从旧位置借用秘密，应要求明确新输入或引用。既有目标侧配置保存字面量的合同保持，不宣称已加密或自动迁移已有密钥；缺少的其他实体秘密能力只显示准确支持边界。

#### Scenario: 远端秘密输入与取消
- **WHEN** 用户向 B 的 Provider 输入秘密后确认，或在保存请求发出前取消
- **THEN** 确认只提交 B 原 owner，保存前取消不提交；后续页面、缓存及诊断不含秘密正文，A 的环境变量不被当作 B 的值

#### Scenario: 密码已保存但响应丢失
- **WHEN** B 已完成来源保存，而客户端连接中断或关闭页面未收到回执
- **THEN** 沿原键查询确认原提交事实，重开仍只查询；不以明文与脱敏文本不同判定未保存，不重发 PUT、不转投 A；来源后来变化时分别显示原提交结果与当前版本

#### Scenario: 修改已有 Provider 而不替换密钥
- **WHEN** 用户编辑 B Provider 的非秘密参数并保留原脱敏占位，或改变其对象身份／endpoint
- **THEN** 相同绑定经原 writer 保留既有秘密且不回显；身份／地址改变时拒绝沿旧占位借用秘密，要求明确凭据输入或目标侧环境引用，不另写一份 Provider 秘密记录

### Requirement: Agent 管理必须保持定义与运行选择分离

产品 SHALL 提供 Agent 列表、详情、定义编辑、创建和移除本层定义，管理提示词、Provider 引用和现有工具配置，并显示默认值的来源与影响范围；稳定 ID 创建后不提供重命名，已有显示名可编辑。配置定义及 `default_agent` SHALL 沿原 source writer；工作区会话默认 Agent／Provider SHALL 沿原 SessionDefaults API／存储；当前 Session 选择 SHALL 沿原 Session owner，与 Composer 共用对应操作，不迁入同一 JSONC、双写或新增默认值协调器。定义变更遵守原后续 Job 生效规则，不能重建当前执行或批量重写已有会话。Agent 可删除性沿完整 candidate 当前引用及该工作区默认值检查，继承恢复与历史引用遵守上文同一边界，不维护第二引用索引或全历史扫描。

#### Scenario: 修改默认值与既有执行并存
- **WHEN** A 中已有会话运行，用户修改工作区默认 Agent 或某 Agent 的配置
- **THEN** 默认值对原合同规定的新会话／后续 Job 生效，既有绑定和正在运行的执行保持，不将当前 Composer 选择伪装成全局默认

### Requirement: 工具管理必须复用真实目录与既有策略

工具管理 SHALL 展示所属 Agent／工作区、来源插件或内置组、只读工具说明与参数 schema、可见性、执行状态及已有策略，复用现有选择与测试 owner。定义策略走原配置来源；两个运行时开关走工作区 ToolSelectionStore，按 `agent_id` 保存，影响该工作区使用同一 Agent 的会话，不能标为当前 Session 私有开关、迁入来源 JSONC 或当作插件启停。静态策略锁定、插件实例／MCP可用性分别展示，开工具不自动启动插件。

selection PATCH 的回执只含 changed 项，SHALL 按稳定 `tool_id` 完整替换对应实体，其余目录保持；失败沿原 owner 重读，不将部分回执当成完整目录，不为成功更新强制追加全目录读取。默认没有确认要求时 MUST NOT 新增审批中心、轮询审批或每会话后台检查；显式配置的既有确认语义不因页面整理被移除或启用。

模型工具测试 SHALL 由用户先明确选择非空 Provider 范围及已有重复次数，显示实际目标和可能产生模型调用／费用后再显式启动；不在浏览列表时调用工具或模型。不改变原后端空 `provider_ids` 表示全部的合同，新管理 UI 不以空列表表示当前模型。进度沿发起工作区与 run_id 查询；关闭页面仅结束本页观察，不宣称已取消后端测试、不自动重发启动。

#### Scenario: 打开多个 Thread 的工具页面
- **WHEN** 用户打开六个 Thread 并查看其中一个工具目录
- **THEN** 仅按所查看范围读取真实目录，不产生六套审批轮询，不在关闭页面后继续执行工具探测

#### Scenario: 同一工作区 Agent 的工具开关
- **WHEN** 同一工作区两个会话使用相同 Agent，用户从其中一个入口关闭某工具
- **THEN** 说明这是该工作区 Agent 的运行时覆盖，按原策略及目录发布边界生效；PATCH 只返回该工具时，其余行保持，不修改插件 enabled 或伪造 Session 私有配置

#### Scenario: 显式执行模型工具测试
- **WHEN** 用户选择 B 工作区 Agent 的某个 Provider 和重复次数并启动测试，随后切到 A 或关闭页面
- **THEN** 请求只携已选择的非空 Provider 列表和原 B owner；重读只查询 B 原 run_id，不默认测试全部 Provider、不重发启动或假报取消

### Requirement: Skill 与指令管理必须区分来源权限和实际使用版本

产品 SHALL 提供工作区及 Gateway 已支持的 Skill 来源列表、详情、来源配置、已有显式刷新和来源错误，并用正式 URI 打开实际 Skill／AGENTS 资源。来源配置沿各自 source writer；本机 Gateway refresh 与 Workspace 来源 refresh 分别沿原 owner。远端 Gateway 来源的联邦投影目前只读，不能将查看权限当作刷新权限或新增联邦刷新 RPC。刷新来源、插件开关及 Thread 跟踪是独立操作，不新增来源启用账本。

插件包 Skill SHALL 沿安装版本贡献只读打开；已有可写 Skill／AGENTS 仅按实际资源 owner 的写能力编辑。无写能力时明确说明，不提供虚假的副本按钮；本变更不新增包 Skill 复制／物化流程或副本 owner。来源发布、Thread 跟踪与当前请求使用的封存版本分别展示，不将刷新成功宣称为运行中上下文已替换；不恢复 Agent memory。

#### Scenario: 查看只读插件 Skill
- **WHEN** 用户打开安装包内 Skill 并希望编辑
- **THEN** 只读并显示包版本及所属插件，不自动复制或生成可编辑副本；已有可写资源按原 URI 能力另行打开，不改包贡献或注入 Thread

#### Scenario: 查看远端 Gateway Skill 来源
- **WHEN** 用户从远端工作区查看其 Gateway Skill 来源的只读联邦投影
- **THEN** 展示原允许的状态及资源，不发送 Gateway refresh、不回退到本地 Gateway 刷新；Workspace 自有来源的显式刷新仍由原 Workspace owner 执行

#### Scenario: 刷新来源期间存在执行
- **WHEN** 用户刷新工作区 Skill 来源，当前 Turn 已封存上下文
- **THEN** 显示新来源及刷新结果，当前执行保持其封存来源，后续允许边界才采用更新
