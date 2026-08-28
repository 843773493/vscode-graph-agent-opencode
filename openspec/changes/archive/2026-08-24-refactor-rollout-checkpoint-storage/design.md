## Context

当前 change 已经确认不采用物理 chunk，也不再需要通过 JSONL 保存控制事件。新的持久化边界是每个会话 rollout 目录中的一个 `rollout.jsonl` 和一个 `index.sqlite`：JSONL 只保存不可变消息，SQLite 保存上下文、checkpoint、branch、fork、投影、提交和版本状态。

SQLite 是权威数据库，不是可以从 JSONL 任意重建的缓存。JSONL 丢失或 SQLite 损坏时都必须明确失败；如果 SQLite 损坏，系统必须从 SQLite 备份恢复完整的上下文控制状态，不能只根据线性消息伪造 rewind、fork、compaction 或历史 checkpoint。

## Goals / Non-Goals

**Goals:**

- 让 `rollout.jsonl` 只追加 `user`、`assistant`、`tool` 三种不可变 canonical message。
- 通过 SQLite 快速实现 checkpoint、任意历史、rewind、replay、fork、工具摘要和 reasoning 投影读取。
- 让消息正文只保存一份；SQLite 保存 offset、length、哈希和有界投影，不复制大型正文。
- 通过集中 schema 和 migration 管理所有 SQLite 表、字段、参数和版本。
- 保留 LangGraph `BaseCheckpointSaver` 的完整 checkpoint envelope、parent checkpoint、逐 channel values/versions、`versions_seen`、`pending_sends` 和 pending writes 契约。
- 允许崩溃丢失尚未提交的 JSONL 尾部，但已提交的 JSONL 与 SQLite 状态必须保持一致。

**Non-Goals:**

- 不保留 `segment-*.jsonl`、semantic segment、JSONL context control record 或 `manifest.json`。
- 不保留 `message_replace`、`message_truncate`、`message_key`、message revision、`supersedes_sequence` 或 `parent_segment_id`。
- 不从 rollout JSONL 重建完整的 branch、context view、checkpoint 或 fork 控制状态。
- 不在 JSONL 中存储完整 checkpoint、工具投影、控制记录或非 messages channel。
- 不用数据库表复制完整 messages 数组；context view 只能通过范围和视图引用表达。
- 不改变 Gateway 配置和 Web 默认渐进加载策略的既有产品要求。

## Decisions

### 1. 物理布局只有一个 rollout JSONL 和一个 SQLite

每个会话在稳定状态下的 rollout 目录固定为：

```text
<session>/rollout/
├── rollout.jsonl       # 唯一的 canonical message 文件，只追加
└── index.sqlite        # SQLite 权威数据库
```

SQLite 的 `-wal`、`-shm` 或临时 journal 文件属于 SQLite 运行时文件，不属于业务数据格式；它们不能被业务代码直接读取或作为 rollout 内容依赖。

显式离线 compaction 运行期间可以短暂创建带随机 ID 的 `.rollout.jsonl.compaction-*`、
`.index.sqlite.compaction-*` 临时备份；成功提交后必须删除。它们不是新的消息分片，
也不能成为正常读取、写入或目录扫描的入口。

`rollout.jsonl` 每行只包含稳定消息 envelope：

```json
{"sequence":42,"message_id":"msg-42","turn_id":"turn-7","role":"assistant","message":{"content":"我先检查文件","tool_calls":[]}}
```

`sequence` 是 JSONL 中的物理消息顺序，`message_id` 是该条不可变消息的具体 ID，`turn_id` 是所属 Turn，`role` 只能是 `user`、`assistant`、`tool`。消息正文、tool call 参数、tool result 正文和 reasoning block 都在该行的 `message` 中完整保存。

消息序号和消息 ID 是消息数据本身，不表示控制语义。控制语义全部进入 SQLite。

### 2. Canonical message 只追加且不修订

一次稳定的 LangChain `BaseMessage` 只写一条 JSONL。包含 text、reasoning blocks 和 `tool_calls` 的 `AIMessage` 保持一条 assistant 消息；工具结果使用 role 为 `tool` 的消息。

流式 assistant 或逐步组装的工具参数只在运行时内存中累积，完成稳定 checkpoint 或 Turn 后再追加完整消息。每个 chunk 不得重复追加一份完整 assistant 消息，否则会重新产生大 payload 的 O(n²) 空间增长。崩溃时未完成的流式消息可以丢失，下一次执行可以重新开始。

消息被用户编辑、replay 或 rewind 时，不修改旧 JSONL 行，也不生成 revision；系统追加新的消息行，并由 SQLite context view 决定旧后缀是否仍属于当前上下文。

### 3. SQLite 是权威控制数据库

SQLite 同时承担三类职责：

1. `messages`、Turn、tool、reasoning 的 offset 和轻量投影索引；
2. checkpoint、branch、context view、rewind、compaction、fork 和 pruning 的权威控制状态；
3. JSONL 与 SQLite 的提交边界、数据库 schema 版本和迁移记录。

SQLite 的核心数据不能全部依赖一个自由格式 JSON 字段。核心查询字段必须使用独立列；`payload_json` 只能保存尚未需要索引的扩展参数。

### 3.1 `RolloutCheckpointRuntime` 的工作区级共享实例

`RolloutStorage` 是 rollout JSONL 与 SQLite 的内部协调对象，不是业务层可以随意重复创建的无状态数据库连接包装。工作区后端通过唯一的 `RolloutCheckpointRuntime` 组装整套组件；业务层唯一允许持有的 rollout 数据入口仍然是 Runtime 暴露的 `RolloutCheckpointSaver`：

```text
AppContainer
└── RolloutCheckpointRuntime         # 当前工作区唯一组件组装根
    ├── RolloutStorage               # 协调 JSONL + SQLite
    ├── RolloutAppendWriter           # 受控写入组合
    ├── RolloutContextReader          # context view 读取组合
    ├── RolloutHistoryReader          # DTO 投影适配
    └── RolloutCheckpointSaver        # 业务唯一 rollout/checkpoint 数据入口
```

这里的“共享”必须是对象引用共享，而不仅仅是多个对象打开同一个 `index.sqlite` 路径。这样可以保证：

- Runtime 中的 Writer、Reader、历史 DTO reader 和 Saver 使用同一套 rollout 根目录解析、进程锁、连接配置和事务协调策略；
- Saver 的 checkpoint 恢复、Web 历史加载和 fork 上下文读取都遵循同一个 storage 级读快照边界；
- 对同一 workspace 内重复请求的锁、缓存失效和 `projection_epoch` 观察不会因为重复构造 storage 而分裂；
- 集成测试可以向 Runtime 注入临时 `RolloutStorage` 组合，并通过对象身份断言确认 Runtime 与 Saver 内部组件确实使用同一个实例；生产业务服务不得接收该 storage。

共享实例的边界是“一个工作区后端进程对应一个 sessions 根目录”，不是整个进程或所有工作区的全局单例。Gateway 不创建也不持有该对象；它只代理到目标工作区后端。多个工作区必须分别创建各自的 `RolloutStorage`，避免 session 路径、锁、SQLite 连接和缓存跨工作区污染。

`RolloutStorage` 内部可以为每次操作创建短生命周期的 SQLite connection/read transaction；共享的是 storage 的协调对象和配置，不要求所有请求共用同一条 SQLite connection。业务模块不得自行从 sessions 路径构造第二个 `RolloutStorage`，也不得绕过 saver、writer 或 reader 直接操作 JSONL/SQLite。

生产构造路径只创建并注入 `RolloutCheckpointRuntime.saver`；Runtime 显式把已创建的 `storage`、`writer`、`context_reader` 和历史 DTO reader 注入 Saver，避免 Saver 在生产路径隐式重复创建底层组件。Saver 仍保留未注入依赖时的自包含构造方式，供单元测试和独立工具使用；`RolloutStorage`、`RolloutAppendWriter` 和 `RolloutContextReader` 不得作为生产业务服务构造参数。

### 4. SQLite 表和字段设计

以下是本 change 的 SQLite v1 设计。每个字段后面的注释是该字段的规范作用；实现时表名、列名和枚举值应保持一致。

#### 4.1 `database_meta`

单行全局状态表，`singleton_id` 必须固定为 `1`。

```sql
CREATE TABLE database_meta (
    singleton_id INTEGER PRIMARY KEY, -- 作用：固定为 1，保证数据库只有一行全局状态；场景：打开数据库时用它确认 rollout 元数据完整
    rollout_id TEXT NOT NULL UNIQUE, -- 作用：标识当前 rollout；场景：cursor、SQLite backup 和 fork provenance 用它确认数据属于哪个 rollout
    session_id TEXT NOT NULL UNIQUE, -- 作用：关联宿主会话；场景：Workspace 通过 session_id 找到本目录的 rollout 和 SQLite
    schema_version INTEGER NOT NULL, -- 作用：标识 SQLite 表结构版本；场景：启动时决定是否执行 schema migration
    message_format_version INTEGER NOT NULL, -- 作用：标识 rollout.jsonl 消息 envelope 版本；场景：读取 JSONL 前确认当前 reader 能解析该格式
    database_state TEXT NOT NULL, -- 作用：表示 active/compacting/recovery_required/closed；场景：离线 JSONL 回收期间阻止业务读写，完整性失败时阻止业务继续读取
    last_commit_id INTEGER, -- 作用：指向最近成功的 storage_commits；场景：read snapshot 固定一次提交水位
    last_message_sequence INTEGER NOT NULL, -- 作用：记录最后一条已提交消息的 sequence；场景：分配下一条消息 sequence，不能用文件尾部猜测
    last_control_sequence INTEGER NOT NULL, -- 作用：记录最后一个已提交控制事件；场景：生成新的控制序号和校验控制事件链
    committed_jsonl_offset INTEGER NOT NULL, -- 作用：记录 JSONL 中已提交内容的结束字节偏移；场景：崩溃后截断或忽略该偏移之后的未提交尾部
    active_branch_id TEXT, -- 作用：仅作为默认 namespace 初始化时的 branch seed；场景：首次创建 namespace_state 时提供 branch-001，运行时不得把它当成所有 namespace 的 active branch
    projection_epoch INTEGER NOT NULL, -- 作用：保存全局创建时的初始投影版本；场景：数据库诊断和旧索引检查，运行时 cursor 版本以 checkpoint_namespace_state.projection_epoch 为准
    created_at TEXT NOT NULL, -- 作用：记录 rollout 创建时间；场景：审计、排序和诊断
    updated_at TEXT NOT NULL -- 作用：记录全局状态更新时间；场景：判断数据库是否有新提交
);
```

#### 4.1A `checkpoint_namespace_state`

按 LangGraph `checkpoint_ns` 保存 active branch 和 cursor 投影版本。一个 rollout 仍然只拥有一份 `rollout.jsonl` 和一份 `index.sqlite`，但同一 thread 可以有多个 checkpoint namespace；因此不能再用 `database_meta.active_branch_id` 作为全局 head。

```sql
CREATE TABLE checkpoint_namespace_state (
    checkpoint_ns TEXT PRIMARY KEY, -- 作用：标识同一 thread 内的 checkpoint 分区；场景：get_tuple/list/rewind/fork 只在请求 namespace 内选择状态
    active_branch_id TEXT NOT NULL, -- 作用：指向该 namespace 当前有效的 branch；场景：最新 checkpoint、around cursor 和 Turn anchor 都从该 branch 的 head_view_id 开始
    projection_epoch INTEGER NOT NULL DEFAULT 1, -- 作用：记录该 namespace 的逻辑 view 代数；场景：rewind、compaction、pruning 或 branch 切换后使该 namespace 的旧 cursor 明确失效
    created_at TEXT NOT NULL, -- 作用：记录 namespace 首次出现时间；场景：审计 LangGraph 子图或 namespace 的创建
    updated_at TEXT NOT NULL -- 作用：记录该 namespace head 或 epoch 最近更新时间；场景：诊断并发读取和 stale cursor
);
```

`checkpoint_namespace_state` 是 namespace head 的唯一权威来源。`branches`、`context_views` 和 `checkpoints` 仍使用全局唯一 ID，但所有通过 active head 的查询必须先按 `checkpoint_ns` 取得这里的 `active_branch_id`；不能从其它 namespace 借用 head，也不能用物理 `MAX(commit_id)` 代替逻辑 head。

#### 4.2 `schema_migrations`

记录 SQLite schema 的升级过程。升级只能通过集中 migration 执行。

```sql
CREATE TABLE schema_migrations (
    migration_id INTEGER PRIMARY KEY AUTOINCREMENT, -- 作用：标识一次迁移尝试；场景：审计某次升级是否执行过
    from_version INTEGER NOT NULL, -- 作用：记录迁移前版本；场景：防止把 v2 migration 错套到 v1 以外的数据库
    to_version INTEGER NOT NULL, -- 作用：记录迁移目标版本；场景：成功后写入 database_meta.schema_version
    migration_name TEXT NOT NULL, -- 作用：标识迁移逻辑；场景：错误报告中说明哪一步失败
    migration_checksum TEXT NOT NULL, -- 作用：校验迁移定义未被修改；场景：检测部署版本和执行版本不一致
    status TEXT NOT NULL, -- 作用：表示 running/completed/failed；场景：启动时发现 running 可进入恢复流程
    started_at TEXT NOT NULL, -- 作用：记录迁移开始时间；场景：诊断长时间锁等待
    completed_at TEXT, -- 作用：记录成功完成时间；场景：确认迁移事务已结束
    error_message TEXT -- 作用：保存失败原因；场景：迁移回滚后向用户显示明确错误
);
```

#### 4.3 `storage_commits`

协调 JSONL 文件写入和 SQLite 事务提交。

```sql
CREATE TABLE storage_commits (
    commit_id INTEGER PRIMARY KEY AUTOINCREMENT, -- 作用：标识一次跨文件提交；场景：checkpoint、messages 和 view 用同一 commit 证明原子提交边界
    transaction_id TEXT NOT NULL UNIQUE, -- 作用：标识写入事务；场景：崩溃恢复和重试时避免重复提交
    first_message_sequence INTEGER, -- 作用：记录本批第一条消息；场景：诊断本次提交写入了哪些 JSONL 行
    last_message_sequence INTEGER, -- 作用：记录本批最后一条消息；场景：与 database_meta.last_message_sequence 对账
    jsonl_start_offset INTEGER NOT NULL, -- 作用：记录本批 JSONL 起始偏移；场景：定位本次写入的物理字节范围
    jsonl_end_offset INTEGER NOT NULL, -- 作用：记录本批 JSONL 结束偏移；场景：只有 fsync 到这里后 SQLite 才能提交
    first_control_sequence INTEGER, -- 作用：记录本批第一个控制事件；场景：把 view/checkpoint 变化关联到同一提交
    last_control_sequence INTEGER, -- 作用：记录本批最后一个控制事件；场景：恢复控制事件水位
    jsonl_fsync_at TEXT, -- 作用：记录 JSONL 完成 fsync 的时间；场景：判断 SQLite 提交是否具备文件先行条件
    status TEXT NOT NULL, -- 作用：表示 prepared/committed/aborted；场景：启动恢复只接受 committed 状态
    created_at TEXT NOT NULL, -- 作用：记录提交开始时间；场景：诊断写入延迟
    committed_at TEXT -- 作用：记录 SQLite commit 时间；场景：区分已提交数据和崩溃时未完成的数据
);
```

#### 4.4 `compaction_runs`

记录唯一允许改变 JSONL 物理布局的离线 compaction 两阶段状态。它不是消息或控制
语义来源，而是为了在“替换 JSONL”和“提交新 offset”之间崩溃时恢复旧 JSONL 与旧
SQLite backup；完成后该行必须删除。

```sql
CREATE TABLE compaction_runs (
    compaction_id TEXT PRIMARY KEY, -- 作用：标识一次离线回收；场景：恢复时找到对应的临时 JSONL 和 SQLite backup
    status TEXT NOT NULL, -- 作用：表示 prepared/replaced；场景：prepared 表示尚未替换，replaced 表示文件已替换但新 offset 尚未完成提交
    old_file_name TEXT NOT NULL, -- 作用：保存旧 rollout.jsonl 的临时备份文件名；场景：compaction 中途失败时恢复原始消息文件
    temp_file_name TEXT NOT NULL, -- 作用：保存新 JSONL 临时文件名；场景：校验完整内容后再一次性替换正式文件
    index_backup_name TEXT NOT NULL, -- 作用：保存替换前 index.sqlite backup 文件名；场景：SQLite 已进入 compaction 状态但新索引未提交时回滚
    new_file_hash TEXT NOT NULL, -- 作用：保存候选 JSONL 的完整 hash；场景：判断正式文件是否确实是本次 compaction 产生的文件
    new_file_size INTEGER NOT NULL, -- 作用：保存候选 JSONL 字节数；场景：与 database_meta.committed_jsonl_offset 一起判断新索引是否已提交
    created_at TEXT NOT NULL, -- 作用：记录 compaction 开始时间；场景：诊断长时间占用临时备份的离线任务
    completed_at TEXT -- 作用：记录恢复清理完成时间；场景：审计 compaction journal 生命周期
);
```

#### 4.5 `control_events`

所有控制语义的 SQLite 审计日志。规范化状态表用于快速查询，该表用于保留不可变控制历史。

```sql
CREATE TABLE control_events (
    control_sequence INTEGER PRIMARY KEY AUTOINCREMENT, -- 作用：给所有控制事件建立单调顺序；场景：按时间重放 view/checkpoint 状态或定位某次控制提交
    control_id TEXT NOT NULL UNIQUE, -- 作用：控制事件的稳定 ID；场景：重试 rewind/fork 请求时避免重复创建控制状态
    control_kind TEXT NOT NULL, -- 作用：区分 view_created/checkpoint_created/rewind/compaction/fork/prune 等操作；场景：历史审计和定向查询
    entity_type TEXT NOT NULL, -- 作用：说明事件作用于 view/checkpoint/branch/fork/turn/message；场景：通过 entity_id 查询某个对象的完整控制历史
    entity_id TEXT NOT NULL, -- 作用：指向被操作对象；场景：某个 view 被创建、激活或 pruning 时关联对应记录
    branch_id TEXT, -- 作用：记录事件所属逻辑 branch；场景：判断 rewind 是否改变了当前 branch
    view_id TEXT, -- 作用：记录事件关联的 context view；场景：把 view_created 和后续 checkpoint 变化串起来
    checkpoint_id TEXT, -- 作用：记录事件关联的 checkpoint；场景：恢复某个 checkpoint 的创建来源
    payload_json TEXT NOT NULL, -- 作用：保存暂时不需要索引的扩展参数；场景：保存 UI 操作来源，但不得放入核心定位字段
    transaction_id TEXT NOT NULL, -- 作用：关联 storage_commits；场景：确认控制事件和消息/索引在同一提交中
    previous_event_hash TEXT, -- 作用：连接前一个控制事件；场景：检测控制历史是否被删除或重排
    event_hash TEXT NOT NULL, -- 作用：当前控制事件校验值；场景：SQLite backup 恢复后验证控制事件链
    created_at TEXT NOT NULL -- 作用：记录事件创建时间；场景：审计和按时间显示 branch 操作
);
```

允许的 `control_kind` 至少包括：`view_created`、`checkpoint_created`、`checkpoint_finalized`、`rewind`、`compaction`、`offline_compaction`、`branch_activated`、`fork_created`、`prune_marked`、`prune_released` 和 `schema_migration`。

#### 4.6 `messages`

对应 `rollout.jsonl` 中的一行，只保存定位、身份和轻量状态。

```sql
CREATE TABLE messages (
    message_sequence INTEGER PRIMARY KEY, -- 作用：对应 JSONL 行的物理顺序；场景：根据 SQLite 定位后读取具体消息，不能把它当成当前 view 的逻辑顺序
    message_id TEXT NOT NULL UNIQUE, -- 作用：标识一条不可变具体消息；场景：cursor、anchor、工具结果关联和 UI 稳定 key 使用它
    turn_id TEXT NOT NULL, -- 作用：把消息归入一个 Turn；场景：加载 Turn 时一次取得 user、assistant、tool 全部消息
    role TEXT NOT NULL, -- 作用：限制为 user/assistant/tool；场景：决定消息解码方式和默认 projection 分类
    jsonl_offset INTEGER NOT NULL, -- 作用：记录 JSONL 行的起始字节；场景：不扫描文件前缀即可 seek 到消息
    jsonl_length INTEGER NOT NULL, -- 作用：记录 JSONL 行的完整字节长度；场景：只读取该消息，不把后续大型消息带入内存
    content_length INTEGER NOT NULL, -- 作用：记录 canonical message 正文大小；场景：执行单项字节预算和识别大型工具结果
    content_hash TEXT NOT NULL, -- 作用：记录正文校验值；场景：读取 JSONL 后确认内容没有被外部修改
    visibility TEXT NOT NULL, -- 作用：表示 visible/internal/model_only/hidden；场景：Web 默认过滤 internal，Agent full 可读取 model_only
    commit_id INTEGER NOT NULL, -- 作用：关联写入该消息的 storage commit；场景：恢复时判断消息是否已提交
    created_at TEXT NOT NULL -- 作用：记录消息写入时间；场景：审计、排序和 Turn 时间线展示
);
```

#### 4.7 `message_projections`

保存 Web 和历史摘要使用的有界消息投影，不保存大型 canonical 正文。

```sql
CREATE TABLE message_projections (
    message_sequence INTEGER PRIMARY KEY, -- 作用：关联一条 canonical message；场景：Web summary 先读此表，只有 detail/full 才读 JSONL
    text_preview TEXT, -- 作用：保存受字符上限限制的可见文本；场景：列表或历史摘要无需打开大型 assistant 正文
    visible_text TEXT, -- 作用：保存可直接展示的有界 text/output_text；场景：默认 final_response 直接从此字段返回
    visible_text_length INTEGER NOT NULL, -- 作用：记录投影文本长度；场景：判断是否达到投影截断限制
    has_reasoning INTEGER NOT NULL, -- 作用：标记是否存在 reasoning block；场景：决定 UI 是否显示折叠 reasoning 区域
    has_encrypted_reasoning INTEGER NOT NULL, -- 作用：标记是否存在加密 reasoning；场景：只显示存在标记，不把密文返回 Web
    has_tool_calls INTEGER NOT NULL, -- 作用：标记 assistant 是否包含 tool_calls；场景：决定是否查询 tool_calls 摘要
    phase TEXT, -- 作用：表示 visible_text/tool_request/final_answer 等派生阶段；场景：辅助投影查询，最终响应必须以 turns.final_message_sequence 为准，不能只依赖 phase
    projection_version INTEGER NOT NULL, -- 作用：标记投影算法版本；场景：算法升级后只重建投影，不改变 JSONL
    updated_at TEXT NOT NULL -- 作用：记录投影更新时间；场景：诊断投影是否滞后于消息提交
);
```

#### 4.8 `tool_calls`

工具调用和工具结果的轻量索引。工具参数和结果正文仍只存在 JSONL。

```sql
CREATE TABLE tool_calls (
    tool_call_id TEXT PRIMARY KEY, -- 作用：标识一次具体工具调用；场景：把 assistant 中的 tool_call 和后续 tool result 关联起来
    assistant_message_sequence INTEGER NOT NULL, -- 作用：定位发起调用的 assistant 消息；场景：读取当前 Turn 的工具摘要或完整 tool_call
    call_index INTEGER NOT NULL, -- 作用：标识同一 assistant 消息内的第几个调用；场景：一个 assistant 同时调用多个工具时保持顺序
    tool_name TEXT NOT NULL, -- 作用：保存工具名称；场景：默认 Web 工具折叠项只显示名称
    status TEXT NOT NULL, -- 作用：表示 pending/running/succeeded/failed/cancelled；场景：默认摘要只返回名称和状态
    result_message_sequence INTEGER, -- 作用：指向工具结果消息；场景：用户点击详情时通过它读取 JSONL result
    argument_length INTEGER NOT NULL, -- 作用：记录参数正文长度；场景：决定是否允许 bounded detail 读取
    result_length INTEGER, -- 作用：记录结果正文长度；场景：提前执行总字节预算，不必先读取正文
    argument_hash TEXT, -- 作用：记录参数校验值；场景：读取详情后检测正文是否变化
    result_hash TEXT, -- 作用：记录结果校验值；场景：验证大型工具输出和索引是否一致
    summary_text TEXT, -- 作用：保存有界工具摘要；场景：默认历史加载不读取参数和结果正文
    started_at TEXT, -- 作用：记录工具开始时间；场景：展示耗时和诊断运行状态
    completed_at TEXT, -- 作用：记录工具结束时间；场景：判断状态是否已经最终化
    projection_version INTEGER NOT NULL -- 作用：标记工具投影算法版本；场景：工具摘要格式变化时重建索引
);
```

#### 4.9 `reasoning_blocks`

记录普通 reasoning、可展示 summary 和加密 reasoning 的投影信息。

```sql
CREATE TABLE reasoning_blocks (
    message_sequence INTEGER NOT NULL, -- 作用：关联包含该 block 的 assistant 消息；场景：Web 读取 reasoning_summary 时先定位消息
    content_block_index INTEGER NOT NULL, -- 作用：保存 block 在 AIMessage.content 数组中的零基索引；场景：恢复 reasoning_content、reasoning_items、thinking、redacted_thinking 与 text 的原始相对顺序
    item_index INTEGER NOT NULL DEFAULT 0, -- 作用：保存 reasoning_items 数组中某个嵌套 item 的零基索引；场景：一个 content block 包含多个 reasoning item 时保持 provider 返回顺序
    carrier_type TEXT NOT NULL, -- 作用：保存原始 carrier 名称 reasoning_content/reasoning_items/thinking/redacted_thinking；场景：跨 provider 恢复时按载体能力过滤，不把不同载体错误合并
    item_id TEXT, -- 作用：保存 provider 为 reasoning item 提供的稳定 id；场景：诊断流式 item 更新和恢复时校验 item 对应关系
    reasoning_text TEXT, -- 作用：保存有界的普通 reasoning 文本投影；场景：detail 模式显示普通思考块，不读取大型 canonical 正文
    summary_text TEXT, -- 作用：保存安全且有界的 summary_text 投影；场景：默认最新 Turn 展示折叠 reasoning summary
    signature_present INTEGER NOT NULL DEFAULT 0, -- 作用：标记 thinking carrier 是否带签名；场景：Web 只显示存在性，provider adapter 决定是否可回放
    encrypted_length INTEGER, -- 作用：记录密文大小但不保存密文正文；场景：显示存在状态并执行响应预算
    encrypted_hash TEXT, -- 作用：保存密文校验值；场景：从 JSONL 读取 opaque payload 后验证完整性
    provider_id TEXT, -- 作用：标识产生该 block 的 provider；场景：决定是否允许特定 provider 回放 encrypted reasoning
    projection_version INTEGER NOT NULL, -- 作用：标记 reasoning 投影算法版本；场景：摘要规则升级时重新投影
    PRIMARY KEY (message_sequence, content_block_index, item_index)
);
```

#### 4.10 `turns`

按用户消息分隔的逻辑 Turn 索引。

```sql
CREATE TABLE turns (
    turn_id TEXT PRIMARY KEY, -- 作用：标识一个以用户消息为分隔的物理 Turn；场景：history API 一次返回完整 Turn
    turn_ordinal INTEGER NOT NULL UNIQUE, -- 作用：记录该 Turn 在 rollout 中的创建顺序，属于物理序号而非当前 view 的逻辑顺序；场景：新 replay Turn-4' 创建后拥有新的全局 ordinal
    turn_kind TEXT NOT NULL, -- 作用：区分 normal/compaction_summary；场景：Agent full 包含压缩摘要，但 Web 普通分页过滤 summary Turn
    branch_id TEXT NOT NULL, -- 作用：记录 Turn 首次创建时所属 branch；场景：审计消息来源，实际是否有效仍由 context_view_ranges 决定
    first_message_sequence INTEGER NOT NULL, -- 作用：定位 Turn 的第一条物理消息；场景：读取 Turn 内全部消息前先确定 JSONL 范围
    last_message_sequence INTEGER NOT NULL, -- 作用：定位 Turn 的最后一条物理消息；场景：计算 Turn 的最大读取范围和详情预算
    user_message_sequence INTEGER, -- 作用：指向该 Turn 的用户消息那一行，不是逻辑位置；场景：读取最新 Turn 时先得到用户消息的 JSONL offset，再读取该 Turn 的 assistant/tool；必须先用当前 view ranges 验证它属于当前上下文
    final_message_sequence INTEGER, -- 作用：指向该 Turn 最终 assistant 消息；场景：默认 final_response 直接定位该消息，不按 max(message_sequence) 猜测
    final_message_id TEXT, -- 作用：保存最终 assistant 的稳定 message_id；场景：final_message_sequence 变化时仍能校验 finalize 指针确实指向目标消息
    status TEXT NOT NULL, -- 作用：表示 open/completed/failed/interrupted；场景：历史摘要决定是否显示 final_response 或未完成状态
    created_at TEXT NOT NULL, -- 作用：记录 Turn 创建时间；场景：时间线排序和运行耗时统计
    updated_at TEXT NOT NULL -- 作用：记录 Turn 最近一次消息或 finalize 更新的时间；场景：判断中间 assistant 是否在最终响应之后重新打开 Turn
);
```

#### 4.11 `branches`

记录同一 rollout 内的逻辑分支。`parent_branch_id` 只表示 provenance，不作为物理消息定位依赖。

```sql
CREATE TABLE branches (
    branch_id TEXT PRIMARY KEY, -- 作用：标识同一 rollout 内的逻辑分支；场景：rewind 后新对话和旧后缀使用不同 branch
    parent_branch_id TEXT, -- 作用：记录来源 branch provenance，不参与消息定位；场景：审计 rewind/replay 从哪条 branch 创建
    branch_kind TEXT NOT NULL, -- 作用：记录 root/rewind/replay/compaction/fork 等创建原因；场景：审计当前 branch 的来源
    status TEXT NOT NULL, -- 作用：表示 active/archived/deleted；场景：active branch 才能作为当前会话执行入口
    head_view_id TEXT, -- 作用：当前 branch 的 head view；场景：先由 checkpoint_namespace_state.active_branch_id 找到它，不能查最大物理 sequence
    head_checkpoint_id TEXT, -- 作用：当前 branch 的 head checkpoint；场景：LangGraph 恢复最新 envelope 时固定 checkpoint，而不是按最大 sequence 猜测
    created_at TEXT NOT NULL, -- 作用：记录 branch 创建时间；场景：显示 replay/rewind 分支历史
    updated_at TEXT NOT NULL -- 作用：记录 branch head 或状态最近更新时间；场景：判断 active branch 是否发生了新的控制提交
);
```

#### 4.12 `context_views`

context view 是不可变的逻辑上下文快照，不复制 messages 数组。

```sql
CREATE TABLE context_views (
    view_id TEXT PRIMARY KEY, -- 作用：标识一份不可变有效上下文快照；场景：checkpoint、cursor 和 fork 都通过它确定当时有效消息
    branch_id TEXT NOT NULL, -- 作用：记录 view 所属 branch；场景：验证 source view 和当前 branch 不被错误混用
    parent_view_id TEXT, -- 作用：记录直接父 view；场景：沿 view 链审计 rewind/compaction 的来源，具体有效消息仍以 ranges 为准
    view_kind TEXT NOT NULL, -- 作用：区分 initial/append/rewind/compaction/fork；场景：决定 cursor epoch、压缩边界和审计展示
    head_turn_id TEXT, -- 作用：当前 view 中最后一个可展示的 normal Turn；场景：最新 Turn 直接查询它，不使用 max(message_sequence)
    head_message_sequence INTEGER NOT NULL, -- 作用：当前 view 所包含消息中最大的物理 sequence，可能指向内部消息；场景：确定物理读取水位，但不能用它判断最新可展示 Turn
    logical_turn_count INTEGER NOT NULL, -- 作用：记录当前 view 中可分页的 normal Turn 数，不把 compaction_summary 计入 Web Turn 批次；场景：最新 1/4/16/64 Turn 查询
    control_sequence INTEGER, -- 作用：指向创建该 view 的 control event；场景：把 view 和 append/rewind/compaction 控制提交关联起来
    created_at TEXT NOT NULL -- 作用：记录 view 创建时间；场景：按时间审计上下文变化
);
```

#### 4.13 `context_view_ranges`

通过消息范围和 view 范围引用表达 rewind、compaction、replay，避免复制完整历史。

```sql
CREATE TABLE context_view_ranges (
    view_id TEXT NOT NULL, -- 作用：标识该范围属于哪个 context view；场景：读取 checkpoint 时先筛选目标 view 的全部范围
    range_index INTEGER NOT NULL, -- 作用：标识该 view 内的物理范围行；场景：同一个 view 可以有多段不连续 JSONL 范围
    source_kind TEXT NOT NULL, -- 作用：区分 view 或 messages；场景：旧历史使用 source view，新增消息使用 JSONL message range
    source_view_id TEXT, -- 作用：source_kind=view 时指向来源 view；场景：rewind/replay 读取来源 view 的前缀
    start_message_sequence INTEGER, -- 作用：内部范围起点；场景：直接引用本 rollout 的 JSONL 物理消息时快速展开范围
    end_message_sequence INTEGER, -- 作用：内部范围终点；场景：直接引用本 rollout 的 JSONL 物理消息时确定闭区间
    source_start_ordinal INTEGER, -- 作用：来源 view 内的逻辑消息起点；场景：V1 的 Turn-1 起始逻辑位置是 0，即使其物理 sequence 很大
    source_end_ordinal INTEGER, -- 作用：来源 view 内的逻辑消息终点；场景：before B:3 时只保留 B:3 前的 source ordinal
    source_start_turn_ordinal INTEGER, -- 作用：来源 view 内的逻辑 Turn 起点；场景：从 V1 取 Turn-1~Turn-3 时不需要扫描所有 message
    source_end_turn_ordinal INTEGER, -- 作用：来源 view 内的逻辑 Turn 终点；场景：跨 view 向前加载完当前范围后定位来源 view 的下一段
    message_start_sequence INTEGER, -- 作用：source_kind=messages 时的物理消息起点；场景：读取 replay 新增 Turn-4' 的 user 消息
    message_end_sequence INTEGER, -- 作用：source_kind=messages 时的物理消息终点；场景：一次 bounded 读取新 Turn 的 assistant/tool 消息
    logical_start_ordinal INTEGER NOT NULL, -- 作用：范围在当前 view 的逻辑消息起点；场景：根据 cursor 的逻辑消息位置找到对应 range
    logical_end_ordinal INTEGER NOT NULL, -- 作用：范围在当前 view 的逻辑消息终点；场景：确定范围是否覆盖请求的消息窗口
    logical_start_turn_ordinal INTEGER, -- 作用：范围在当前 view 的可分页 Turn 起点；场景：最新 Turn 向前加载时按 Turn 而不是物理消息倒序
    logical_end_turn_ordinal INTEGER, -- 作用：范围在当前 view 的可分页 Turn 终点；场景：计算该 range 还剩多少完整 Turn
    range_ordinal INTEGER, -- 作用：规范化的逻辑范围序号；场景：跨 view 读取计划保留范围顺序
    PRIMARY KEY (view_id, range_index)
);
```

例如 rewind 到 B:3 后继续追加的新 view 可以表达为：

```text
range 1: source_kind=view, source_view=旧 view, source_start=0, source_end=B:3
range 2: source_kind=messages, message_start=新消息, message_end=当前尾部
```

#### 4.14 `context_view_jumps`

上下文 view 链较长时，用二进制跳转快速定位祖先 view。

```sql
CREATE TABLE context_view_jumps (
    view_id TEXT NOT NULL, -- 作用：当前需要加速解析的 view；场景：读取长 replay/compaction 链时避免逐层查询
    jump_level INTEGER NOT NULL, -- 作用：表示 2^level 的跳跃层级；场景：一次跳过多个祖先 view
    ancestor_view_id TEXT NOT NULL, -- 作用：保存该跳跃层级命中的祖先 view；场景：从 V64 快速跳到 V1 附近
    ancestor_depth INTEGER NOT NULL, -- 作用：记录实际跨过的 view 层数；场景：检查跳跃索引是否与 source_view 链一致
    PRIMARY KEY (view_id, jump_level)
);
```

#### 4.15 `checkpoints`

LangGraph checkpoint 只引用一个不可变 context view，不保存 messages 数组。

```sql
CREATE TABLE checkpoints (
    checkpoint_id TEXT PRIMARY KEY, -- 作用：标识一次 LangGraph 状态快照；场景：用户从历史位置 fork 或 rewind 时作为稳定 anchor
    checkpoint_ns TEXT NOT NULL, -- 作用：区分同一 thread 下的 checkpoint namespace；场景：实现 LangGraph list/get_tuple 的 namespace 过滤
    commit_id INTEGER NOT NULL, -- 作用：关联创建该 checkpoint 的 storage commit；场景：确认 checkpoint 与消息/view 在同一提交边界
    message_sequence INTEGER NOT NULL, -- 作用：保存该 checkpoint 对应的物理消息水位；场景：通过 context view 找到 checkpoint 当时可恢复的消息范围
    message_count INTEGER NOT NULL, -- 作用：记录该 checkpoint view 的消息数量；场景：诊断恢复规模和校验 view materialize 结果
    parent_checkpoint_id TEXT, -- 作用：保存 LangGraph checkpoint lineage；场景：list 历史 checkpoint 和恢复 parent 关系
    view_id TEXT NOT NULL, -- 作用：固定该 checkpoint 当时的有效消息集合；场景：active view 改变后仍恢复旧上下文
    branch_id TEXT NOT NULL, -- 作用：记录 checkpoint 创建时所属 branch；场景：防止历史 checkpoint 被错误解释为另一条 branch 的状态
    checkpoint_version INTEGER NOT NULL, -- 作用：保存 LangGraph checkpoint 的 v 字段；场景：get_tuple 重建原始 checkpoint envelope，并在 serializer/schema 变化时拒绝错误解码
    checkpoint_timestamp TEXT NOT NULL, -- 作用：保存 LangGraph checkpoint 的 ts 字段；场景：保持 checkpoint 时间线、list 排序和恢复后的 checkpoint 语义
    checkpoint_kind TEXT NOT NULL, -- 作用：区分 normal/rewind/compaction/fork；场景：决定恢复、审计和 retention 规则
    status TEXT NOT NULL, -- 作用：表示 active/archived/deleted；场景：deleted checkpoint 不能继续作为 fork anchor
    checkpoint_json TEXT NOT NULL, -- 作用：保存非 messages 的 checkpoint envelope 核心字段；场景：恢复 LangGraph 的 v、ts、updated_channels 等原始结构但不重复保存 messages 数组
    metadata_json TEXT NOT NULL, -- 作用：保存 checkpoint 元数据但禁止放 messages；场景：记录 provider/job 来源而不重新序列化消息数组
    envelope_serializer_name TEXT NOT NULL, -- 作用：标识 versions_seen 和 pending_sends 的序列化器；场景：get_tuple 按写入时格式恢复 checkpoint-level 状态
    versions_seen_blob BLOB NOT NULL, -- 作用：保存 LangGraph checkpoint 的 versions_seen；场景：图恢复时判断各 task 已经消费过哪些 channel version
    versions_seen_length INTEGER NOT NULL, -- 作用：保存 versions_seen 序列化字节长度；场景：读取预算、完整性检查和异常大 checkpoint 诊断
    versions_seen_hash TEXT NOT NULL, -- 作用：保存 versions_seen BLOB 校验值；场景：发现 SQLite 数据被部分修改时阻止静默恢复
    pending_sends_blob BLOB NOT NULL, -- 作用：保存 LangGraph checkpoint 的 pending_sends；场景：恢复 checkpoint 时继续处理尚未交付给下游节点的发送项
    pending_sends_length INTEGER NOT NULL, -- 作用：保存 pending_sends 序列化字节长度；场景：读取预算和损坏定位
    pending_sends_hash TEXT NOT NULL -- 作用：保存 pending_sends BLOB 校验值；场景：验证 checkpoint envelope 没有发生静默变化
);
```

#### 4.16 `checkpoint_channels`

保存 checkpoint 的逐 channel 状态。每一个 checkpoint 在这里为 `channel_values`、`channel_versions` 和 `updated_channels` 的并集建立一行；`messages` 也有一行，但只保存 rollout view 指针，不保存完整消息数组。这样所有 channel 都经过同一套参数和恢复流程，同时仍保持 messages 的增量存储。

```sql
CREATE TABLE checkpoint_channels (
    checkpoint_id TEXT NOT NULL, -- 作用：关联所属 checkpoint；场景：get_tuple 按 checkpoint 一次取回全部 channel，并保证历史 checkpoint 不读取 active view 的新值
    channel_name TEXT NOT NULL, -- 作用：标识 LangGraph channel；场景：恢复 messages、工具状态、任务状态和其它业务 channel 时保持原始键名
    storage_kind TEXT NOT NULL, -- 作用：区分 rollout_view/sqlite_value；场景：messages 走 context view materialize，其它 channel 走 SQLite BLOB 反序列化
    value_state TEXT NOT NULL, -- 作用：区分 present/absent/view；场景：区分 channel 值为序列化后的 None、checkpoint 中没有值，以及 messages 由 view 表示
    channel_version TEXT, -- 作用：保存该 channel 在 checkpoint 的 channel_versions 值；场景：LangGraph 判断节点是否需要消费该 channel 的新版本；某些输入可能没有版本
    serializer_name TEXT, -- 作用：保存 sqlite_value 使用的 serializer；场景：恢复非 messages channel 时选择与写入时一致的解码器，rollout_view 可为空
    value_blob BLOB, -- 作用：保存非 messages channel 的序列化值；场景：恢复计数器、任务状态、provider 状态等，不把它们写入 rollout JSONL；absent/view 状态为空
    value_length INTEGER, -- 作用：保存 value_blob 字节长度；场景：执行 checkpoint 读取预算、识别异常大的业务 channel，并校验读取完整性
    value_hash TEXT, -- 作用：保存 value_blob 的校验值；场景：读取 SQLite 后确认 channel 数据未被外部修改
    context_view_id TEXT, -- 作用：在 storage_kind=rollout_view 时保存 messages channel 的有效 view；场景：历史 checkpoint 恢复时从该 view materialize messages，而不是读取当前 active view
    updated_index INTEGER, -- 作用：保存该 channel 在 checkpoint.updated_channels 中的 0-based 顺序，未更新时为空；场景：重建 checkpoint envelope 时保留 LangGraph 的更新 channel 集合和原始顺序
    created_at TEXT NOT NULL, -- 作用：记录 channel 行创建时间；场景：审计 checkpoint 内各 channel 的提交时序
    PRIMARY KEY (checkpoint_id, channel_name), -- 作用：保证一个 checkpoint 的一个 channel 只有唯一状态；场景：避免同一 channel 在恢复时出现冲突版本
    CHECK (
        (storage_kind = 'rollout_view'
         AND channel_name = 'messages'
         AND value_state = 'view'
         AND context_view_id IS NOT NULL
         AND serializer_name IS NULL
         AND value_blob IS NULL
         AND value_length IS NULL
         AND value_hash IS NULL)
        OR
        (storage_kind = 'sqlite_value'
         AND channel_name <> 'messages'
         AND context_view_id IS NULL
         AND (
             (value_state = 'present'
              AND serializer_name IS NOT NULL
              AND value_blob IS NOT NULL
              AND value_length IS NOT NULL
              AND value_hash IS NOT NULL)
             OR
             (value_state = 'absent'
              AND value_blob IS NULL
              AND value_length IS NULL
              AND value_hash IS NULL)
         ))
    )
);
```

`checkpoint_channels` 的写入约束如下：

1. `channel_values`、`channel_versions` 和 `updated_channels` 中出现过的 channel 必须合并成一组唯一 channel 名称；即使某个 channel 本次没有 value，也要以 `value_state=absent` 保存其版本，不能因为没有 BLOB 就丢失 `channel_versions`。
2. 非 messages channel 的 `None` 是合法值，必须序列化后以 `value_state=present` 保存，不能和没有该 channel 的 `value_state=absent` 混淆。
3. `messages` 行的 `context_view_id` 必须与 `checkpoints.view_id` 相同；该行只表示消息 channel 的存储位置，实际消息仍由 `RolloutContextReader.full` 从 view/range 和 rollout JSONL materialize。
4. `get_tuple` 必须根据这些行重建 `channel_values`、`channel_versions` 和 `updated_channels`，再从 `checkpoints` 恢复 `v`、`ts`、`versions_seen` 和 `pending_sends`，不能只返回非 messages channel。
5. checkpoint、所有 `checkpoint_channels` 行和新 context view 必须在同一个 SQLite commit 中提交；任何一部分失败都不能产生半个 checkpoint。

#### 4.17 `pending_writes`

保存 LangGraph 尚未合并进 checkpoint 的写入。

```sql
CREATE TABLE pending_writes (
    checkpoint_id TEXT NOT NULL, -- 作用：关联目标 checkpoint；场景：checkpoint 尚未完成时暂存待合并写入
    task_id TEXT NOT NULL, -- 作用：关联 LangGraph task；场景：按 task 返回 pending writes
    task_path TEXT NOT NULL, -- 作用：保存 LangGraph 写入来源的 task path；场景：多个嵌套 task 使用相同 task_id 时仍能恢复原始写入归属
    write_index INTEGER NOT NULL, -- 作用：保存同一 task 内的写入顺序；场景：多个写入不能因 SQL 查询顺序改变
    channel TEXT NOT NULL, -- 作用：标识待写入的 channel；场景：恢复时按 channel 合并状态
    serializer_name TEXT NOT NULL, -- 作用：标识 value_blob 的序列化器；场景：恢复 pending write 的原始类型
    value_blob BLOB NOT NULL, -- 作用：保存 pending write 值；场景：进程崩溃后继续处理尚未合并的 channel 更新
    value_length INTEGER NOT NULL, -- 作用：保存 pending write 序列化长度；场景：恢复前执行大小限制和诊断异常大的待写入值
    value_hash TEXT NOT NULL, -- 作用：保存 pending write 校验值；场景：确认待写入值没有在 SQLite 中被部分覆盖
    status TEXT NOT NULL, -- 作用：表示 pending/committed/discarded；场景：区分仍需恢复和已经消费的写入
    created_at TEXT NOT NULL, -- 作用：记录 pending write 创建时间；场景：诊断长期未完成任务
    PRIMARY KEY (checkpoint_id, task_id, task_path, write_index) -- 作用：保证同一 checkpoint/task/path/index 只有一条写入；场景：重试 put_writes 时避免重复 pending write
);
```

#### 4.18 LangGraph checkpoint envelope 的恢复规则

`checkpoints` 和 `checkpoint_channels` 合在一起保存完整的 LangGraph checkpoint envelope，但不保存重复的 messages 数组。一次 `get_tuple` 的 materialize 规则如下：

```text
checkpoints
  ├── checkpoint_id / checkpoint_version / checkpoint_timestamp
  ├── metadata_json
  ├── versions_seen_blob → versions_seen
  └── pending_sends_blob → pending_sends

checkpoint_channels
  ├── channel_name = messages
  │     └── context_view_id → RolloutContextReader.full → BaseMessage list
  └── channel_name != messages
        └── serializer_name + value_blob → channel value
```

写入一个 checkpoint 时，必须按以下规则生成 channel 行：

- 将 `checkpoint.channel_values`、`checkpoint.channel_versions` 和 `checkpoint.updated_channels` 的 key 合并；不能只遍历 `channel_values`，否则没有当前 value 的 channel version 会丢失。
- LangGraph 允许某些增量 checkpoint 省略未更新的 `messages` channel：如果存在父 checkpoint，`RolloutCheckpointSaver.put` 必须在写入前从父 checkpoint 固定的 messages view materialize 当前消息；如果这是没有父 checkpoint 的初始化 checkpoint，则显式写入空 messages view。写入后的 checkpoint 一律必须有自己的 messages channel 行，`get_tuple` 不能在读取阶段静默补齐缺失行。
- `messages` channel 只写 `storage_kind=rollout_view`、`value_state=view` 和 `context_view_id`，不写 `value_blob`。`context_view_id` 必须等于 `checkpoints.view_id`。
- 其它 channel 写 `storage_kind=sqlite_value`。值为 Python `None` 时仍然序列化为 BLOB 并使用 `value_state=present`；只有 channel 不在 `channel_values` 时才使用 `value_state=absent`。
- `channel_version` 来自 `checkpoint.channel_versions`，`updated_index` 来自 `checkpoint.updated_channels` 的列表位置；不能使用写入时间或 SQLite 自增值替代 LangGraph 的 channel version，也不能只保存一个无序布尔标记。
- `versions_seen` 和 `pending_sends` 使用 `envelope_serializer_name` 序列化到 `checkpoints`，并记录长度和哈希。它们不是 channel value，不能塞进某个伪造的 channel 名称。

恢复时，`RolloutCheckpointSaver.get_tuple` 必须先校验 checkpoint、channel 行和 view 指针属于同一个 commit，再分别解码。任何 channel 缺失、版本重复、serializer 不存在、hash 不匹配或 messages view 不一致，都必须返回明确的 checkpoint 数据错误，不得用当前 active view 或默认空值静默补齐。

例如一个 checkpoint 同时包含消息、计数器和任务状态时，表中应类似这样：

```text
checkpoints(checkpoint_id = C42, view_id = V7, checkpoint_version = 4)

checkpoint_channels(C42, messages,
                    storage_kind = rollout_view,
                    value_state = view,
                    context_view_id = V7,
                    channel_version = "...", value_blob = NULL)
checkpoint_channels(C42, counter,
                    storage_kind = sqlite_value,
                    value_state = present,
                    serializer_name = jsonplus,
                    value_blob = <serialized 12>, channel_version = "...")
checkpoint_channels(C42, task_state,
                    storage_kind = sqlite_value,
                    value_state = present,
                    serializer_name = jsonplus,
                    value_blob = <serialized state>, channel_version = "...")
```

下一次 checkpoint 即使 `counter` 没有变化，也要保存该 checkpoint 自己的 `present` channel 行和序列化值；只有 LangGraph checkpoint 明确没有该 channel 时才使用 `value_state=absent`。`get_tuple(C43)` 必须恢复 C43 自己的完整 channel snapshot，不能隐式读取 C42 的当前值。

#### 4.19 `fork_origins`

保存子 rollout 的来源关系。子 rollout 不依赖父 rollout 的文件或 SQLite。

```sql
CREATE TABLE fork_origins (
    fork_id TEXT PRIMARY KEY, -- 作用：标识一次 fork；场景：连接父子会话 provenance 和 retention
    child_session_id TEXT NOT NULL, -- 作用：标识子会话；场景：确认 fork 结果写入哪个独立 rollout
    source_session_id TEXT NOT NULL, -- 作用：标识父会话；场景：展示来源、释放对应 retention 并追踪 fork provenance
    source_checkpoint_id TEXT, -- 作用：记录 fork 的精确历史 anchor；场景：复查子会话从父会话哪一刻创建；没有可恢复 checkpoint 的空 fork 时为空
    source_view_id TEXT, -- 作用：记录 fork 使用的有效消息 view；场景：父 active view 后续变化不影响子会话；空 rollout fork 时为空
    fork_mode TEXT NOT NULL, -- 作用：区分 context_fork/history_prefix_fork/full_rollout_copy；场景：决定复制多少消息和控制状态
    relationship TEXT NOT NULL, -- 作用：表示 detached/pinned；场景：决定父会话是否允许删除
    copied_message_count INTEGER NOT NULL, -- 作用：记录实际复制的消息数；场景：验证 fork 是否完整和估算复制成本
    created_at TEXT NOT NULL -- fork 创建时间
);
```

#### 4.20 `retention_refs`

保护仍被 checkpoint、fork、审计或 pinned relationship 使用的 view 和消息。

```sql
CREATE TABLE retention_refs (
    retention_id TEXT PRIMARY KEY, -- 作用：标识一条独立的保留约束；场景：删除 view、checkpoint 或 rollout 前，按此 ID 精确释放、审计或排查是哪条引用阻止了清理
    reference_kind TEXT NOT NULL, -- 作用：说明保留约束由什么对象产生；场景：区分 checkpoint、fork、pinned 关系和审计任务，从而决定释放时检查哪个上层对象
    reference_id TEXT NOT NULL, -- 作用：保存产生保留约束的对象 ID；场景：用户删除 checkpoint 或解除 pinned fork 时，通过 reference_kind + reference_id 找到并更新对应保留记录
    target_view_id TEXT, -- 作用：指出被保护的逻辑 context view；场景：某个历史 checkpoint 仍需可恢复时，禁止 pruning 删除该 view 或其唯一可达范围
    target_message_sequence INTEGER, -- 作用：在只需保护单条物理消息时记录其 JSONL 序号；场景：工具结果详情、审计引用或尚未完成物化的 fork 只依赖某条消息时，不必锁住整个 view
    owner_session_id TEXT, -- 作用：记录这条保留引用属于哪个 rollout/session；场景：执行会话级清理、显示占用来源或跨会话 fork 删除检查时，避免误清理其它会话的引用
    expires_at TEXT, -- 作用：记录临时保留约束的自动失效时间；场景：临时审计、后台物化任务或短期 pinned 操作超时后，可将其标记为 expired 而不是永久阻止 pruning
    status TEXT NOT NULL, -- 作用：记录 active/released/expired 生命周期；场景：pruning 只阻止 active，用户主动删除引用后标记 released，过期任务标记 expired，完整保留审计轨迹
    created_at TEXT NOT NULL -- 作用：记录保留约束创建时间；场景：审计保留策略、诊断长期未释放的引用，以及按时间展示 fork/checkpoint 的保留来源
);
```

#### 4.21 `context_view_turns`

`context_view_turns` 是按 view 物化的 Turn 定位表，用于把用户的 `turn_id` 转换成候选 view 内的逻辑 Turn 位置。它是派生索引，不是新的控制语义来源：有效范围仍由 `context_views` 和 `context_view_ranges` 定义，Turn 的物理消息边界仍由 `turns` 定义，compaction 的精确边界仍由 `message_id`/`message_sequence` 和 context range 定义。

该表只保存在当前 view 中完整可定位的 `normal` Turn；`compaction_summary` 等 `model_only` 控制性内容不计入 Web 的普通 Turn 分页，但仍可通过 `context_view_ranges` 为 Agent `full` 模式读取。一个 Turn 在同一个 view 中最多一行；旧 view 不修改，rewind、replay、compaction 创建新 view 时生成新行。若 message 级 compaction 切入某个 Turn，使当前 view 只剩部分消息或 summary，则当前 view 不生成该 Turn 的完整映射，Turn resolver 会继续查找祖先 view。

```sql
CREATE TABLE context_view_turns (
    view_id TEXT NOT NULL, -- 作用：标识这条物化映射属于哪个不可变 context view；场景：同一个 turn_id 在不同 rewind/replay view 中是否有效，必须按 view 分开判断
    turn_id TEXT NOT NULL, -- 作用：关联一个 normal Turn；场景：用户提交 turn_id 后，resolver 用 turn_id + 候选 view 查询其当前逻辑位置
    logical_turn_ordinal INTEGER NOT NULL, -- 作用：记录该 Turn 在当前 view 内的连续逻辑序号，不是 JSONL 物理 sequence；场景：以锚点为中心快速查询前后 4 个 Turn，并支持 cursor 翻页
    user_message_sequence INTEGER, -- 作用：缓存该 Turn 用户消息的物理 JSONL 序号；场景：批量返回 user 投影时无需再次从 turns 连接，随后通过 messages offset 定位正文
    final_message_sequence INTEGER, -- 作用：缓存该 Turn 已最终化 assistant 响应的物理 JSONL 序号；场景：只加载 user + final_response 时跳过中间 assistant/tool_call/tool_result
    PRIMARY KEY (view_id, turn_id), -- 作用：保证一个完整 Turn 在同一 view 只有一条映射；场景：防止范围重叠导致同一 Turn 被重复返回
    UNIQUE (view_id, logical_turn_ordinal) -- 作用：保证当前 view 的逻辑 Turn 序号不重复；场景：保证 before/after/around 查询可以得到稳定且无歧义的顺序
);
```

维护规则：

1. 创建新 view 时，在同一 SQLite 事务中根据 `context_view_ranges` 展开有效的 normal Turn，并写入该 view 的 `context_view_turns`；不能先提交 view、稍后异步补表，否则读快照可能看到不完整历史。
2. 该表不复制消息正文，也不改变 `rollout.jsonl`；它只复制 Turn ID、逻辑序号和两个物理消息指针，因此不会重新产生消息正文的 O(n²) 存储。
3. 如果派生表的行与 `context_view_ranges` 不一致，可以从 SQLite 基础表重新物化；如果基础 view/range 数据损坏，则按数据库的 `recovery_required` 流程处理，不能用线性 JSONL 猜测当前上下文。
4. 删除或归档旧 view 时同步删除或归档该 view 的映射；父 view 的映射不能被新 branch 原地修改。

#### 4.21A `turn_id` 用户锚点与 message 级 compaction 边界

这两个定位层次不能合并：

```text
用户 fork / rewind / replay
    └── turn_id + inclusive/before
          ↓ 后端解析
    完整 Turn 所在的最近可达 source view
          ↓ 再解析
    source checkpoint/channel state + message boundary

自动或用户主动 compaction
    └── message_id / message_sequence cutoff
          ↓
    message 范围级 context view + compaction summary
```

`context_view_turns` 只记录某个 view 中完整可定位的 normal Turn。它不能决定 compaction 的截断位置，也不能替代 `context_view_ranges`。例如 Turn-5 的消息顺序是 user、assistant(tool_call)、tool_result、final assistant，如果 compaction 的 cutoff 落在 tool_result 之前，新 view 可以只包含摘要和 Turn-5 的后续范围，但不应在 `context_view_turns` 中伪造一个“完整 Turn-5”行。Agent 的 `full` 读取仍直接展开 view ranges，因此压缩不会因为缺少 Turn 派生行而失效。

后端提供统一的 `resolve_turn_anchor(snapshot, turn_id, anchor_mode)`，流程固定为：

1. 从 `checkpoint_namespace_state(checkpoint_ns).active_branch_id` 找到该 namespace 的 active branch，再取得其 `head_view_id`；不能从所有 view 中按最大 `control_sequence` 选一个“最新” view。
2. 用 `context_view_turns(turn_id, view_id)` 的反向索引取得候选 view 集合。
3. 从 active head 沿 `parent_view_id`，必要时使用 `context_view_jumps` 跳转并做 cycle/越界校验；遇到第一个仍完整包含该 Turn 的 view 就停止。不能只按候选 view 的全局创建时间排序，也不能把 `cv.branch_id = active_branch_id` 当成唯一条件，因为 rewind 后有效的祖先 view 可能属于旧 branch。
4. 从 `turns.first_message_sequence`、`user_message_sequence` 和 `last_message_sequence` 计算边界：`inclusive` 包含目标 Turn 的最后一条有效消息，`before` 截断到用户消息之前。若目标 Turn 只剩 summary 或部分消息，则继续查找祖先；整个 active lineage 都没有完整 Turn 时返回明确的不可达错误。
5. 通过 `checkpoints.view_id`、active branch 的 `head_checkpoint_id` 和关联 control event 找到 source checkpoint/channel state。该 checkpoint 只是恢复非 messages channel 的来源；跨 rollout fork 时，选中 view 的消息必须 materialize 到子 rollout 自己的 JSONL，不能复用父 rollout 的 offset/range。

前端默认 fork 不携带 `turn_id`。此时 Saver 不得直接调用“最新 checkpoint”作为起点，而是执行 `resolve_latest_completed_turn_anchor(snapshot)`：

1. 从 active branch 的 `head_view_id` 开始沿 `parent_view_id` 向祖先遍历；
2. 在每个 view 内按 `logical_turn_ordinal DESC` 查询 `context_view_turns`，只接受 `turns.final_message_sequence IS NOT NULL` 且状态为 `completed`/`succeeded` 的 normal Turn；
3. 找到当前 lineage 中最近的已完成 Turn 后，复用同一 Turn resolver 取得 source view、checkpoint 和 message cutoff；
4. `context_fork`/`history_prefix_fork` 的消息物化只能到该 Turn 的 inclusive 边界，不能把后续运行中的用户消息带入子会话。

这条默认路径不使用 `MAX(messages.message_sequence)`，因为物理尾部可能只有正在生成的 user/assistant/tool 消息。显式传入 `turn_id` 时也必须要求该 Turn 已完成；如果 `final_message_sequence` 为空或状态仍为运行态，Saver 直接返回错误，不把 `cancel_unfinished_turns` 当成成功语义。没有任何消息的空 rollout 可以创建空子会话。内部 checkpoint/anchor 参数仍只作为低层测试和维护入口，Web API 不暴露它们。

compaction 自己不调用这个 Turn resolver 来决定 cutoff。它仍以 `message_id`/`message_sequence` 保存精确边界，在 SQLite 的 control event payload 和 `context_view_ranges` 中记录 cutoff、摘要消息及保留范围。用户随后从历史 Turn 发起 fork/rewind 时，resolver 只利用这些 message 范围判断该 Turn 是否完整可用。这样既能支持 Turn 级用户操作，也不会把中途压缩错误地提升为 Turn 级压缩。

反向索引和 active-lineage 查询的目标是避免扫描 JSONL；复杂度取决于 view lineage 深度，不能笼统宣称恒为 `O(log V + log T)`。`context_view_jumps` 只用于降低祖先链跳转成本，最终仍必须在同一个 read snapshot 内验证候选 view 的完整 Turn 覆盖和 checkpoint 关联。

### 4.21 模型切换时的 reasoning 恢复

`rollout.jsonl` 仍保存 assistant `AIMessage` 的完整 content，包括原始 `reasoning_content`、`reasoning_items`、`thinking`、`redacted_thinking` 和 `text` carrier；SQLite `reasoning_blocks` 只保存 carrier 类型、content/item 坐标、有限文本投影、签名存在性和密文长度/hash，不保存密文正文。读取历史和构造下一次模型请求是两个不同层次：Web projection 可以按统一 response part 模型显示 reasoning、summary 和没有正文的 encrypted 存在标记，provider adapter 则根据来源 provider 和目标模型能力决定是否把对应 carrier 放回请求。

```text
canonical AIMessage
  ├── content[]                       保留原始 carrier 和块顺序
  ├── tool_calls[]                    保留在 content 之后的调用顺序
  └── ToolMessage                     通过 tool_call_id 与调用配对
```

例如 `primary(big-pickle)` 产生了带 reasoning 的 assistant，切换到 `backup_4(gpt-5.6-luna)` 时，Responses adapter 不应把 primary 的 encrypted response item 发送给 backup_4；但 assistant 的可见文本、tool_call、tool_result 和 final response 仍按 LangChain 消息顺序组织。切回相同 provider 时才允许恢复 encrypted payload，并移除不能跨请求复用的服务端 id/state。这样模型能力差异只影响请求投影，不影响 checkpoint 恢复和 JSONL canonical 历史。

### 4.22 Canonical carrier 顺序与历史/live 统一响应模型

`AIMessage` 的 canonical 结构不再通过额外的全局 `part_sequence` 或“response part 索引”重复编码。恢复和展示必须遵循以下固定顺序：

```text
AIMessage.content[0..n]       # reasoning_content / reasoning_items / thinking /
                              # redacted_thinking / text，严格按 content_block_index
AIMessage.tool_calls[0..m]    # tool_calls 位于同一 AIMessage 的 content 之后，按 call_index
ToolMessage                   # 后续 role=tool 的消息，通过 tool_call_id 关联对应调用
下一个 AIMessage              # 新的 content/tool_calls 继续同样规则
```

`tool_calls.call_index` 只表示同一个 assistant message 内的第几个调用；它不是 `content` 的索引，也不需要为“工具调用在内容尾部”再创建一套索引。`reasoning_blocks.content_block_index` 只表示 `AIMessage.content` 的索引，`item_index` 只表示 `reasoning_items` 内部项的索引。工具卡片在投影层通过 `tool_call_id` 把调用和后续 `ToolMessage` 合并显示，但不会改变 canonical 消息顺序。

历史和 live 使用同一个前端语义渲染模型，而不是要求每次历史请求都返回 live 的全部流式事件。后端历史 DTO 与 live SSE 适配器最终都进入前端同一个 `TurnResponsePart` 联合类型：

```text
TurnResponsePartDTO
  ├── text       source=(message_sequence, content_block_index)
  ├── reasoning  source=(message_sequence, content_block_index, item_index)
  ├── tool_call  source=(assistant_message_sequence, call_index)
  ├── tool_result source=(result_message_sequence, assistant_message_sequence, call_index, tool_call_id)
  └── final_text source=(final_message_sequence, content_block_index)
```

这里的 `assistant_message_sequence` 只在 `tool_result` 上补充“结果由哪条
assistant AIMessage 产生”的关联坐标，不是新的全局 part 序号；`call_index`
也只是在同一条 assistant 的 `tool_calls[]` 中保持多个调用的数组顺序。
因此，即使不同 assistant 恰好复用了同一个外部 `tool_call_id`，前端仍以
`(assistant_message_sequence, call_index)` 配对工具卡片，不会把两个历史调用
合并。live part 尚未有 assistant sequence 时使用稳定的流式 `part_id`，不把
它伪装成 LangChain 的工具 ID。

每个 part 携带 `part_id`、`source`、`projection`（`summary`/`detail`/`streaming`）、`status` 和有界正文。历史 summary 只产生用户消息、最终文本、按 include 选择的 reasoning 摘要、工具摘要和隐藏统计；未请求的普通 reasoning、encrypted reasoning、tool_call 正文和 tool_result 正文不得因为底层投影存在而返回。历史 detail 根据同一个 source 坐标补齐中间文本、reasoning、tool_call 与 tool_result；live 则把 `text_start/delta/end`、`reasoning_start/delta/end` 和工具生命周期事件增量合并为同一种 part。未落盘的 live part 不伪造 JSONL `message_sequence`，只使用稳定的流式 `part_id`；这不是新的排序索引。live 工具只有在事件 payload 提供真实 `tool_call_id` 时才填写该字段，缺失时使用 `part_id` 做内部调用/结果配对但不把它冒充为 LangChain 工具 ID。前端只维护一套 `ResponsePartRenderer`，不为历史和 live 维护两套消息排列算法。

历史详情展开必须调用 Saver 的 detail API，并以返回的完整 Turn projection 替换当前 Turn；不得在前端把 summary 占位符拼成伪事件，也不得为补齐详情绕过 Saver 直接读取 JSONL。历史缺少 live 的 delta 事件是正常情况，因为 canonical JSONL 只保存稳定完成的消息；detail 通过 SQLite source 坐标重建与 live 相同的语义 parts。

最终响应定位使用 `turns.final_message_sequence` 指向的完整 assistant 消息，而不是“最后一个没有 tool_calls 的 AIMessage”推断。该消息中可见的 text/refusal content block 都属于 `final_text`，各自仍用 canonical `content_block_index` 保持顺序；同一消息中的 reasoning carrier 仍保持 reasoning 语义，不需要额外的 `final_content_block_index` 或全局 part 索引。只有旧数据缺少 finalization 指针时才允许 heuristic fallback，并且必须在诊断中标记 fallback。

实现约束：provider adapter 在发送请求前必须基于目标 provider capability 过滤 canonical content 中不可重放的 reasoning carrier；不能因为目标模型不支持该 carrier 就把原始 `AIMessage` 原样传出。`open_read_snapshot()` 在交付任何历史读取前校验 `reasoning_blocks` schema；索引读取路径发生异常时必须先关闭 snapshot 和文件锁，再把原始错误交给 API 层。历史 Turn 一旦已经有 `response_parts`，前端不得回退到 `assistant_text`、`thinking_blocks`、`tool_summary` 或旧 trace 字段生成第二套消息；只有尚未落盘的 live SSE 才允许由事件聚合器适配为 streaming response parts。

### 4.23 字段语义总则与典型读取情景

表中的不同编号承担不同职责，不能把它们都理解成“当前界面看到的第几个”或“消息数组下标”：

- `*_id` 是实体身份。例如 `turn_id` 标识一个不可重复的 Turn，`view_id` 标识一份逻辑上下文视图；它们用于跨表关联，不随着当前 view 的前后滚动改变。
- `*_sequence`（尤其是 `messages.message_sequence`、`turns.user_message_sequence`）是 rollout 的物理创建序号，也是 JSONL 行的定位键。它用于从 SQLite 直接找到 `rollout.jsonl` 的 offset/length，不表示消息在 rewind、compaction 或 fork 后的当前逻辑位置。
- `*_ordinal` 是某个 context view 内的逻辑顺序。例如同一个历史 Turn 在不同 view 中可能位于不同的逻辑位置；它用于 cursor、向前翻页和范围拼接，不能替代物理 JSONL 序号。
- `head_*` 是为了避免每次从头计算而保存的当前头指针。它们必须和对应的 `context_view_ranges` 在同一个 SQLite 事务中更新；读取端按它们快速定位后，仍要检查 view、branch 和 range 关系。
- `source_*` 表示范围引用的来源 view 或来源逻辑位置，`logical_*` 表示新 view 中呈现出来的位置。两者同时存在，是因为一个 view 可以把旧 view 的范围重新排列或截短。

#### 情景一：`turns.user_message_sequence` 到底拿来做什么

假设 `turns` 中有一行：

```text
turn_id = turn-008
turn_ordinal = 8
first_message_sequence = 101
user_message_sequence = 101
last_message_sequence = 108
final_message_sequence = 108
```

这里的 `101` 不是“当前页面第 101 条消息”，而是这条用户消息在 `rollout.jsonl` 中的物理序号。历史接口要加载这个 Turn 时，可以先执行：

```sql
SELECT message_sequence, turn_id
FROM messages
WHERE message_id = :anchor_message_id;

SELECT logical_turn_ordinal
FROM context_view_turns
WHERE view_id = :view_id
  AND turn_id = :anchor_turn_id;

SELECT turn_id, logical_turn_ordinal,
       user_message_sequence, final_message_sequence
FROM context_view_turns
WHERE view_id = :view_id
  AND logical_turn_ordinal BETWEEN :anchor_ordinal - 4
                              AND :anchor_ordinal + 4
ORDER BY logical_turn_ordinal;

SELECT jsonl_offset, jsonl_length
FROM messages
WHERE message_sequence = 101;
```

实际实现中，最后一个查询的 `101` 应替换为上一步得到的 `user_message_sequence`；上面的固定数字只是为了说明它与 `message_id` 的关系。随后直接 seek 到 JSONL 的对应位置，读取用户消息；如果只需要 `user + final_response`，可用两个指针批量读取：

```sql
SELECT c.logical_turn_ordinal,
       c.turn_id,
       c.user_message_sequence,
       c.final_message_sequence,
       user_message.jsonl_offset AS user_jsonl_offset,
       user_message.jsonl_length AS user_jsonl_length,
       final_message.jsonl_offset AS final_jsonl_offset,
       final_message.jsonl_length AS final_jsonl_length
FROM context_view_turns AS c
JOIN messages AS user_message
  ON user_message.message_sequence = c.user_message_sequence
LEFT JOIN messages AS final_message
  ON final_message.message_sequence = c.final_message_sequence
WHERE c.view_id = :view_id
  AND c.logical_turn_ordinal BETWEEN :start_ordinal AND :end_ordinal
ORDER BY c.logical_turn_ordinal;
```

这个查询不会读取中间 assistant 消息，也不会读取 `tool_calls.result_message_sequence` 指向的工具结果；如果 `message_projections.visible_text` 已经存在，Web 可以直接返回投影文本而不打开 JSONL。若 `final_message_sequence` 为空，说明该 Turn 尚未最终化，应按 `turns.status` 返回进行中或失败状态，而不是猜测最后一条 assistant 消息。

如果需要该 Turn 的完整细节，再执行：

```sql
SELECT message_sequence, role, jsonl_offset, jsonl_length
FROM messages
WHERE turn_id = 'turn-008'
ORDER BY message_sequence;
```

因此它主要解决三个问题：

1. 从 Turn 索引快速找到用户消息，而不扫描整个 `rollout.jsonl`；
2. 把用户消息作为前端 Turn 的稳定锚点，用于向上加载时生成 cursor；
3. 在 `rewind` 后验证这条用户消息是否仍被当前 view 的 range 包含，避免把旧后缀误当成当前上下文。

如果 `turn-008` 被新 view 隐藏，`user_message_sequence=101` 仍然保留在 `turns` 中作为历史事实，但 `RolloutContextReader` 会先检查 `context_view_ranges`，不会因为查到了物理行就把它返回给当前会话。

#### 情景二：加载最新一个可显示 Turn

最新 Turn 不能使用 `MAX(messages.message_sequence)`。例如压缩时最后追加了一条物理序号更大的 `compaction_summary`，它可能不是 Web 首屏要显示的普通 Turn。正确读取链路是：

```sql
SELECT active_branch_id
FROM checkpoint_namespace_state
WHERE checkpoint_ns = :checkpoint_ns;

SELECT head_view_id
FROM branches
WHERE branch_id = :active_branch_id
  AND status = 'active';

SELECT head_turn_id, head_message_sequence, logical_turn_count
FROM context_views
WHERE view_id = :head_view_id;

SELECT user_message_sequence, first_message_sequence,
       last_message_sequence, final_message_sequence
FROM turns
WHERE turn_id = :head_turn_id;

SELECT jsonl_offset, jsonl_length
FROM messages
WHERE message_sequence = :user_message_sequence;
```

`head_turn_id` 是“最新可显示的普通 Turn”；`head_message_sequence` 只是“当前 view 包含的最大物理消息序号”，在它指向压缩摘要或内部消息时，不能用来推断 Web 的最新 Turn。这样首屏才能稳定实现“最近 1 个 Turn 的用户消息 + tool_summary + final_response”。

#### 情景三：rewind 后从顶部向历史加载

假设原始 view `V1` 有 Turn-1 到 Turn-20。用户从 Turn-8 的 checkpoint rewind，产生 `V2`，其有效内容是 Turn-1 到 Turn-8；之后用户继续对话，新增 Turn-9' 到 `V3`。再次编辑 Turn-4 并重放后，当前 view `V4` 可能由以下范围组成：

```text
V4.range-0: source_kind = view,    source_view_id = V1,
           source_start_turn_ordinal = 1, source_end_turn_ordinal = 3,
           logical_start_turn_ordinal = 1, logical_end_turn_ordinal = 3
V4.range-1: source_kind = messages, start_message_sequence = 80,
           source_start_turn_ordinal = 4, source_end_turn_ordinal = 8,
           logical_start_turn_ordinal = 4, logical_end_turn_ordinal = 8
```

用户在当前 view 顶部继续滚动时，Reader 先按 `logical_start_turn_ordinal` 向前取 `V4.range-1`，读完后再沿 `source_view_id=V1` 继续取 `V1` 的更早范围。它不会从 JSONL 开头重新解析，也不会因为物理序号 80 大于原始消息序号而把旧分支拼回来。实现上应先把范围展开成迭代式读取计划，例如：

```text
ReadFrame(view_id = V4, range_id = V4.range-1,
          logical_turn_start = 4, logical_turn_end = 8)
ReadFrame(view_id = V1, range_id = V1.range-0,
          logical_turn_start = 1, logical_turn_end = 3)
```

#### 情景四：compaction 后区分 Agent 上下文和 Web 历史

假设 `V1` 有 Turn-1 到 Turn-20，压缩后追加一条 `turn_kind=compaction_summary` 的 assistant 消息，创建 `V2`：

```text
V2.range-0: 新增压缩摘要，visibility = model_only
V2.range-1: 引用 V1 中仍需保留的 Turn-18 到 Turn-20
```

Agent 的 `full` 模式可以读取压缩摘要和保留后缀，把摘要作为模型上下文的一部分；Web 的历史投影则把 `compaction_summary` 从普通 Turn 计数中排除，显示“历史已压缩”的边界提示，并继续允许用户加载归档 view 中的旧历史。此时：

- `context_views.head_turn_id` 指向最新普通 Turn，例如 Turn-21，而不是压缩摘要；
- `context_views.head_message_sequence` 可以指向物理上更晚的压缩摘要；
- `turns.user_message_sequence` 仍只用于定位对应用户消息，不承担“是否最终响应”的判断。

这使“模型当前有效上下文”和“用户仍可浏览的归档历史”可以使用同一套 SQLite 数据，但采用不同的 `include` 投影策略。

#### 情景五：用户编辑历史消息后 replay

原 view `V1` 包含 Turn-1 到 Turn-7。用户编辑 Turn-4 的文本并点击重放，业务层执行 `rewind + continue`，创建新 view：

```text
V5.range-0: 引用 V1 的 Turn-1 到 Turn-3
V5.range-1: 在 JSONL 尾部追加编辑后的用户消息、工具消息和新最终响应
```

旧 Turn-4 到 Turn-7 仍保留在 `messages` 和 `turns` 中，便于审计或旧 checkpoint 恢复，但不出现在 V5 的 range 中。新的 Turn-4' 具有新的 `turn_id` 和新的 `user_message_sequence`，所以历史读取不需要修改旧消息，也不会产生 `message_replace`。

#### 情景六：fork 后父 rollout 删除

所有 fork 模式由业务层最终只调用一次 provenance writer。`clone_rollout()` 只复制文件和 SQLite，不自行插入 `fork_origins`；否则完整复制模式会产生两条来源记录。writer 通过 `source_checkpoint_id` 查询父 SQLite 的 `checkpoints.view_id`，将真实 `source_session_id`、`source_checkpoint_id`、`source_view_id`、`fork_mode` 和 `relationship` 写入子 SQLite。

执行 `context_fork` 时，读取父 rollout 当前 `source_view_id` 的有效范围，将消息 materialize 到子 rollout 自己的 `rollout.jsonl`，并在子 SQLite 写入 `fork_origins` 作为来源记录。子会话之后只读取自己的：

```text
child checkpoint_namespace_state(checkpoint_ns).active_branch_id
  → child branches.head_view_id
  → child context_view_ranges
  → child messages
  → child rollout.jsonl
```

`fork_origins.source_view_id` 只用于显示“从哪里 fork”，不参与子会话后续上下文解析。因此父 rollout 删除后，detached 的子会话仍可正常对话。

当 `relationship = 'pinned'` 时，writer 使用同一个 `fork_id` 在父 SQLite 的 `retention_refs` 写入 active fork 保留记录，记录 `target_view_id`、子会话 `owner_session_id` 和 `reference_id = fork_id`。删除子会话时必须将该记录标记为 `released`；父会话删除和 pruning 都直接检查父 SQLite 的 active `retention_refs`，分别防止误删会话和误裁剪 view。

### 5. 必须建立的索引

```sql
CREATE INDEX messages_turn_index
    ON messages(turn_id, message_sequence); -- 按 Turn 读取消息

CREATE INDEX messages_role_index
    ON messages(role, message_sequence); -- 按 role 筛选消息

CREATE INDEX messages_offset_index
    ON messages(jsonl_offset); -- 按 JSONL 偏移定位消息

CREATE INDEX turns_ordinal_index
    ON turns(turn_ordinal); -- 按 Turn cursor 定位

CREATE INDEX context_views_head_turn_index
    ON context_views(branch_id, head_turn_id); -- 由 branch head 快速定位最新可显示 Turn，避免使用 MAX(message_sequence) 误选压缩摘要

CREATE INDEX context_view_turns_ordinal_index
    ON context_view_turns(view_id, logical_turn_ordinal); -- 按当前 view 的逻辑 Turn 序号快速执行 before/after/around 和 4/16/64 批次查询

CREATE INDEX context_view_turns_turn_index
    ON context_view_turns(turn_id, view_id); -- 从用户提交的 turn_id 反查所有包含该完整 Turn 的候选 view，再沿 active head 的祖先链选择最近可达 view；不能只按全局 control_sequence 取最大值

CREATE INDEX context_view_turns_user_message_index
    ON context_view_turns(view_id, user_message_sequence); -- 在已知 view 时从用户消息物理 sequence 反查 Turn；用户级操作仍优先使用 turn_id resolver

CREATE INDEX context_view_turns_final_message_index
    ON context_view_turns(view_id, final_message_sequence); -- 从 final_response 的物理 sequence 反查当前 view 中对应的 Turn，支持最终响应作为 cursor 锚点

CREATE INDEX tool_calls_message_index
    ON tool_calls(assistant_message_sequence, call_index); -- 读取 assistant 的工具摘要

CREATE INDEX checkpoints_view_index
    ON checkpoints(view_id); -- 由 checkpoint 定位 context view

CREATE INDEX checkpoint_channels_name_index
    ON checkpoint_channels(channel_name, checkpoint_id); -- 按 channel 查询一组 checkpoint，支持恢复 channel version、审计 channel 变化和调试非 messages 状态

CREATE INDEX checkpoint_channels_view_index
    ON checkpoint_channels(context_view_id); -- 从 messages channel 的 view 指针反查 checkpoint，支持 view 保留检查和历史恢复诊断

CREATE INDEX pending_writes_checkpoint_index
    ON pending_writes(checkpoint_id, status, task_path, task_id, write_index); -- 按 checkpoint、task path 和 task 有序恢复 pending writes，过滤已消费写入

CREATE INDEX context_ranges_logical_index
    ON context_view_ranges(view_id, logical_start_ordinal, logical_end_ordinal); -- 按逻辑消息范围读取

CREATE INDEX context_ranges_turn_index
    ON context_view_ranges(view_id, logical_start_turn_ordinal, logical_end_turn_ordinal); -- 按当前 view 的逻辑 Turn 区间实现 before/around 和 4/16/64 批次向前加载

CREATE INDEX context_ranges_source_turn_index
    ON context_view_ranges(source_view_id, source_start_turn_ordinal, source_end_turn_ordinal); -- 沿 view 链跳转到来源 view 的对应 Turn 范围

CREATE INDEX control_events_entity_index
    ON control_events(entity_type, entity_id, control_sequence); -- 查询对象的控制历史
```

#### 4.21 `fork_materializations`

`fork_materializations` 是子 rollout 的 fork 物化日志。它不是新的业务关系表，
而是把“目标 JSONL 已追加、目标 SQLite 已写入、Turn 终态已收敛、来源记录和
pinned retention 已完成”拆成可恢复的提交阶段。这样进程可以在任意阶段崩溃，
下次打开子 rollout 时不会把半个 fork 当成可继续对话的历史。

```sql
CREATE TABLE fork_materializations (
    materialization_id TEXT PRIMARY KEY, -- 作用：标识一次可恢复的 fork 物化尝试；场景：重启时定位并继续收敛或回滚同一项操作
    fork_id TEXT NOT NULL UNIQUE, -- 作用：预先分配最终 fork provenance ID；场景：目标提交和父 retention 重试时保持幂等，不能产生两条来源记录
    target_session_id TEXT NOT NULL, -- 作用：确认 journal 属于哪个子 rollout；场景：防止把其它会话的恢复记录写入当前 SQLite
    source_session_id TEXT NOT NULL, -- 作用：保存来源会话；场景：pinned 阶段重试父 retention，以及审计 fork 来源
    source_checkpoint_id TEXT, -- 作用：保存选择的源 checkpoint；场景：恢复或诊断时复现 fork 的非 messages channel 来源
    source_view_id TEXT, -- 作用：保存选择的源 context view；场景：诊断消息边界和 pinned retention 目标
    fork_mode TEXT NOT NULL, -- 作用：区分 context_fork/history_prefix_fork/full_rollout_copy；场景：恢复时判断目标是空库物化还是完整副本
    relationship TEXT NOT NULL, -- 作用：记录 detached/pinned；场景：目标提交后决定是否还要在父库建立 retention
    status TEXT NOT NULL, -- 作用：表示 prepared/target_committed/committed/aborted；场景：启动恢复只处理未完成阶段
    rollback_jsonl_offset INTEGER NOT NULL, -- 作用：记录本次 fork 开始前目标 JSONL 水位；场景：prepared 失败时截断目标尾部而不扫描消息
    copied_message_count INTEGER NOT NULL DEFAULT 0, -- 作用：记录提交时目标消息数量；场景：审计物化规模并校验 provenance
    error_message TEXT, -- 作用：保存显式失败原因；场景：启动恢复或维护界面报告半完成 fork，而不是静默忽略
    created_at TEXT NOT NULL, -- 作用：记录物化开始时间；场景：诊断长时间卡在 prepared 的 fork
    target_committed_at TEXT, -- 作用：记录目标 JSONL/SQLite/finalization/provenance 已同一事务收敛的时间
    committed_at TEXT -- 作用：记录父 retention 也完成后的最终时间；场景：判断整个 fork 是否可以对外使用
);

CREATE INDEX fork_materializations_status_index
    ON fork_materializations(status, created_at); -- 启动恢复和维护查询未完成物化
```

### 6. JSONL 与 SQLite 的提交协议

所有 rollout 写入必须由受控 `RolloutAppendWriter` 执行，并持有当前 rollout 的进程锁：

1. 开启 SQLite `BEGIN IMMEDIATE`，读取当前 `committed_jsonl_offset` 和消息序号。
2. 在同一事务中创建 `storage_commits` 的 `prepared` 记录，准备消息、投影、context view、checkpoint envelope 和全部 `checkpoint_channels` 行。
3. 在 JSONL 尾部追加完整消息并 flush/fsync。
4. 写入 SQLite 的 messages、投影、控制、view、checkpoint、全部 `checkpoint_channels` 行和 `database_meta` 更新；如果调用的是 `put_writes`，则以独立 SQLite commit 追加 pending writes。
5. 将 `storage_commits.status` 更新为 `committed`，提交 SQLite 事务。

如果进程在 SQLite 提交前崩溃，启动时根据 `database_meta.committed_jsonl_offset` 截断未提交 JSONL 尾部。若 SQLite 已提交，则 JSONL 在提交前已经 fsync，因此不得回退该消息。SQLite `integrity_check` 失败时必须进入 `recovery_required`，不得仅扫描 JSONL 伪造完整上下文。

读取使用 SQLite read transaction 创建 `RolloutReadSnapshot`，固定请求 namespace 的 `checkpoint_namespace_state.active_branch_id`、对应 `branches.head_view_id`、`projection_epoch`、`last_commit_id` 和目标 view。snapshot 同时持有会话目录下的 `.rollout.write.lock` 共享锁以及 SQLite connection；在历史 API 返回前必须关闭它们。写入、删除、fork 复制和离线 compaction 持有同一锁的独占锁，因此不会出现 JSONL 已经切换而 SQLite 仍读取旧 offset，或反过来的跨文件混合水位。SQLite connection 开启 WAL、foreign key 和 busy timeout；WAL 只改善 SQLite 内部读写并发，不能替代跨文件锁。

正常历史读取不执行全库 `PRAGMA integrity_check`，避免每次滚动把完整索引校验带入热路径；它只执行目标表查询、offset 对应行的 sequence 校验和 context view/range 局部校验。维护入口 `validate_index()`、SQLite backup、schema migration 和 restore 必须执行完整 integrity check，失败时进入 `recovery_required`。之后通过 `messages.jsonl_offset/jsonl_length` 读取命中的 JSONL 行，不扫描未命中的消息。任何公开返回 `RolloutReadSnapshot` 的维护接口都要求调用方使用上下文管理器或显式 `close()`，防止读锁长期阻塞写入。

历史 Turn 分页不先读取整个 `context_view_turns`。`tail`、`head`、`before` 和 `after` 使用 `(view_id, logical_turn_ordinal)` 复合索引做 keyset 查询：SQL 只取 `limit + 1` 行，额外一行仅用于计算 `has_more`；倒序查询完成后在内存中反转本页，使 API 始终按从旧到新的逻辑顺序返回。`around` 使用逻辑序号范围查询。游标保存的是逻辑 ordinal，不是 `messages.message_sequence` 的最大值，因此 rewind、compaction 追加物理消息后仍不会把控制消息误当成最新普通 Turn。

namespace 说明：上面的 snapshot 描述以 `checkpoint_namespace_state` 为准。`database_meta.active_branch_id` 仅保留为默认 namespace 的初始化 seed；运行时必须用请求的 `checkpoint_ns` 读取该表的 `active_branch_id` 和 `projection_epoch`，这样同一 thread 的多个 namespace 不会共享 active head。

### 7. Context view 的解析方式

`RolloutContextReader` 是 `RolloutCheckpointSaver` 内部的唯一历史读取实现。业务层只能调用 Saver；Saver 先从 checkpoint 或 active view 找到 `context_views`，再通过内部 reader 的 `context_view_ranges` 解析逻辑消息顺序。对 LangGraph checkpoint，Saver 还必须从同一个 checkpoint 的 `checkpoint_channels` 读取非 messages channel，并把 messages channel 的 view 指针交给内部 reader：

```text
checkpoint
  → context_view
  → context_view_ranges
  → source view / message sequence
  → messages offset
  → rollout.jsonl

checkpoint
  → checkpoint_channels
  → non-message serializer/value_blob
```

`source_kind=view` 表示引用另一个 view 的逻辑范围；`source_kind=messages` 表示引用 JSONL 的连续消息范围。范围解析不复制消息正文，也不需要 segment parent。

`context_view_jumps` 用于长链跳转和 cycle 检查。引用不存在、范围越界、view 成环、消息 sequence 不存在或 branch 不一致时，projection、detail、full 三种模式都必须返回同一种明确错误。

### 8. rewind、replay、compaction 和 fork

- `continue/resume`：追加新消息，创建新的 append context view 或 checkpoint，不修改旧 view。
- `rewind`：创建新 branch 和新 context view，引用目标 checkpoint/view 的前缀范围，隐藏旧后缀。
- `replay`：执行 rewind，再追加用户编辑后的新消息，最后继续执行；不生成消息 revision。
- 首个真实 checkpoint 可能收到 LangGraph 只存在于内存中的初始父 ID。若当前 checkpoint 已携带完整 `messages` 快照，Saver 必须按根 checkpoint 追加，不能把不可读取的父 ID 写入 SQLite；从该 checkpoint 发起 replay 时直接使用目标消息之前的当前快照前缀。只有当前 checkpoint 未携带 `messages` 且父状态不可读取时才失败，避免静默丢失上下文。
- `compaction`：在 JSONL 追加 assistant 形式的压缩摘要或必要上下文消息，SQLite 创建包含摘要和保留后缀范围的新 view。
- `context_fork`：将目标 checkpoint/view 的有效消息 materialize 后复制到子 rollout 的新 JSONL，并将源 checkpoint 的 `checkpoint_channels` 非 messages 状态、channel versions、`versions_seen`、原始 `pending_sends` 和 pending writes 复制到子 SQLite 的初始 checkpoint。
- `history_prefix_fork`：复制会话起点到 anchor 的有效消息前缀和各个源 checkpoint 的 channel 状态；每个子 checkpoint 都保留对应的 `pending_sends` 和 pending writes。没有可恢复的源 checkpoint 时明确失败，不用空 channel 伪造可执行状态。
- `full_rollout_copy`：显式复制父 rollout 的全部消息和 SQLite checkpoint/view/branch 状态，包括所有 checkpoint、`checkpoint_channels` BLOB、pending writes 和 view/branch 关系；但不复制父库的 `fork_origins` 与 `retention_refs`，因为这两张表保存的是原会话的 child/source/owner 关系，复制后会产生错误的父子关系和 retention 占用。复制完成后由统一 writer 为新子会话写入唯一一条新的 provenance，并按需在父库创建新的 pinned retention。

三种模式都先在目标 SQLite 写入 `fork_materializations(status='prepared')`。消息和 checkpoint
物化期间允许使用已有的 append/clone 提交流程，但目标 rollout 对外不可用；目标 JSONL、
checkpoint channel、pending state、Turn finalization、运行态终止和唯一
`fork_origins` 在一次目标 SQLite 事务中收敛，并把 journal 更新为
`target_committed`。随后为 pinned fork 幂等写入父库 `retention_refs`，成功后再把 journal
更新为 `committed`。如果进程在 `prepared` 阶段崩溃，启动恢复按
`rollback_jsonl_offset` 清理目标的半成品；如果停在 `target_committed`，恢复只重试父
retention 和最终状态更新。任何未完成 journal 都不会被历史 reader 或 LangGraph saver
当作有效子会话返回。

三种模式共享上述 fork 锚点规则：默认省略 `turn_id` 时先定位最近已完成 Turn；显式 `turn_id` 指向运行中的 Turn 时全部拒绝。`full_rollout_copy` 仍然是显式的完整物理副本模式，不改变父 rollout；它不能被用来绕过运行中 Turn 的用户锚点校验。

父 rollout 删除后，`context_fork` 和 `history_prefix_fork` 不再读取父数据。`pinned` 关系只通过 `fork_origins` 和 `retention_refs` 阻止父会话删除。

### 9. 历史 API 和 Web 投影

历史 API 的 LoadPlan 仍使用：

```text
direction: head / tail / before / after / around
anchor: opaque cursor 或 turn_id/message_id
unit: turn 或 message
include: user / text / reasoning_summary / reasoning_detail /
         encrypted_reasoning_meta / tool_summary / tool_call / tool_result /
         final_response / internal / metadata
projection: summary / detail / full
limits: turns / records / bytes / chars / item_chars / detail_batch
```

`text` 是统一 response part 中的可见文本，不再把 `assistant_text` 当作 canonical 事件类型；`reasoning_summary`、`reasoning_detail` 和 `encrypted_reasoning_meta` 都从 `reasoning_blocks` 派生。默认 Web 首次读取最新 1 个 Turn，返回 `user + reasoning_summary + tool_summary + final_response`；继续向上滚动依次读取 4、16、64 个 Turn，旧批次只返回 `user + final_response`。工具详情只读取 `tool_calls` 对应的 JSONL offset，当前 Turn 立即重新加载，工具区域保持折叠。历史 summary/detail 的响应都转换为与 live 相同的 `TurnResponsePartDTO`，区别仅在 `projection` 和是否含有中间细节。

cursor 由 `rollout_id`、`view_id`、`projection_epoch`、方向、逻辑 Turn 位置和 include 策略组成。cursor 不需要单独持久化；服务端通过 SQLite 当前 view 和 projection epoch 校验它。

### 10. SQLite 迁移

SQLite schema 通过 `schema_migrations` 逐版本迁移：

1. 对数据库加写锁并执行 `PRAGMA integrity_check`。
2. 创建 SQLite backup。
3. 在一个事务中执行当前版本的 migration。
4. 更新 `database_meta.schema_version`。
5. 写入 `schema_migrations.completed`。
6. 迁移失败时回滚事务并恢复 backup，数据库进入明确错误状态。

rollout JSONL 消息格式应保持稳定。若未来必须改变 canonical message 格式，不能只升级 SQLite；应显式创建新的 message format 迁移方案或新的 rollout 格式版本。

### 11. Pruning

单个 append-only JSONL 不能安全地原地删除中间消息。SQLite 可以立即执行逻辑 pruning：从 context view、checkpoint、branch、fork 和 retention 引用中移除不可见范围。物理回收只能作为显式离线 compaction：按 active checkpoint 的有效 view 生成临时 JSONL，保留旧 JSONL 与 SQLite backup，先写入 `compaction_runs` journal，再原子替换文件并在同一个 SQLite 提交中更新所有 offset；成功后删除临时文件。它不创建 segment，也不改变任何 message sequence、message_id 或有效 view。

本 change 不把物理回收当作普通历史读取路径的一部分；读取只依赖 SQLite 当前有效 view。

## Risks / Trade-offs

- [SQLite 损坏不能仅凭 JSONL 恢复控制状态] → SQLite 是权威数据，必须提供 SQLite backup 和 `recovery_required` 错误；不能返回伪造的线性历史。
- [单个 rollout.jsonl 会持续变大] → 通过 SQLite offset 选择性读取；物理回收只由显式离线 compaction 完成。
- [非 messages channel 的完整快照可能在多个 checkpoint 重复占用 SQLite 空间] → 这些 channel 通常是有界状态；写入时记录 value hash 和长度，后续可在 SQLite 内增加按 hash 的值去重，但本 change 不把大型消息正文重新放入 channel BLOB。
- [流式消息若每个 chunk 持久化会重新产生 O(n²)] → 流式期间只在内存中累积，完成稳定消息后一次追加；崩溃允许丢失未完成消息。
- [context view 链可能变长] → 使用 `context_view_ranges` 和 `context_view_jumps`，并在 compaction 时创建新的 view 基线。
- [JSONL 与 SQLite 不是单一原子文件] → 文件先行、SQLite 后提交，使用 committed offset 清理未提交尾部。
- [SQLite 投影算法升级可能需要重建投影] → 保留 `projection_version`，由 SQLite migration 或显式 repair 重新生成轻量投影。
- [父 rollout 物理删除会影响未独立 fork] → `fork_origins` 和 `retention_refs` 在删除前检查 pinned 或未完成物化关系。

## Test Strategy

### 测试工作区和常驻 fixture 边界

测试资源统一来自只读资产工作区：

```text
asset/custom_tool_test_workspace/
└── .boxteam/sessions/
    ├── ses_8128.../  # 真实模型 128 Turn：多 provider、reasoning/summary/encrypted
    ├── ses_9f4e.../  # 确定性 mock：大型工具调用和固定 Web 投影
    ├── ses_4c0a.../  # compaction/summary
    ├── ses_6b2d.../  # fork/独立分支
    └── ses_7e5d.../  # 多工具调用和大输出
```

每个会话都必须只有 `rollout/rollout.jsonl`、`rollout/index.sqlite` 和会话 manifest；资产目录不得包含旧 Trace/TurnHistory 历史文件。正式测试通过 `prepare_default_test_workspace` 或测试 fixture 将整个工作区复制到 `out/tests/.../workspace`，测试只读复制品，不调用 fixture 生成脚本，也不修改 `asset/`。

真实模型生成器和确定性 mock 生成器必须使用不相交的 session ID。真实 128 Turn 会话的 ID 固定为 `ses_8128d7f0a4b64aa0b3f1c9e7d2a65018`；mock 生成器不得把它作为自己的 `LONG_SESSION_ID`，否则显式重新生成 mock 时会覆盖真实验收样本。资产清单、导航索引、生成器常量和测试常量必须保持一致。

- SQLite schema 测试覆盖所有表、字段约束、migration、backup、`PRAGMA integrity_check` 和版本升级失败恢复。
- rollout writer 测试覆盖消息追加、JSONL fsync、SQLite 提交、未提交尾部截断和重复写入保护。
- context view 测试覆盖 normal append、rewind、before/inclusive、replay、compaction、多级 range、jump 和 cycle 错误。
- `context_view_turns` 测试覆盖 view 创建时的原子物化、`turn_id` 反向锚点定位、前后 4 个 Turn 查询、隐藏 Turn 不可见、message-level compaction summary 排除和从 SQLite 基础表重建派生表。
- checkpoint saver 测试覆盖最新/历史 checkpoint、parent lineage、messages view 指针、多个非 messages channel、None 与 absent 区分、serializer/hash、`versions_seen`、`pending_sends`、pending writes、同步/异步接口和 view 固定性。
- 历史 API 集成测试覆盖用户消息 + tool summary + final response、用户消息 + tool call + tool result + final response、游标前后 Turn 和 bounded detail。
- fork 测试覆盖三种模式、父 rollout 删除、detached/pinned 关系和 retention 引用。
- 128 Turn fixture 覆盖普通 assistant、reasoning summary、encrypted reasoning、tool_call/tool_result 和 finalization。
- Web 测试覆盖最新 1 Turn、4/16/64 渐进加载、顶部 pending、prepend 锚点、工具折叠和当前 Turn 详情重载。

## Migration Plan

1. 先实现 `rollout.jsonl` canonical message writer 和 SQLite v1 schema。
2. 实现集中 `RolloutDatabase`、migration、提交协调和 offset reader。
3. 实现 message、Turn、tool、reasoning 投影和 finalization。
4. 实现 context view、range、jump、`context_view_turns` 派生物化、checkpoint 和 branch。
5. 实现 `RolloutContextReader` 的 projection/detail/full 三种模式。
6. 实现 `RolloutCheckpointSaver`、rewind、replay、compaction 和三种 fork。
7. 切换历史 API、Gateway 配置和 Web 默认加载。
8. 删除旧 segment、JSONL control event、旧 message operation 和旧历史 fixture。
9. 运行 schema migration、后端集成、Web 构建和浏览器回归。

回滚时只能停止创建新格式并恢复代码版本；旧 saver 不得尝试读取新 SQLite 权威数据库或新 rollout JSONL。

## Open Questions

无。SQLite 权威性、单 rollout JSONL、表结构和字段含义在本 change 中先固定，后续讨论应以增量修订这些表为准。
