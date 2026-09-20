## ADDED Requirements

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

配置诊断接口 SHALL（必须）同时报告 JSONC 来源摘要、SQLite 记录摘要、当前有效来源、文件/记录 revision 或 digest、active/pending 状态及最近失败原因。诊断接口的重新读取 MUST（必须）只用于观测，不能绕过正常候选提交流程改变运行时配置。

#### Scenario: JSONC 已迁移到 SQLite
- **WHEN** 用户查询 Gateway 或 Workspace 配置诊断
- **THEN** 响应明确显示 SQLite 为当前有效可变来源、JSONC 的最近导入摘要以及是否存在待应用文件变化

#### Scenario: 候选失败后查询状态
- **WHEN** 最近一次配置候选因为 schema 或业务预检失败
- **THEN** 诊断响应保留旧 active revision，并返回失败来源、路径和错误，不返回伪造的成功状态

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

配置候选 SHALL（必须）以整体变更集合为原子应用单位。若候选因需重启、应用失败、冲突或校验失败而未成为 active，则 `applied_paths` MUST（必须）为空；其中整体进入 pending 的候选 MUST（必须）将全部 changed paths 放入 `deferred_paths`，不得把可热更新路径误列为已应用。只有 active snapshot 已提交且对应生效策略满足时，路径才能列入 `applied_paths`。

#### Scenario: 混合修改整体待重启
- **WHEN** 一个候选同时修改可热更新模型字段和需重启 MCP 字段
- **THEN** 结果为 `restart_required`，`applied_paths=[]`，`deferred_paths` 等于全部 changed paths

#### Scenario: 后续 Job 生效
- **WHEN** 模型字段已提交到 active，但运行中的 Job 保持旧快照
- **THEN** 结果为 `applied`，该模型路径列入 `applied_paths`，并通过策略/activation scope 标明从后续 Job 生效，而不是伪装成当前 Job 已改变

#### Scenario: 候选被拒绝
- **WHEN** 候选校验或 CAS 失败
- **THEN** 结果为 `rejected` 或 `conflict`，`applied_paths=[]`、`deferred_paths=[]`，错误字段包含失败路径和原因

#### Scenario: 状态提交后 relay 崩溃
- **WHEN** SQLite 事务已经提交 active/pending 状态，但 SSE relay 在发布前崩溃
- **THEN** outbox 保留未发布事件，relay 恢复后按 cursor 发布；不得出现状态已变但事件永久丢失

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
