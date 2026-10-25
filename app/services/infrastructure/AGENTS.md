# 目录用途

`app/services/infrastructure/` 放服务层中的基础设施型实现，例如配置读取、事件查询与推送、日志、工具、工作区相关能力。

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
