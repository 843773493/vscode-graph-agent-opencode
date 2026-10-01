# 目录用途

`app/services/infrastructure/node_debug/session/` 承载 Node Debug 会话的基础设施实现：会话存储读写、会话调试状态、会话准入判定、调试动作分发与工具动作记录、启动/重启与关停/排空入口链路、只读状态读取、动作快照、线程所有权解析以及方案 fork 预发布边界。

# 可修改内容

- 可以维护会话 manifest/方案/claim 持久化、调试快照组装、线程归属解析和 fork 源捕获与目标校验。
- 可以维护 `NodeDebugService` 的方法族 mixin 垂直链路：`action_dispatch.py`（`apply_action` 调试动作分发）、`tool_actions.py`（`record_tool_action` 与 `_TOOL_ACTION_SOURCES` 工具动作时间线）、`launch_entry.py`（`start`/`restart` 启动入口与编排器构造）、`state_reads.py`（`get_state`/`get_variables` 只读入口）、`lifecycle_entry.py`（`close`/`drain_session` 关停入口）。这些 mixin 只声明方法，宿主字段与协作者由 `service.py` 装配。
- 可以维护本子包内模块之间的导入路径和实现细节。

# 不可修改内容

- 不得实现 API 路由、Agent 编排或进程生命周期控制。
- 入口 mixin 不得自建 owner 准入或 per-owner 临界区托管，必须复用宿主已有的 `_admit_mutation`、`_owner_lock`、`_runtimes_lock` 等能力；也不得反向依赖顶层 `service.py`。
- 不得保留旧顶层模块路径、re-export wrapper 或兼容导入别名。
- 不得拼接固定的会话/线程物理目录，必须通过统一会话路径解析器定位。
- 不得静默吞掉会话持久化或准入错误，也不得返回虚假的会话状态。

# 规范

- 文件按单一会话职责命名，只依赖会话领域模块、配置注册表与上层 schema。
- 需要跨子包能力时通过明确的绝对模块路径导入，不反向依赖 `service.py`。
- 失败必须直接抛出详细错误；修改本目录后运行 `uv run ruff check` 和对应 Node Debug 专项测试。
- 代码注释使用中文，公开行为变更必须同步更新调用方和测试，不得建立双轨实现。

