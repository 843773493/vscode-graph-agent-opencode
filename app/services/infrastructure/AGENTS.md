# 目录用途

`app/services/infrastructure/` 放服务层中的基础设施型实现，例如配置读取、事件查询与推送、日志、工具、工作区相关能力。

## 模块职责树

下面先列实际子目录，再按职责索引仍直接放在本目录的模块。根级模块目前有 30 个；职责分组只是查找索引，不代表这些文件已迁入子目录。

```text
infrastructure/
├── attachment_blob_catalog/   附件正文 blob、locator、fork 引用与回收
├── config/                    配置解析、事件游标和重载状态组件
├── config_service/            工作区 ConfigService 实现
├── events/                    有界事件 channel 与 typed 事件合同
├── mcp/                       MCP 连接、工具目录与 Agent 工具适配
├── node_debug/                Node 调试运行时；按 breakpoint/config/process/session 分区
├── resource_platform/         资源观察与已发布快照
├── rollout_context/           rollout、checkpoint、fork、seal 的 I/O owner
├── rollout_history/           rollout 历史只读快照和分页
├── team/                      Session 团队协作存储访问
├── turn_history/              按 Job Turn 组织的派生读取模型与恢复
├── workspace_activity/        工作区活动状态持久化
├── workspace_config_events/   工作区配置事件游标与状态
├── workspace_config_source/   工作区配置来源与快照
├── workspace_files/           工作区文件树观察、operation 与恢复
├── workspace_state_store/     工作区 SQLite 状态库与迁移
└── 根级模块（仍实际放在本层）
    ├── 外部服务客户端与资源租约
    │   ├── browser_manager_client.py
    │   ├── terminal_manager_client.py
    │   └── external_resource_leases.py
    ├── Gateway 通信客户端
    │   ├── gateway_client_common.py
    │   ├── gateway_communication_client.py
    │   └── gateway_session_context_client.py
    ├── 会话、队列与后台工作状态
    │   ├── message_stream_store.py
    │   ├── pending_queue_migration.py
    │   ├── pending_request_store.py
    │   ├── session_changes_store.py
    │   ├── session_goal_store.py
    │   ├── session_message_idempotency_store.py
    │   ├── background_work_handlers.py
    │   └── background_work_store.py
    ├── 工具运行时与选择
    │   ├── tool_catalog_service.py
    │   ├── tool_output_store.py
    │   ├── tool_selection_store.py
    │   └── tool_service.py
    ├── 日志与 trace
    │   ├── llm_request_log_service.py
    │   ├── log_service.py
    │   ├── trace_event_recorder.py
    │   └── trace_event_store.py
    ├── 工作区文件与服务装配
    │   ├── file_tree_settings_service.py
    │   ├── workspace_file_watch_service.py
    │   └── workspace_service.py
    └── 其他基础设施
        ├── artifact_service.py
        ├── attachment_content_service.py
        ├── rollout_checkpoint_runtime.py
        ├── rollout_history_reader.py
        └── runtime_service.py
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
- 失败时直接抛错，避免“看起来成功”的假默认值。
- 不要引入与 `app/core/` 职责重复的内容。
- 会话专属持久化必须先通过统一会话路径解析器取得物理会话节点，再使用该节点下的对应子目录；禁止拼接或假设 `${workspace_abs_path}/.boxteam/sessions/{session_id}/` 固定路径。
- 新增会话存储时必须同时考虑会话移动、删除、导出和迁移，确保操作解析后的整个会话目录即可完整处理数据。
- 派生索引不得反向覆盖物理目录树；发现重复稳定 ID、损坏 manifest 或越界路径时必须直接报告冲突。
- `workspace_state_sql/` 是 WorkspaceStateStore 的静态迁移唯一来源；Python 保留版本顺序和条件数据转换。

- `message_stream_store.py` 是消息流持久读取的唯一 owner；公开错误只带稳定身份、artifact 和错误事实，物理路径及损坏原文留在诊断日志；全部恢复依赖校验成功后才能发布状态及幂等缓存。
- 消息流 applied-event-id 的事件归属、信封和索引键复用事件日志的唯一校验；恢复、裁剪及追加沿同一读取入口，追加前拒绝损坏的幂等事实，不在日志追加后才发现已存在的非法归属。
- 消息流启动恢复与普通冷读取必须共用完整恢复入口，不直接信任高水位快照或另行发布部分缓存；终态校验后立即卸载缓存，空日志保持无事件的原有语义。
- 消息流ID定位由原store统一验证：请求、索引、生成端及三个文件模板共用单一文件名约束；不得把路径或URI解释为ID，也不得将显式空值当成未提供而回退，保留既有合法不透明ID格式。
