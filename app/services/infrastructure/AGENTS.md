# 目录用途

`app/services/infrastructure/` 放服务层中的基础设施型实现，例如配置读取、事件查询与推送、日志、工具、工作区相关能力。

## 模块职责树

下面先列实际子目录，再按职责索引仍直接放在本目录的模块。根级模块目前有 20 个；职责分组只是查找索引，不代表这些文件已迁入子目录。

```text
infrastructure/
├── attachment_blob_catalog/   附件正文 blob、locator、fork 引用与回收
├── config/                    配置解析、事件游标和重载状态组件
├── config_service/            工作区 ConfigService 实现
├── external_resource_leases/   资源身份、operation lease 与增量事务
├── events/                    有界事件 channel 与 typed 事件合同
├── gateway_search/            复用原 Gateway 连接的发现与联邦搜索传输
├── mcp/                       MCP 连接、工具目录与 Agent 工具适配
├── persistent_tasks/          持久任务定义版本、运行账本与轻量事件
├── node_debug/                Node 调试运行时；按 breakpoint/config/process/session 分区
├── resource_platform/         资源观察与已发布快照
├── rollout_context/           rollout、checkpoint、fork、seal 的 I/O owner
├── rollout_history/           RolloutHistoryReader 入口、只读快照、分页和搜索
├── team/                      Session 团队协作存储访问
├── session_trace/             Session Trace 存储、查询、流与恢复
├── workspace_activity/        工作区活动状态持久化
├── workspace_config_events/   工作区配置事件游标与状态
├── workspace_config_source/   工作区配置来源与快照
├── workspace_files/           工作区文件树观察、operation 与恢复
├── workspace_state_store/     工作区 SQLite 状态库与迁移
└── 根级模块（仍实际放在本层）
    ├── 外部服务客户端与资源租约
    │   ├── browser_manager_client.py
    │   └── terminal_manager_client.py
    ├── Gateway 通信客户端
    │   ├── gateway_client_common.py
    │   ├── gateway_communication_client.py
    │   └── gateway_session_context_client.py
    ├── 会话、队列与后台工作状态
    │   ├── message_stream_store.py
    │   ├── pending_queue_migration.py
    │   ├── pending_request_store.py
    │   ├── session_changes_store.py
    │   └── background_work_store.py
    ├── 工具结果与选择持久化
    │   ├── tool_output_store.py
    │   └── tool_selection_store.py
    ├── 模型请求日志
    │   └── llm_request_log_reader.py
    ├── 前端 HTML 快照
    │   └── html_snapshot_store.py
    ├── 工作区文件与服务装配
    │   ├── file_tree_settings_service.py
    │   ├── workspace_file_watch_service.py
    │   └── workspace_service.py
    └── 其他基础设施
        ├── artifact_service.py
        ├── attachment_content_service.py
        └── rollout_checkpoint_runtime.py
```

新增实现优先归入职责匹配的现有子目录；跨域根级模块只在确实服务多个子域时保留。迁移既有文件时，全量平移导入方与对应测试，并同步修订这棵树；不要只移动文件留下旧路径或重复入口。

## 可修改内容

- 可以新增、修改、删除基础设施服务实现文件。
- 可以补充与外部数据、系统状态或通用能力交互的实现。
- `team/`访问既有Session control协作存储，不保存JSON board或承担业务语义派生、fanout编排。
- `workspace_files/`承载WorkspaceService装配的文件树观察、operation存储及worker；真实文件事实归文件系统，不借Session catalog或新增第二文件writer。

## 不可修改内容

- 不要把业务规则和流程编排塞到这里。
- 不要把纯模型转换逻辑放到这里。
- 不要为会话数据重新引入顶层 `checkpoints/`、`background_tasks/`、`conversation_history/`、`logs/llm_requests/` 或 `logs/traces/` 等分散目录。

## 规范

- 保持实现可替换，优先通过协议向上暴露能力。
- `llm_request_log_reader.py` 只负责 Session 请求日志摘要、正文读取、有界搜索和精确定位；`list_session_log_summaries` 不读正文。日志写入和裁剪由 `app/agents/llm_logging_middleware.py` 负责，不在 Reader 中增加第二写入方。
- 失败时直接抛错，避免“看起来成功”的假默认值。
- HTML快照按metadata创建时刻保留30天，过期读取返回不存在；工作区每小时任务在worker中删除metadata和HTML，并回收超过30天的未发布HTML。只通过catalog逐会话定位并持有原生命周期gate，不递归发现未知会话；回收不读取HTML正文、不在普通读取中扫描目录。
- Context detail复用每小时任务，经RolloutCheckpointRuntime持原Session生命周期gate逐会话、逐已发布Thread和namespace调用原GC owner；worker只删除已过期且无封存引用的正文，不裁剪恢复依赖、不新建空rollout、不在请求路径回收。
- 不要引入与 `app/core/` 职责重复的内容。
- 会话专属持久化必须先通过统一会话路径解析器取得物理会话节点，再使用该节点下的对应子目录；禁止拼接或假设 `${workspace_abs_path}/.boxteam/sessions/{session_id}/` 固定路径。
- 新增会话存储时必须同时考虑会话移动、删除、导出和迁移，确保操作解析后的整个会话目录即可完整处理数据。
- 派生索引不得反向覆盖物理目录树；发现重复稳定 ID、损坏 manifest 或越界路径时必须直接报告冲突。
- `pending_request_store.py` 的全工作区恢复扫描逐个取得短 topology 准入和 Session read guard，重新核对 catalog active 状态后读取 main/child 队列；guard 覆盖同步读取线程及取消结算，不并持两个 Session 锁。删除排空中的精确 Session 读取沿用调用方已有的准入，不在 store 内重入该锁；原存储故障必须继续抛出。
- `workspace_state_store/migrations/` 是 WorkspaceStateStore 的静态迁移唯一来源；Python 保留版本顺序和条件数据转换。

- `message_stream_store.py` 是消息流持久读取的唯一 owner；公开错误只带稳定身份、artifact 和错误事实，物理路径及损坏原文留在诊断日志；全部恢复依赖校验成功后才能发布状态及幂等缓存。
- 消息流显式event_id的完整重试事件随主日志一次提交，仅在裁剪时先归档被移出的幂等事实，再替换事件日志；自动ID不归档。普通追加复用已经完成恢复校验的缓存，不逐次重读或重写历史幂等文件；冷加载、恢复及裁剪校验归属、信封和索引键，沿事件日志的唯一校验规则，全部成功后发布缓存。
- 消息流已加载后的分页与SSE续播复用已校验的保留窗口序号索引，正文与幂等缓存共享，只复制本页展示事件；内部水位、归属和终态检查只读缓存，不为每页复制整份状态，对外快照仍独立复制；SSE按小批次补发。索引随冷加载、追加、裁剪和淘汰统一维护，不持久化第二套事件。进程加载后的外部篡改在下次冷加载或恢复中报告。
- 消息流终态缓存的字节预算同时核算快照与幂等/重放共用的事件正文，不只计算快照；新终态版本在worker中核算共享JSON对象，不为计费重新序列化历史，普通缓存命中仅更新LRU。淘汰同时卸载状态、幂等和重放索引，磁盘历史及显式重试事实仍可冷加载。
- 消息流对外状态在线程中复制捕获的不可变版本，控制帧直接持有该独立副本，不再次深拷贝；事件幂等查询只读内部身份和所选事件。
- 消息流派生快照每条流只运行一个写入任务，写入期间合并为最新待写版本；不得为每个快照触发点排队并保留历史状态。逐事件 fsync、裁剪前快照及冷恢复校验仍沿原入口。
- 消息流启动恢复与普通冷读取必须共用完整恢复入口，不直接信任高水位快照或另行发布部分缓存；终态校验后立即卸载缓存，空日志保持无事件的原有语义。
- 消息流ID定位由原store统一验证：请求、索引、生成端及三个文件模板共用单一文件名约束；不得把路径或URI解释为ID，也不得将显式空值当成未提供而回退，保留既有合法不透明ID格式。
