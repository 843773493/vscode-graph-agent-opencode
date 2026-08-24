## 1. Rollout 和 SQLite 基础

- [x] 1.1 定义单文件 `rollout/rollout.jsonl` 的 canonical message envelope、sequence、message_id、turn_id 和 `user/assistant/tool` role 校验
- [x] 1.2 定义 SQLite `database_meta`、`schema_migrations`、schema version 和 message format version
- [x] 1.3 创建完整 SQLite v1 schema：`database_meta`、`schema_migrations`、`storage_commits`、`control_events`、`messages`、`message_projections`、`tool_calls`、`reasoning_blocks`、`turns`、`branches`、`context_views`、`context_view_ranges`、`context_view_jumps`、`context_view_turns`、`checkpoints`、`checkpoint_channels`、`pending_writes`、`fork_origins`、`retention_refs`、`fork_materializations`；其中 `checkpoints` 保存完整 checkpoint envelope，`checkpoint_channels` 为每个 checkpoint/channel 保存统一 channel 状态
- [x] 1.4 集中定义全部表名、列名、枚举值、约束、索引和 SQL 参数，禁止业务模块散落 SQLite primitive
- [x] 1.5 实现 SQLite 连接初始化、foreign key、事务、WAL、busy timeout、跨文件读快照和写锁；读写连接在损坏数据库上先做只读 schema 探测，禁止 WAL pragma 把坏库重新初始化成空库
- [x] 1.6 实现 schema migration runner、migration checksum、事务回滚和 SQLite backup
- [x] 1.7 为所有表字段约束、schema version、migration 成功/失败和 backup 恢复补充单元测试

## 2. JSONL 写入和跨文件提交

- [x] 2.1 实现受控 `RolloutAppendWriter`，只允许追加稳定的 user/assistant/tool canonical message
- [x] 2.2 实现流式 assistant 和工具参数的内存累积，禁止每个 chunk 追加完整消息
- [x] 2.3 实现 `storage_commits` prepared/committed/aborted 状态和 JSONL fsync → SQLite transaction 提交流程
- [x] 2.4 实现 committed JSONL offset、message sequence 和 control sequence 的一致更新
- [x] 2.5 实现崩溃恢复：忽略或截断未提交 JSONL 尾部，不回退已提交 SQLite 状态
- [x] 2.6 实现 committed JSONL offset、消息 hash、SQLite integrity check 和数据库损坏时 recovery_required 错误
- [x] 2.7 为同进程/跨进程并发写入、read snapshot 阻塞写入、fsync 失败、SQLite 提交窗口、重复事务和半行尾部补充测试

## 3. 消息、Turn 和投影索引

- [x] 3.1 实现 `messages` offset/length/hash 索引，保证大型正文不复制到 SQLite
- [x] 3.2 实现 Turn 按用户消息分隔、first/last message、user message 和 status 索引
- [x] 3.3 实现 `turns.final_message_sequence` 和 SQLite finalization 控制，不在 JSONL 写入 finalization record
- [x] 3.4 实现 message projection、统一 text/final_response 和 internal visibility 投影；`assistant_text` 只能作为派生字段
- [x] 3.5 实现 tool_calls/tool result 关联、工具名称/状态摘要和 bounded detail offset 读取，明确 `call_index` 只表示同一 assistant 内的调用顺序
- [x] 3.6 实现 reasoning_blocks 的 carrier_type、content_block_index、item_index、reasoning/summary/encrypted 投影，禁止默认返回 encrypted payload
- [x] 3.7 实现大型 assistant/tool message、工具参数、工具结果和 reasoning 正文只保存在 JSONL 的断言
- [x] 3.8 为混合 assistant message、中间 assistant、final response、工具摘要、工具详情和 reasoning 脱敏补充测试

## 4. LangGraph checkpoint 接入

- [x] 4.1 实现继承 `langgraph.checkpoint.base.BaseCheckpointSaver[str]` 的 `RolloutCheckpointSaver`
- [x] 4.2 将 messages channel 转换为不可变 JSONL message append，不保存完整 messages 数组和消息 revision
- [x] 4.3 将 checkpoint envelope、parent checkpoint、metadata、context view 和全部 `checkpoint_channels` 行写入同一 SQLite commit；`put_writes` 使用关联 checkpoint 的独立事务追加 pending writes
- [x] 4.4 实现 `checkpoint_channels` 的逐 channel 状态：messages 的 rollout view 指针、其它 channel 的 serializer/value BLOB/value_state、channel version、updated_index、长度和 hash；同时保存 `versions_seen`、`pending_sends` 和 pending write 的 task path/校验值
- [x] 4.5 实现 `put`、`put_writes`、`get_tuple`、`list` 及同步/异步变体的配置契约
- [x] 4.6 实现历史 checkpoint 固定使用写入时的 context view，不被 active view 覆盖
- [x] 4.7 为最新/历史 checkpoint、parent lineage、messages view 指针、多个非 messages channel、None 与 absent 区分、`versions_seen`、`pending_sends`、pending writes 和同步/异步接口补充集成测试

## 5. SQLite context view 和 branch

- [x] 5.1 实现不可变 `context_views`，支持 initial、append、rewind、compaction、fork view kind
- [x] 5.2 实现 `context_view_ranges` 的 view range 和 JSONL message range 两种 source
- [x] 5.3 实现 `context_view_jumps` 的祖先跳转、范围查询、cycle 检查和越界检查
- [x] 5.4 实现 branch active head、source view、source checkpoint 和 projection epoch
- [x] 5.5 实现 rewind 的 inclusive/before anchor，隐藏旧后缀但不修改旧消息
- [x] 5.6 实现 compaction 摘要消息、保留后缀和新 context view
- [x] 5.7 实现 replay = rewind + 新编辑消息追加 + continue，不生成 replace/truncate/revision
- [x] 5.8 实现 continue/resume 在当前 view 中追加，不无故创建 branch
- [x] 5.9 为多级 view range、rewind inclusive/before anchor 跨旧后缀、compaction 专用 view、replay、非法引用和 projection epoch 补充集成测试

## 6. 统一历史读取

- [x] 6.1 实现 `RolloutContextReader` 作为 `RolloutCheckpointSaver` 内部的 checkpoint、Web history 和 fork context 读取实现
- [x] 6.2 实现一次 read snapshot 内的 SQLite view/range 解析和 message offset 计划
- [x] 6.3 实现 `projection` 模式：只读取 SQLite 投影和必要的小型 JSONL message
- [x] 6.4 实现 `detail` 模式：按 Turn 读取 bounded tool_call/tool_result 和继续 cursor
- [x] 6.5 实现 `full` 模式：沿 context view ranges 返回完整 LangChain BaseMessage 列表
- [x] 6.6 禁止 `RolloutHistoryReader`、saver、fork service 和业务服务直接组合 SQLite/JSONL 低层读取
- [x] 6.7 实现 head、tail、before、after、around 五种方向；使用 `(view_id, logical_turn_ordinal)` keyset 分页，保证 Turn 不拆分、去重和边界状态
- [x] 6.8 为 projection/detail 不调用 full、复杂 view 仍使用同一 reader 和非法 view 统一失败补充测试

## 7. 历史 API、Gateway 和 Web

- [x] 7.1 将 `/bootstrap` 和 `/history` 切换到 rollout JSONL + SQLite reader，禁止 Trace/TurnHistory fallback
- [x] 7.2 实现用户消息、text、reasoning_summary/reasoning_detail、encrypted_reasoning_meta、tool_summary、tool_call、tool_result、final_response 和 internal 的独立 include；`assistant_text` 仅作为派生别名
- [x] 7.3 在 Gateway 配置中实现四层以上渐进配置，默认批次为初始 1、向前 4/16/64，最后阶段重复
- [x] 7.4 确保 Gateway-to-Gateway 代理使用会话所属 Gateway 的历史策略
- [x] 7.5 实现 cursor 绑定 rollout、view、branch、projection epoch 和逻辑位置，旧 view 变更返回 stale 错误
- [x] 7.6 实现 Web 首次只加载最新 1 个 Turn，默认显示 user、reasoning_summary、tool_summary、final_response
- [x] 7.7 实现顶部 pending、继续滚动加载 4/16/64 Turn、prepend 去重和滚动锚点恢复
- [x] 7.8 实现当前 Turn 工具详情立即重载，默认折叠且不影响其它 Turn、会话和 Gateway 策略
- [x] 7.9 使用 rollout-backed stub API 补充后端 API、Gateway 配置和 Web 时间线测试，并运行 Web build

## 8. Fork、保留和 pruning

- [x] 8.1 实现 `context_fork`：复制 effective context 消息、源 checkpoint 的全部非 messages channel/envelope 状态、原始 `pending_sends` 和 pending writes 到子 rollout，建立独立 SQLite 初始 view/checkpoint
- [x] 8.2 实现 `history_prefix_fork`：按 checkpoint 顺序复制会话起点到 anchor 的有效消息前缀和每个源 checkpoint 的 channel/pending 状态；无源 checkpoint 时明确失败
- [x] 8.3 实现 `full_rollout_copy`：复制父 rollout.jsonl 和 checkpoint/view/branch 控制状态，包括 checkpoint_channels BLOB、versions_seen、pending_sends 和 pending writes；不复制父库的 fork_origins/retention_refs 关系表，复制步骤不重复写入 fork provenance
- [x] 8.4 通过统一 writer 写入唯一一条 `fork_origins` provenance，保存真实 source session/checkpoint/view；默认 fork 不依赖父 rollout 或父 SQLite
- [x] 8.5 实现 detached/pinned relationship；pinned fork 在父 SQLite 写入 `retention_refs`，子会话删除时释放并阻止父 pruning/删除
- [x] 8.6 实现 logical pruning，检查 checkpoint、view、branch、fork、audit 和 pinned 引用
- [x] 8.7 将物理 JSONL 回收限制为显式离线 compaction，不在普通历史读取路径中执行
- [x] 8.8 为三种 fork、非 messages channel 状态复制、父删除、pinned 阻塞、anchor 语义、retention 和 pruning 补充集成测试

## 9. 业务接入和旧实现清理

- [x] 9.1 将 Job 合并、steering、内部消息、子 Agent、工具调用/结果、compaction、rewind、continue/resume、replay 和 fork 接入新 saver/view
- [x] 9.2 删除旧 FileSystemCheckpointSaver、旧 checkpoint migration、旧 message operation 和旧 Trace/TurnHistory 历史读取路径
- [x] 9.3 删除 segment header、segment checksum、parent_segment_id、message_replace、message_truncate 和 message revision 实现
- [x] 9.4 更新 checkpoint/history 测试 fixture 为 `rollout/rollout.jsonl` + `rollout/index.sqlite`，删除旧 segment 和旧 Trace 投影 seed 依赖；保留的旧 Trace 文件只用于验证历史 API 不回退
- [x] 9.5 确认控制记录不进入 LangChain messages，业务状态不绕过 RolloutContextReader

## 10. 性能、崩溃和最终验证

- [x] 10.1 创建包含 128 Turn、assistant text、reasoning summary、encrypted reasoning、tool_call、tool_result 和 finalization 的确定性 fixture
- [x] 10.2 验证大型 assistant/tool 正文只在 JSONL 保存一次；SQLite 保存 offset、hash、投影、控制数据以及按 serializer 编码的非 messages checkpoint channel，但不保存完整 messages 数组
- [x] 10.3 验证重复 checkpoint 不复制完整 messages 数组，流式 assistant 不因 chunk 产生 O(n²) 存储
- [x] 10.4 验证 8011 → Gateway → workspace backend 的 bootstrap、4/16/64 历史加载和当前 Turn 详情读取延迟
- [x] 10.5 验证一次历史请求只创建一个持锁 read snapshot，使用逻辑 Turn keyset 窗口、`limit + 1` 边界查询和按 offset 的批量 JSONL 读取
- [x] 10.6 增加 JSONL 半行、fsync 窗口、SQLite 提交窗口、SQLite backup 恢复和 schema migration 崩溃测试
- [x] 10.7 运行 Python 静态分析、相关 pytest、rollout-backed 集成测试、Web build、浏览器回归和 OpenSpec strict validation
- [x] 10.8 固定 `asset/custom_tool_test_workspace` 的真实 128 Turn 与确定性 mock 会话边界；生成器、资产清单、导航索引和测试使用不相交的 session ID，测试只复制资产到 `out/tests`，不得写回资产目录
- [x] 10.9 在 `AppContainer` 中为当前工作区 sessions 根目录创建唯一 `RolloutCheckpointRuntime`；Runtime 组装的 `RolloutStorage`、`RolloutAppendWriter`、`RolloutContextReader`、历史 DTO reader 和 `RolloutCheckpointSaver` 共享同一对象引用；补充对象身份测试，并确认不同工作区不会共享该实例

## 11. Turn-only 用户锚点与 message-level compaction

- [x] 11.1 为 `context_view_turns` 增加 `(turn_id, view_id)` 反向索引，并实现单一 `resolve_turn_anchor()`：从 active head 沿祖先 view 查找最近仍完整包含目标 Turn 的 view；不得按最大物理 message sequence 或全局 control sequence 猜测
- [x] 11.2 保留 compaction 的 `message_id`/`message_sequence` 精确 cutoff，允许 cutoff 落在 Turn 中间；Turn 派生表只登记完整 Turn，Agent full reader 继续直接展开 context ranges
- [x] 11.3 让 fork、rewind、replay 的用户入口只接收 `turn_id`，统一使用 resolver 得到 source view/checkpoint/channel state 和 inclusive/before message boundary；跨 rollout 时复制消息到子 rollout，不复用父 JSONL offset
- [x] 11.4 补充多轮 rewind + 多轮 message-level compaction 后从早期 Turn fork/rewind/replay 的集成测试，覆盖目标 Turn 在当前 view 中完整、部分保留、summary-only 和完全不可达四种结果

## 12. RolloutCheckpointSaver 唯一业务入口

- [x] 12.1 将 `RolloutCheckpointSaver` 扩展为唯一 rollout/checkpoint 业务数据端口，内部封装历史 projection/detail/full、Turn 状态、anchor、fork、rewind、replay 和 compaction 操作
- [x] 12.2 移除生产业务服务对 `RolloutStorage`、`RolloutAppendWriter` 和 `RolloutContextReader` 的依赖；`AppContainer` 只向业务服务注入 `RolloutCheckpointSaver`
- [x] 12.3 将 `RolloutHistoryReader` 改为 Saver 内部实现或纯 DTO 适配器，禁止其直接持有 storage；将 `SessionTurnHistoryService` 的终态修复改为调用 Saver
- [x] 12.4 将三种 fork 收敛为 Saver 的单一业务 fork API，删除业务层对 `aput`、`copy_turn_finalizations` 和 `cancel_unfinished_turns` 的组合调用
- [x] 12.5 为唯一入口补充依赖边界测试：业务模块不得导入低层 rollout 类，Saver 内部组件共享同一 storage，历史/fork/status 操作均经过 Saver
- [x] 12.6 清理 OpenSpec 中“RolloutContextReader 作为业务唯一入口”和“RolloutHistoryReader 注入共享 storage”的旧表述，并运行 strict validation、静态分析和相关集成测试
- [x] 12.7 将 Saver 内部 fork 的目标 JSONL、checkpoint、Turn finalization、运行态终止和 provenance 收敛到同一个可恢复的 fork materialization 提交协议，消除默认 fork 中间状态
- [x] 12.8 前端省略 `turn_id` 的默认 fork 按 active view lineage 选择最近已完成 Turn；显式指向运行中 Turn 时拒绝 fork，并补充默认锚点与非法状态集成测试

## 13. Canonical carrier 与历史/live 统一响应模型

- [x] 13.1 校正 `reasoning_blocks` 的 SQLite 投影坐标：以 `content_block_index + item_index` 表示 AIMessage.content 和 reasoning_items 顺序，禁止把 tool_calls 编入 reasoning block 索引
- [x] 13.2 为历史 summary、历史 detail 和 live 流式事件定义统一的 `TurnResponsePart` DTO；保留 `reasoning_content`、`reasoning_items`、`thinking`、`redacted_thinking`、`text` 原始 carrier 语义
- [x] 13.3 实现 history adapter：按 SQLite source 坐标生成 summary/detail response parts，最终响应使用 finalization 指针，旧数据 heuristic 只能作为显式 fallback
- [x] 13.4 实现 live adapter：将 text/reasoning delta 和 tool lifecycle 合并为同一 response part 模型；不追加全量消息重复事件，工具调用沿 content 后顺序开始新的 part
- [x] 13.5 前端改为单一 response part renderer：历史 summary/detail 与 live 共用排序、part key、工具卡片和 reasoning 展示；详情展开只通过 Saver API 替换 projection
- [x] 13.6 为多 carrier assistant、多 tool_calls、ToolMessage 配对、最终响应指针、跨 provider reasoning 过滤、history/live 同构渲染补充集成和 Web 测试
- [x] 13.7 运行 Python 静态分析、相关 pytest、Web build、8011 浏览器回归和 OpenSpec strict validation；两个独立审查任务均无 P2 或更高问题后再收尾
