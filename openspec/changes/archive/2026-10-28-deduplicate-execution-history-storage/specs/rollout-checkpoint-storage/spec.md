## MODIFIED Requirements

### Requirement: SQLite 保存完整 LangGraph checkpoint envelope 和逐 channel 状态

系统 SHALL 在 `checkpoints` 保存 checkpoint_id、namespace、parent checkpoint、branch、view、commit、checkpoint version、timestamp、metadata、`versions_seen` 和 `pending_sends`。系统 SHALL 在 `checkpoint_channels` 为 `channel_values`、`channel_versions` 和 `updated_channels` 的并集保存每个 channel 一行。

`messages` channel 的行 SHALL 使用 rollout view 指针，不保存完整 messages 数组；其它 channel SHALL 通过明确历史 owner、namespace、channel 与 version 的不可变正文引用保存序列化值、值状态、serializer、长度和 hash，checkpoint 保存完整逻辑快照所需的引用与 updated 顺序；仅新版本保存正文，未变化的大对象不重复序列化。checkpoint metadata、channel BLOB、`versions_seen` 和 `pending_sends` 都不得重复保存完整 messages 数组。

#### Scenario: 恢复最新 checkpoint 的全部 channel

- **WHEN** LangGraph 请求一个 thread 的最新 checkpoint，且 checkpoint 同时包含 messages、计数器、任务状态和 provider 状态 channel
- **THEN** saver 从 SQLite 恢复 checkpoint envelope、逐 channel version、`updated_channels`、所有非 messages channel 和 pending writes，并通过 messages channel 的 view 指针 materialize 可执行的 LangChain BaseMessage 列表

#### Scenario: messages channel 使用 checkpoint view

- **WHEN** `checkpoint_channels` 中存在 `channel_name=messages` 的 channel 行
- **THEN** 该行或其增量基准引用必须定位该 checkpoint 冻结的 messages selection view 与 canonical 正文，恢复时只从该 checkpoint 对应的历史选择/增量链读取，不读取当前 active view，不把 canonical 审计 view 混作有效消息选择，也不从 checkpoint BLOB 读取完整 messages 数组

#### Scenario: 区分缺失 channel 和 None channel

- **WHEN** 一个非 messages channel 没有出现在 `channel_values`，或明确保存了 Python `None`
- **THEN** 前者以 `value_state=absent` 恢复为缺失 channel，后者以 `value_state=present` 解码为 None，不得把两种状态混淆

#### Scenario: 未变化的非 messages channel 仍属于当前快照

- **WHEN** 新 checkpoint 中某个非 messages channel 的值与 parent checkpoint 相同
- **THEN** 当前 checkpoint 保存自己的 `checkpoint_channels` 行与同一不可变版本引用，不再次保存正文；恢复独立对象，不隐式读取 parent/latest channel 来代替当前快照的明确引用

#### Scenario: 恢复历史 checkpoint

- **WHEN** 调用方请求已经被后续消息覆盖的历史 checkpoint
- **THEN** 系统使用该 checkpoint 固定的 view 和对应 channel rows，而不是当前 active view 或最新 channel value

#### Scenario: checkpoint namespace 隔离

- **WHEN** 同一 thread 在多个 `checkpoint_ns` 下保存 checkpoint，并分别调用最新、按 ID 和列表查询
- **THEN** 每次查询都必须把请求 namespace 作为 checkpoint 条件；不同 namespace 的 checkpoint、parent、channel 和 pending writes 不得互相命中
- **AND** namespace 不存在时返回该 namespace 的空结果或明确缺失错误，不得借用另一个 namespace 的 active head

#### Scenario: 恢复 checkpoint-level 状态

- **WHEN** checkpoint 包含 `versions_seen` 或 `pending_sends`
- **THEN** `get_tuple` 使用 checkpoint 保存的 serializer、长度和 hash 恢复原始结构；serializer 缺失或 hash 不匹配时返回明确 checkpoint 数据错误

#### Scenario: 版本引用冲突或缺失

- **WHEN** 同一历史 owner/namespace/channel/version 被提供不同内容，或引用的正文缺失、serializer/长度/hash不匹配
- **THEN** 明确报错且不发布当前checkpoint，不覆盖既有值，不静默继承parent或最新值

#### Scenario: 官方增量消息 channel

- **WHEN** channel协议表示当前步没有新snapshot，而状态需要通过基准与祖先writes恢复
- **THEN** 保留版本、官方snapshot计数及重放顺序，区分增量缺snapshot、absent、None与空消息；同步和异步恢复一致，已知依赖缺失必须失败
