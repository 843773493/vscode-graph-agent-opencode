## Purpose

定义用户级与工作区级配置的独立版本迁移、内置工具稳定标识、历史工具迁移、配置提交前预检以及诊断命令。
## Requirements
### Requirement: 配置文件独立版本迁移
系统 SHALL 为用户级与工作区级配置分别维护 `config_version`，并在合并配置前按各自版本顺序迁移到当前版本。

#### Scenario: 用户配置和工作区覆盖版本不同
- **WHEN** 用户配置与工作区配置分别处于不同旧版本
- **THEN** 系统分别迁移并原子写回两个文件，再按既有优先级合并当前版本配置

#### Scenario: 重复执行迁移
- **WHEN** 当前版本配置再次执行迁移
- **THEN** 文件内容保持不变且命令报告无需迁移

### Requirement: 内置工具使用稳定标识
持久化配置中的内置工具 SHALL 使用稳定 `tool_id`，不得依赖 Python 模块或工厂函数路径；自定义扩展 MAY 继续使用显式 factory。

#### Scenario: 内置工具源码重构
- **WHEN** 内置工具实现移动到新的模块或函数
- **THEN** 未变化的 `tool_id` 配置无需迁移即可解析到新实现

### Requirement: 迁移旧会话历史工具
迁移器 SHALL 将旧的会话历史读取、grep 和 JSONL 工具声明替换为 `read_context` 与 `search_context`，且不得修改会话目录或历史事件。

#### Scenario: 旧工作区配置包含三个历史工具
- **WHEN** 工作区配置包含已删除的三个 session history factory
- **THEN** 迁移结果只包含 `read_context` 与 `search_context`，旧会话可使用当前配置重试

### Requirement: 配置提交前预检工具
启动和热重载 SHALL 在提交候选快照前检查内置工具 ID、自定义 factory 导入、工具名唯一性和声明结构；无效候选 MUST 报告来源文件和配置路径且不得进入运行时。

#### Scenario: 自定义工厂函数不存在
- **WHEN** 候选配置引用模块中不存在的 factory 属性
- **THEN** 配置加载立即失败并指出配置来源与属性路径，而不是在发送消息后失败

### Requirement: 提供迁移与诊断命令
配置 CLI SHALL 支持迁移用户配置、指定工作区配置和诊断配置，不得通过强制重建覆盖用户自定义值。

#### Scenario: 诊断旧配置
- **WHEN** 用户运行配置诊断命令
- **THEN** 命令报告当前版本、待迁移步骤、工具解析结果和具体错误位置，不修改文件

### Requirement: 配置变化必须可观察并具有稳定 revision

Gateway 与 Workspace SHALL（必须）为每次配置尝试维护唯一 `candidate_id` 和 `attempt_id`，并为已持久化候选维护 active/pending revision、来源、changed paths、已应用路径、延后路径、重启要求和错误信息。每次 API/UI 或 watcher 逻辑提交还 MUST（必须）带有稳定的调用方 `idempotency_key`；该 key 在配置域内绑定一个逻辑 candidate/提交结果，不能把每一次 attempt 当成新提交。配置变化事件 MUST（必须）携带配置域、candidate_id、attempt_id 以及可用时的 revision；失败的 rejected/conflict/apply_failed 尝试没有持久化提交时，其 `commit_revision` MUST（必须）为空，不能为了填充事件字段而递增提交 revision。

#### Scenario: 成功热更新
- **WHEN** 候选配置校验通过且全部变化可在当前 runtime 应用
- **THEN** 系统递增 active revision，发布包含 changed paths 和 applied paths 的配置变化事件

#### Scenario: 需要重启
- **WHEN** 候选配置校验通过但包含需重启变化
- **THEN** 系统不递增 active revision，发布 pending revision、restart required、changed paths 和原因

#### Scenario: 配置内容未改变
- **WHEN** 文件系统事件发生但合并后的有效配置与 active snapshot 相同
- **THEN** 系统不产生新的 revision 或配置变化事件

#### Scenario: 同一逻辑提交重试
- **WHEN** API/UI、watcher 或恢复流程使用同一个 `idempotency_key` 重试同一候选
- **THEN** 系统返回原 candidate 和已记录结果，不再次分配逻辑 commit_revision，也不重复写入相同结果的配置事件；必要的实际执行只能创建新的 attempt_id/apply_id

#### Scenario: 不同逻辑提交具有相同内容
- **WHEN** 两个不同 idempotency_key 产生相同 candidate digest
- **THEN** 系统可以按 digest 返回 `unchanged` 或复用已激活候选，但不得把 digest 当作 commit_revision、event cursor 或调用方幂等键

### Requirement: 配置诊断必须揭示真实来源和应用状态

配置诊断接口 SHALL（必须）同时报告 JSONC 来源摘要、SQLite 记录摘要、当前有效来源、文件/记录 revision 或 digest、active/pending 状态及最近失败原因。来源正文编辑的结果 SHALL 分别报告文件保存、SQLite 层物化和 active／pending／最近失败事实，返回原候选及幂等提交身份；文件写入成功不得被表述为全部资源已生效。诊断接口的重新读取 MUST（必须）只用于观测，不能绕过正常候选提交流程改变运行时配置。

#### Scenario: JSONC 已迁移到 SQLite
- **WHEN** 用户查询 Gateway 或 Workspace 配置诊断
- **THEN** 响应明确显示 SQLite 为当前有效可变来源、JSONC 的最近导入摘要以及是否存在待应用文件变化

#### Scenario: 候选失败后查询状态
- **WHEN** 最近一次配置候选因为 schema 或业务预检失败
- **THEN** 诊断响应保留旧 active revision，并返回失败来源、路径和错误，不返回伪造的成功状态

#### Scenario: 来源文件保存后候选等待重启
- **WHEN** 显式来源保存成功，而有效变化包含原生效策略要求重启的字段
- **THEN** 结果显示来源已保存、原 active 保留及实际 pending／restart required；重读或同幂等请求不得伪造 promotion、重复副作用或另一逻辑提交

### Requirement: 配置事件必须使用独立的可回放事件流

Gateway 与 Workspace SHALL（必须）分别维护独立于 Job 分区事件总线的配置事件流。每个事件 MUST（必须）包含唯一 `event_id`、单调递增的域内 `cursor`、配置域、`candidate_id`、`attempt_id`、可为空的 `commit_revision`、active/pending revision、来源、changed paths、`result`、`activation_scope`、`applied_paths`、`deferred_paths` 和错误或原因；cursor MUST NOT（不得）由任意内容 digest 代替。

`result` 的唯一枚举和语义为：`applied` 表示 active promotion 成功；`restart_required` 表示候选已持久化但等待重启；`restart_failed` 表示重启失败且旧 runtime 已恢复；`apply_failed` 表示热应用失败且补偿成功；`rejected` 表示解析/schema/业务校验失败；`conflict` 表示 CAS/TOCTOU/owner 校验失败；`discarded` 表示用户显式丢弃；`recovery_required` 表示副作用和旧 runtime 均无法安全恢复；`unchanged` 表示候选与 active 内容相同且不产生配置事件。`deferred` 不是 result，而是 `deferred_paths` 或 `activation_scope` 的状态描述。

事件流 SHALL（必须）支持按 cursor replay、明确的 cursor 过期/需要快照结果以及基于 `event_id` 的消费者去重。配置状态和事件 outbox MUST（必须）在同一配置域 SQLite 事务中写入；SSE relay 只发布已提交的 outbox，不得在业务事务之外先发事件。active 事件在 active promotion 完成后进入 outbox，pending/rejected/conflict/discarded/recovery_required/restart_failed/apply_failed 事件在状态记录和原因原子提交后进入 outbox；解析中的候选不得提前发布成功事件。outbox 至少以 `(domain, idempotency_key, logical_result_state)` 或等价的 candidate/result 唯一约束防止同一逻辑提交重复写事件；恢复 retry 的新 attempt 不得绕过该约束。

#### Scenario: 配置事件重放
- **WHEN** 客户端使用有效 cursor 订阅并请求 replay
- **THEN** 系统按 cursor 顺序返回之后的配置事件；若 cursor 已超出保留窗口，则返回明确的 snapshot_required，而不是静默从当前事件开始

#### Scenario: 重复 watcher 事件
- **WHEN** 同一来源 digest 在 watcher、API 重试或进程恢复中被重复处理
- **THEN** 系统只保留一个对应提交和 event_id，消费者可使用 event_id 安全去重

#### Scenario: Job 事件与配置事件隔离
- **WHEN** 客户端订阅配置事件
- **THEN** 客户端无需绑定 job_id 或 session_id 即可获取配置状态，且 Job 事件流不承担配置事件的 replay/cursor 语义

### Requirement: 配置事件结果必须准确表达整体应用边界

配置候选 SHALL（必须）以整体变更集合为原子应用单位。若候选因需重启、应用失败、冲突或校验失败而未成为 active，则 applied_paths MUST（必须）为空；其中整体进入 pending 的候选 MUST（必须）将全部 changed paths 放入 deferred_paths，不得把可热更新路径误列为已应用。只有 active snapshot 已提交且对应生效策略满足时，路径才能列入 applied_paths。MCP 热应用 MUST（必须）在连接与目录完成受控提交后才报告 applied，并区分后续目录激活边界与已有调用；切换后的退役连接清理失败 MUST（必须）保留已切换事实并单独报告真实清理状态。

#### Scenario: 混合修改整体待重启

- **WHEN** 一个候选同时修改可热更新模型字段和按当前策略仍需重启的字段
- **THEN** 结果为 restart_required，applied_paths=[]，deferred_paths 等于全部 changed paths

#### Scenario: 后续 Job 生效

- **WHEN** 模型字段已提交到 active，但运行中的 Job 保持旧快照
- **THEN** 结果为 applied，该模型路径列入 applied_paths，并通过策略/activation scope 标明从后续 Job 生效，而不是伪装成当前 Job 已改变

#### Scenario: 候选被拒绝

- **WHEN** 候选校验或 CAS 失败
- **THEN** 结果为 rejected 或 conflict，applied_paths=[]、deferred_paths=[]，错误字段包含失败路径和原因

#### Scenario: 状态提交后 relay 崩溃

- **WHEN** SQLite 事务已经提交 active/pending 状态，但 SSE relay 在发布前崩溃
- **THEN** outbox 保留未发布事件，relay 恢复后按 cursor 发布；不得出现状态已变但事件永久丢失

#### Scenario: MCP 热应用成功

- **WHEN** 全部路径可热应用的候选完成 active promotion 和 MCP 受控提交
- **THEN** 对应路径列入 applied_paths，deferred_paths 为空，结果说明目录从允许激活边界生效而非重写旧调用

#### Scenario: 切换完成后清理失败

- **WHEN** 新连接已成为 active，旧连接随后清理失败
- **THEN** 配置事件保留已应用 revision，并报告退役资源的真实失败或待核实状态，不把应用事实倒写成 applied_paths 为空

### Requirement: 配置生效策略必须具有唯一且安全的路径规则

系统 SHALL（必须）维护唯一的配置生效策略事实来源。策略 MUST（必须）支持精确 JSON Pointer 路径、明确的对象键通配规则和按声明 identity key 区分的数组元素；更具体路径优先，重叠或同优先级规则 MUST（必须）在启动/构建时失败。没有声明稳定 identity key 的数组 MUST（必须）按整个数组路径处理，不得猜测元素身份。schema 注释、诊断和测试 SHALL（必须）从该策略生成或校验一致。

对于 schema 中合法但尚未登记的字段，默认策略 MUST（必须）是 `restart_workspace` 或 `restart_gateway`，由配置域选择对应的安全默认值，而不是即时应用；`restart_required` 只允许作为结果/状态，不得作为策略名称。不属于 schema 的字段仍按 schema 错误拒绝。

#### Scenario: 精确路径覆盖
- **WHEN** 同时存在 `runtime.agent` 与 `runtime.agent.run.mode` 策略
- **THEN** `runtime.agent.run.mode` 使用更具体的路径策略，其他 `runtime.agent` 子路径继续使用父级策略

#### Scenario: 数组元素身份未声明
- **WHEN** 未声明 identity key 的数组任意元素发生变化
- **THEN** changed paths 只报告整个数组路径，并按该路径策略统一决定应用或延后

#### Scenario: 合法字段尚未登记
- **WHEN** 新版本 schema 增加合法字段但策略表尚未登记
- **THEN** 该字段默认进入对应配置域的 restart_workspace 或 restart_gateway，诊断明确报告策略缺失，不能即时热应用

### Requirement: Gateway connection_id 迁移必须先校验旧版本并可恢复

Gateway MUST（必须）按版本读取和迁移旧 `config_version: 1` 文档：先执行旧 schema 校验，再生成 migration plan 和持久 migration_id，写入精确原始字节备份，原子持久化 connection_id，执行新 schema 校验，建立 registry generation，最后标记 completed。旧版本校验失败、元素映射有歧义、重复 ID 或任一必需 source layer 仍为 partial 时不得 promotion 新 active generation。

迁移记录至少必须绑定 old digest、new digest、backup location、每个 source layer 的状态和生成的 connection_id。写入必须保留 JSONC 注释并使用 source-preserving patch；不能安全保留原始文档时应在替换前失败。恢复器依据 migration_id、digest 和 journal 选择继续、幂等重试或恢复原始备份；重复执行不能生成新 ID。旧程序回滚必须先恢复精确 v1 备份，再启动旧 loader，不得把新 schema 文档交给旧程序。

#### Scenario: 旧 Gateway 配置首次迁移
- **WHEN** v1 `workspaces` 数组缺少 connection_id 且每个元素可无歧义识别
- **THEN** 系统先通过 v1 schema 校验，备份原始文档并一次性持久化 ID，再通过新 schema/registry generation 校验后完成迁移

#### Scenario: 迁移中途崩溃并重复执行
- **WHEN** 迁移在备份、ID 写入或新 schema 校验之间崩溃后再次启动
- **THEN** 恢复器依据 migration_id、old/new digest 和备份状态继续同一迁移、幂等重试或恢复 v1 原文；不得生成第二组 ID 或把部分 source layer 标记为完成

#### Scenario: 注释保留与旧程序回滚
- **WHEN** v1 JSONC 包含注释，或新版本需要回滚到旧 Gateway
- **THEN** 迁移保留注释；回滚从精确 v1 backup 恢复原始字节后再启动旧 loader

### Requirement: 插件热应用必须展示实际实例与配置 revision

Gateway 配置诊断 SHALL 区分配置来源已保存、active 配置及每个受管插件实际应用的 revision／plugin_id／instance_id。日志、遥测和统计的应用失败 SHALL 返回原实例、阶段及具体错误；部分成功不得伪报全成功，也不要求跨服务事务回滚或通过重启强迫应用。实例重启后 SHALL 从当前已确认设置初始化，不恢复已关闭的采样定时器。

#### Scenario: 一项诊断配置部分应用失败
- **WHEN** 同一热更新在 Terminal 已应用，在 Browser 明确失败，MCP 尚未就绪
- **THEN** 诊断分别报告三者实际状态，不假报采集已经全部关闭，不创建第二配置事实或把未就绪服务读查询当作启动指令

### Requirement: 插件启用期望与运行回执必须分别报告

插件配置提交 SHALL 返回原来源／候选及 enabled，运行 owner 分别报告精确插件和实例实际状态；启动或停止失败不伪报配置回滚／运行成功。旧配置 revision 的 ready／停止回执不得覆盖新期望，开关和重启串行且原进程退出核实前不重复启动。跨 owner 版本切换 SHALL 先预校验，再沿原 CAS 提交期望、撤新准入、停旧核退出／启新、按固定引用顺序发布；首次失败停止后续推进，保留各 owner 实际版本、阶段和部分结果，不跨 owner 补偿或自动重试。预校验失败保留旧发布，实际登记失败只清本次新增句柄，不重建已释放句柄；残留旧贡献只按旧版本诊断，版本不匹配的新调用不可用。期望提交后不自动回滚，原 active／pending 仍按各域既有 promotion 合同报告，不把期望当已生效或增加汇总账本／新恢复状态机。普通参数热应用不能隐式开启插件；需要重启的参数按原生效分类提示显式插件重启，不重启兄弟插件或 Backend。

#### Scenario: 已保存开启但启动失败
- **WHEN** enabled=true 已提交，插件入口启动失败
- **THEN** 配置保持已开启、实际状态 failed 并显示原原因，其他插件可用；用户显式重启该绑定才重试，普通工具和读取不重试启动

#### Scenario: 切版中已退出旧实例而新实例失败
- **WHEN** 新版本期望已提交且旧实例退出已核实，新实例启动失败
- **THEN** 诊断分别显示已提交期望、原 active／pending 事实和运行失败阶段；不自动恢复旧进程或重写来源，用户显式重启重新应用，选择旧版本须另作显式配置提交

### Requirement: 受支持旧版本自动完成停写与可恢复升级

软件启动或升级 SHALL 识别已有支持合同的旧版本，自动沿原生命周期停止写入、保留可恢复原件、由原 owner 迁移并校验，成功后恢复服务。已知且无需数据取舍的转换 MUST NOT 要求用户另行运行离线迁移命令。未知版本、歧义或需要数据取舍时 SHALL 保留原件并让用户决定；普通 I/O 或运行故障 SHALL 明确失败，不伪报完成。普通配置 loader 和业务请求 MUST NOT 兼容双读或偷偷执行结构迁移。

SQLite 升级 SHALL 在锁内核版本，备份包含已提交 WAL；有序链及升级校验 MUST 在提交前完成，失败回滚且不开放写入。共享换代 MUST NOT 在旧实例仍运行时升级其存量旧结构。同版本再次启动 MUST 不重复备份、重写或执行全库校验。恢复 SHALL 沿原 journal 的冻结计划和方向，不能把显式回退自动反转成正向升级。

#### Scenario: 已知旧配置正常启动
- **WHEN** 软件识别无需取舍的受支持旧配置
- **THEN** 自动停止原写入，备份、迁移、校验并启动当前服务，无需用户另跑迁移命令

#### Scenario: 未知版本或取值冲突
- **WHEN** 来源版本未知或迁移需要在冲突值之间取舍
- **THEN** 保留原件并明确报告待决定事项，不猜测、不覆盖、不开放部分结果

#### Scenario: 旧外观设置退役
- **WHEN** 旧 Gateway 外观仍为原默认主题 warm 且无自定义主题
- **THEN** 在保留原始备份后自动移除已退役字段；非默认值或自定义主题保留原件，并在停旧和首次写入前要求用户决定

#### Scenario: 用户批准的空旧资源账本
- **WHEN** 明确目标工作区存在精确空 records／leases 的旧 resources.json
- **THEN** 原 owner 在 Workspace 停写锁内保留原字节备份后退休旧文件；非空或其他形状在首次写入前拒绝并要求用户决定，不新增完整 JSON 账本迁移或运行时双读

#### Scenario: 迁移中断后再次启动
- **WHEN** 原正向迁移只发布部分资产便中断
- **THEN** 自动沿原冻结计划幂等续跑；已明确回退的记录继续保留该方向，不自动重新升级

#### Scenario: SQLite 后续步骤失败
- **WHEN** 已备份的有序升级链后续步骤或最终校验失败
- **THEN** 本次链和版本登记全部回滚，备份仍可核验，服务不开放写入

#### Scenario: 当前版本重复启动
- **WHEN** 当前数据已处于目标结构
- **THEN** 原正文及备份数量不变，启动不重复执行全库校验

### Requirement: 持久化格式转换按业务域和两端版本命名

新增持久化格式转换源码 MUST 位于 `<业务域 owner>/migrations/`；配置域 SHALL 沿用 `configs/migrations/<domain>/`。文件名 MUST 使用 `v<来源版本>_<来源格式>_to_v<目标版本>_<目标格式>.<实现语言后缀>`，明确两端版本和实际格式；格式名使用小写，版本中的点在 Python 模块名中以下划线表达。系统 MUST NOT 将目标固定为某种存储，也不因格式转换另建平行的 imports／recovery 目录或散落顶层模块。

两端版本 SHALL 来自已明确登记的本域数据格式／结构合同，不混同软件发布版本或数据库 schema 版本；无版本旧数据 MUST 先核实来源和版本归属，不能为命名虚构版本。执行顺序 MUST 由原 owner 清单显式登记，不扫描文件名推导。既有有序 SQL 名称和已登记顺序 MUST 保持；只有实际数据库结构变化才提升 schema 版本，不为命名整理新增迁移、框架或版本映射账本。

#### Scenario: 后续发布版换回先前格式
- **WHEN** 某业务域先从 JSONL 合同 v1 迁到 SQLite 合同 v2，后续发布版决定采用 JSONL 合同 v3
- **THEN** 两个转换分别命名为 `v1_jsonl_to_v2_sqlite.py` 和 `v2_sqlite_to_v3_jsonl.py`，位于该域 `migrations/`；第二步属于新的正向迁移，不复用 v1 或当成显式回退

#### Scenario: 同格式升级与既有 SQL 共存
- **WHEN** 某业务域新增 JSONL 合同 v3 到 v4 的转换，目录已有登记的 `001.sql`
- **THEN** 新转换命名为 `v3_jsonl_to_v4_jsonl.py`，既有 SQL 不重命名、不重编号，执行仍由原 owner 显式登记

#### Scenario: 格式未知且没有版本归属
- **WHEN** 旧文件没有可核验的数据合同版本
- **THEN** 保留原件并明确来源缺口，不擅自认定 v1，不因存在通用命名约定就自动纳入支持或创建转换文件
