## Why

当前 checkpoint 对不断增长的 `messages` channel 在多个 checkpoint 中重复保存完整数组，长会话会产生近似 O(n²) 的写入空间和序列化成本，并使历史 checkpoint、上下文压缩、rewind、编辑重放和 fork 互相耦合。需要在保留 LangGraph `BaseCheckpointSaver` 语义的同时，将消息状态改为可恢复的 rollout 增量日志，使会话摘要、任意历史读取和上下文分支不再依赖完整 checkpoint 扫描。

## What Changes

- 新增每个 rollout 一个可直接打开的 `rollout.jsonl` 和一个权威 `index.sqlite`；JSONL 只追加不可变的 `user`、`assistant`、`tool` canonical message，SQLite 保存全部上下文控制、版本、checkpoint、branch、fork 和投影索引。
- 删除 semantic segment 文件、JSONL 控制记录、`message_replace`、`message_truncate`、`message_key`、message revision 和 `parent_segment_id`；上下文变化只在 SQLite 中创建不可变 context view。
- SQLite 不再是可从 JSONL 重建的缓存索引，而是 checkpoint、rewind、fork、compaction、历史 cursor 和有效消息范围的权威数据库；SQLite 损坏时必须从 SQLite 备份恢复，不能仅凭 JSONL 伪造完整上下文。
- 通过 SQLite 的 context view/range/jump 索引快速重建任意 checkpoint、rewind、replay 和 fork 上下文；读取时只根据 SQLite 的 JSONL offset/length 选择性读取消息。
- 新增唯一的 `RolloutCheckpointSaver` 数据访问入口：工作区级 `RolloutCheckpointRuntime` 统一组装并持有 `RolloutContextReader`、`RolloutAppendWriter`、`RolloutStorage` 和 Saver，Saver 统一提供 checkpoint、Web 历史、fork、rewind、replay、compaction 和 Turn 状态 API；业务层不得直接注入或调用低层组件，也不得直接扫描 rollout。
- 新增自定义 `RolloutCheckpointSaver(BaseCheckpointSaver[str])`，其中 `BaseCheckpointSaver` 来自 LangGraph；它将 `messages` channel 的不可变消息追加到 rollout，并在 `get_tuple` 时通过 SQLite context view materialize LangChain 的 `BaseMessage` 消息。
- 将 `checkpoint_channels` 定义为每个 checkpoint 的逐 channel 权威表：`messages` channel 保存 rollout context view 指针，其它 LangGraph channel 保存 serializer、channel version、值状态、序列化 BLOB、长度和哈希；`get_tuple` 必须从该表恢复所有非 messages channel，不能只恢复 messages。
- 在 `checkpoints` 中保存完整 checkpoint envelope 的 checkpoint version、timestamp、`versions_seen` 和 `pending_sends`；`checkpoint_channels` 保存 `channel_versions` 与 `updated_channels`，保证自定义 saver 可以等价实现 LangGraph checkpoint 存取契约。
- 支持从头、从尾、游标前后和中心位置加载多轮对话；加载内容可按用户消息、工具摘要、工具调用、工具结果和内部消息策略选择，并设置服务端大小上限。
- 为 Web 会话时间线提供简单的默认渐进加载：首次只加载最新 1 个 Turn 的用户消息、可展示 `reasoning_summary`、`tool_summary` 和最终响应；用户滚到顶部并继续滚动时，按 4、16、64 个 Turn 逐级向前加载，后续批次只加载用户消息和最终响应。
- 将默认历史加载策略放入 Gateway 内联配置的可扩展嵌套配置中；会话所属 Gateway 负责解析策略，其他 Gateway 通过代理加载该会话时也必须使用会话所属 Gateway 的配置。
- 工具详情开关只作用于当前 Turn，打开后立即重新加载当前 Turn 的 `tool_call` 和 `tool_result`；工具区域默认折叠，不增加工作区级 pin 或其它持久化前端开关。
- 支持 compaction、rewind、continue/resume、replay 和上下文 fork 的统一 SQLite context view；控制记录不作为 LangChain 消息注入模型。
- 用户可见的 fork、rewind、replay 和历史定位只提交稳定的 `turn_id`；后端从当前 active head 沿 view lineage 解析实际 source view/checkpoint。`turn_id` 是用户定位层，不取代 compaction 所需的 `message_id`/`message_sequence` 精确边界。
- 支持三种 fork 模式：默认复制有效上下文快照的独立 fork、复制历史前缀的独立 fork、完整复制父 rollout 的独立 fork。
- `rewind` 沿用同一个物理 rollout，回退 active head、创建新的 SQLite context view 和逻辑 branch；`continue/resume` 从当前 head 继续追加；面向用户的 `replay` 是 `rewind + 追加新的编辑消息 + continue`，旧 view 保留至可观测的 pruning 条件满足后再清理。
- fork 来源只作为 provenance metadata 保存；默认 fork 不依赖父 rollout 的文件或索引，父会话删除后子会话仍可继续运行。
- 增加 active head、projection epoch、JSONL committed offset、SQLite 事务提交边界和数据库迁移规则；崩溃时允许丢弃未提交 JSONL 尾部，但不得静默破坏已提交 SQLite 状态。
- **BREAKING**：大型 assistant/tool 消息完整内联在 `rollout.jsonl`；SQLite 只保存消息 offset/length、轻量投影、工具摘要、checkpoint channel BLOB 和上下文控制数据，不生成 `payload_ref`、外置消息正文或 `rollout/payloads/`。
- **BREAKING**：SQLite 成为控制语义和版本记录的权威数据源；数据库 schema 通过集中 migration 升级，rollout JSONL 消息格式保持稳定。
- `RolloutCheckpointSaver` 是 rollout/checkpoint 数据的唯一业务访问入口；每个工作区后端的 `AppContainer` 只创建一个工作区级 `RolloutCheckpointRuntime`，由 Runtime 统一组装并注入共享的 `RolloutStorage`、`RolloutAppendWriter`、`RolloutContextReader`、历史 DTO reader 和 Saver。业务服务只接收 Runtime 暴露的 Saver；低层组件的对象引用和 primitive 不得泄漏到业务层。TraceEventStore 仍可独立保存实时诊断事件，但不得作为 checkpoint、Web 历史或 fork 数据源。
- **BREAKING**：替换现有基于完整 checkpoint `messages` channel 的持久化布局和历史读取路径；原型阶段不提供旧数据迁移兼容。

## Capabilities

### New Capabilities

- `rollout-checkpoint-storage`: 定义 rollout JSONL、SQLite 权威控制数据库、checkpoint 恢复和崩溃安全提交语义。
- `checkpoint-history-loading`: 定义按方向、游标、上下文视图和内容包含策略读取历史的接口与有界行为。
- `checkpoint-context-branching`: 定义 compaction、rewind、continue/resume、replay 组合动作、三种 fork 模式、branch active head 和 pruning 语义。

### Modified Capabilities

无。

## Impact

- 后端 checkpoint：`app/core/checkpoint_saver.py`、checkpoint 配置、上下文压缩、rewind/continue/replay/fork 服务。
- 持久化基础设施：新增 rollout 目录、单文件 JSONL、SQLite 权威数据库、迁移和恢复逻辑；SQLite 还负责完整 LangGraph checkpoint envelope 和逐 channel 状态。
- API 与业务服务：历史加载、checkpoint 查询、上下文 fork、编辑重放和压缩操作需要使用新的上下文视图。
- Web 前端与 Gateway 配置：`src/clients/web` 实现默认历史时间线加载、顶部继续滚动触发、工具区域折叠和当前 Turn 工具详情重载；Gateway 内联配置和 schema 提供可扩展的渐进加载策略，并由会话所属 Gateway 对本地与远程 Gateway 请求统一生效。
- 测试：新增 checkpoint 增量写入、任意历史读取、压缩后 fork、rewind branch、replay 组合动作、父 rollout 删除、pruning 和崩溃尾部恢复测试。
- 本次覆盖后端 checkpoint、SQLite schema、历史读取接口、Gateway 默认加载策略和 Web 默认历史 UI；测试资产统一放在 `asset/custom_tool_test_workspace/`，其中 `ses_8128d7f0a4b64aa0b3f1c9e7d2a65018` 是预生成的真实模型 128 Turn 会话，其余会话是按 compaction、fork、工具详情和大型工具投影划分的确定性 mock。测试运行时只复制整个资产工作区到 `out/tests/.../workspace`，不得调用生成器或写回 `asset/`；浏览器回归可以使用固定大型工具 mock，不依赖实时模型调用。
- Trace/logs/messages 可以继续承担诊断、实时事件和其它非历史展示职责，但不得再作为 Web 会话历史的权威来源；Web 历史必须直接从 rollout JSONL 和 SQLite 索引读取。不得把 Gateway 或工作区业务状态迁移到全局存储。

## 当前修订范围

本次修订纠正此前实现与本 change 设计之间的脱节。原 `optimize-long-session-turn-loading` change 描述的 Trace/TurnHistory 投影方案保留为历史记录，不再作为本 change 的实现依据。

本 change 的历史加载切换必须完整落地到：

- `rollout/rollout.jsonl`：不可变的 user/assistant/tool canonical message；
- `rollout/index.sqlite`：消息 offset、Turn、checkpoint、context view、branch、fork、工具投影和游标定位的权威数据库；
- rollout-backed history reader：后端 `/bootstrap`、`/history` 和 Web 默认时间线的唯一历史读取入口。

因此，旧的 `TraceEventStore`、`TurnHistoryStore`、`turn_history/turns/*.json` 以及只向这些文件写入 fixture 的 `test_session_history_loading` 不再满足本 change 的验收要求。仍有诊断价值的 Trace 写入可以保留，但不得被历史 API 读取；旧历史投影测试和 fixture 应删除或改写为 rollout fixture。历史接口测试不在运行时调用实时模型；真实模型只通过 `generate_real_model_rollout_fixture.py` 预先生成常驻数据，用于读取性能和模型切换投影审查。确定性 mock 生成器不得复用真实会话 ID，避免重新生成 mock 时覆盖真实样本。
