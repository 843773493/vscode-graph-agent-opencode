## Why

当前 Workspace 后端虽然已经监听 JSONC 文件，但 Gateway 没有等价的配置重载机制；同时可变配置迁移到 SQLite 后，用户直接编辑 `gateway.jsonc`、用户级 `workspace.jsonc` 或工作区 `.boxteam/workspace.jsonc` 的行为不透明。需要参考 VS Code 的“文件变化 → 防抖重载 → 发布配置变化事件”模型，并补齐可恢复的配置提交协议：pending 必须能通过 runtime 启动契约和健康响应证明实际加载，配置应用必须可并发 claim、可 fencing、可从崩溃恢复，JSONC/SQLite/API/UI 冲突和不可回滚副作用必须有明确结果。

## What Changes

- 为 Gateway 和 Workspace 建立统一的配置变更流水线：监听文件、接收 API/UI 修改、生成候选快照、校验、计算有效配置差异并原子提交。
- 补齐 `candidate_validated`、`pending_restart`、`applying`、`active`、`rejected`、`discarded`、`conflict` 和 `recovery_required` 状态机，持久化候选配置、来源层、基线 revision、层 digest、持久化位置和激活/丢弃/合并规则。
- 为每次候选和应用尝试分配 `candidate_id`、`attempt_id`、`apply_id`，并以 CAS claim、lease、generation fencing 和启动恢复协议串联 watcher、API/UI、重启操作。
- 区分单调递增的 `commit_revision`、有效配置 `effective_digest`、每个 JSONC 层 `layer_digest`、候选 digest 和事件 cursor；失败尝试可不产生 commit revision，禁止以内容 SHA 充当事件序号。
- 将 JSONC 文件、SQLite、API/UI 的写入协议统一为带 expected revision/digest 的 CAS，并增加文件读取前后 digest 检查，报告 TOCTOU 和并发冲突而不是静默覆盖。
- 增加独立于 Job 分区事件总线的配置事件 outbox，按配置域提供 `event_id`、cursor/replay、去重、结果枚举和明确的事件产生时机；状态和 outbox 必须同事务提交。
- 为每个配置字段或配置段声明生效策略：立即生效、仅后续 Job/会话/请求生效、排空后重启、必须重启或不可热更新。
- 规定混合修改不得部分应用：整体进入 pending 时 `applied_paths=[]`，所有变更路径进入 `deferred_paths`；只有 active snapshot 已提交的路径才能列入 `applied_paths`。
- 为 Gateway 增加配置监听、候选重载和受控运行时更新；registry 资源区分 `config`、`manual`、`system`、`remote_projection` 所有权，涉及隧道和服务生命周期的变化必须经过租约、引用和排空边界。
- 明确 SQLite 事务与 MCP、logger、进程、SSH 隧道等外部副作用的两阶段边界：优先恢复旧 runtime，恢复失败时进入 `recovery_required`，不得伪造成功。
- 明确 Workspace 新 generation 的启动契约和健康证明必须回显 candidate/revision/digest；Gateway 只能在证明匹配时 promotion。
- 明确 `source layers / desired state → active snapshot / pending candidate → candidate health proof → promotion active` 的物理存储和读取契约：Workspace 新 generation 只能通过 Workspace-owned `candidate_ref` 读取工作区 SQLite 中的完整候选，旧 generation 只能读取 active snapshot，Gateway 不得读取或修改 Workspace `.boxteam/` 数据库。
- 分离 `user`、`user_local`、`workspace` 和 runtime override 来源层的 payload、layer revision、layer digest 与 CAS；用户级 Workspace JSONC 的变更按 `fanout_id` 独立导入每个 Workspace，各 Workspace 分别产生自己的 revision、事件和冲突结果。
- 为 Gateway `workspaces` 配置元素引入持久的 `connection_id`，明确旧配置迁移和 host/username/key 变化时的原地更新、排空与对账行为；禁止用数组下标或可变连接字段推导目标身份。
- 为 API/UI 和 watcher 增加调用方 `idempotency_key`，使同一 candidate 的恢复重试只创建新的 apply attempt，不重复分配提交 revision 或重复发布同一配置结果事件。
- 候选持久化只保存 secret reference、secret version/binding digest 和脱敏候选；事件、诊断、健康证明及 Gateway 委托均不得包含解析后的 provider API key 或完整秘密值。
- 为配置诊断接口和测试补充有效来源、revision、changed paths、应用结果、restart required 与冲突信息。
- 增加文件/API 冲突、并发修改、重复 watcher、重启失败、registry 原子对账、远程委托和进程恢复的验收矩阵，并覆盖 pending 候选真实加载证明、用户级 JSONC fan-out/冲突及 `connection_id` 身份迁移。
- 补充配置来源/权威存储矩阵、状态/结果/revision/candidate_id 对应关系、Gateway 各 runtime 的 prepare/apply/health/promotion/rollback 矩阵。
- 为 `restart_gateway` 增加 Gateway 自身的 pending 启动契约：区分普通崩溃恢复与用户确认的 pending 应用，持久化 Gateway generation、candidate_ref、health proof 和 fencing token，并由稳定 supervisor 在新旧 Gateway 之间执行保留、切换与恢复。
- 规定任何 active promotion 前都必须对所有来源层重新执行 `base_layer_revision`/`base_layer_digest`/source generation CAS；外部副作用期间发现 source 变化时不得 promotion，必须补偿并重建候选或进入 conflict/recovery_required。
- 定义 JSONC 层的 `present`、`absent` 和 tombstone 语义，覆盖删除、重命名、临时文件、恢复文件、备份及删除后的优先级合并和重启判断。
- 固化 active snapshot 的持久化记录结构、唯一键、完整脱敏 payload、来源基线、secret binding、promotion 事务和进程崩溃恢复规则，禁止只依赖进程内快照。
- 保持用户配置的 `api_key` 契约：`${ENV}` 在导入边界规范化为内部 `secret_ref`，字面量 key（本地模型 dummy key、临时 apikey）按原文持久化并在诊断侧脱敏；明确 `${ENV}`、secret rotation、resolver 失败和旧 SQLite 记录（含不可逆 `literal-sha256:` 摘要）的迁移结果。
- 将 Gateway `connection_id` 迁移定义为可恢复的旧版本读取、旧 schema 校验、生成持久 ID、原子备份写入、新 schema 校验和 registry generation 建立流程，覆盖崩溃、重试、注释和旧程序回滚。
- 由单一 source owner 持久化单调 `source_generation` 和 append-only source journal；`fanout_id` 关联 journal 事件，digest 只负责内容去重，必须区分 `A → B → A` 和停止 Workspace 的追赶。
- 为 registry batch、manual CRUD 和 active promotion 增加 registry revision CAS、事务边界、恢复记录及 `workspace_id`/`connection_id`/remote target 的唯一命名空间，禁止整表删除重插入覆盖并发修改。

## Capabilities

### New Capabilities

### Modified Capabilities

- `configuration-bootstrap`: 明确定义配置文件变化、候选快照重载、配置来源与运行时生效状态。
- `configuration-domains`: 扩展 Gateway/Workspace 的独立重载边界和配置变化事件契约。
- `configuration-evolution`: 增加配置变更诊断、冲突处理和有效配置 revision 的可观察性。
- `sqlite-config-storage`: 定义 JSONC 文件层与 SQLite 可变状态之间的同步、冲突和权威来源规则。
- `managed-backend-lifecycle`: 为需要重启的 Workspace 配置提供排空、重启、失败回滚和真实状态反馈。
- `remote-gateway-federation`: 约束远程 Gateway 配置变化只能通过拥有者 Gateway 应用，并保持联邦连接边界。

## Impact

- 影响 `app/services/infrastructure/config/`、Workspace `app/main.py`、Gateway `app/gateway/config.py` 与 `app/gateway/main.py`。
- 影响 `app/container.py` 中启动时构造的 MCP、Job、Terminal、Browser、Gateway 连接等运行时组件，可能需要引入配置订阅或显式重启分类。
- 影响配置诊断 API、独立配置事件 API/SSE、Gateway/Workspace 生命周期 API、`configs/*_schema.jsonc` 及相关单元、集成和真实运行时测试；Gateway `workspaces` 元素需要增加稳定 `connection_id` 及一次性旧配置迁移。
- 不改变 Gateway 读取工作区业务配置的所有权边界；不把 Gateway 配置写入工作区 `.boxteam/`。
