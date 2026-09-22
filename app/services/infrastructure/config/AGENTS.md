# 目录用途

`app/services/infrastructure/config/` 存放配置基础设施的聚焦组件，包括不可变快照、文件变化监听、状态模型、Workspace pending candidate 的待重启契约链路、配置事件消费者游标族、LLM provider/模型解析、Workspace 会话默认值持久化和配置重载状态聚合。

# 可修改内容

- 配置快照、fingerprint 与来源元数据。
- 配置文件目录监听和变化筛选。
- Workspace pending candidate 的启动契约、重启失败记录、重试、健康证明与提升/丢弃。
- 配置事件的游标重放、消费者独立认领、投递确认与游标保留窗口校验。
- 从生效配置解析 LLM provider（含 api_key 环境变量展开）与默认模型。
- 读写 `${workspace_root}/.boxteam/settings/session_defaults.json` 的会话默认 Agent/provider。
- 把状态库的 active/pending 记录叠加到快照重载状态之上。

# 不可修改内容

- 不在本目录实现 Agent、MCP 或 Gateway 的业务编排。
- 不直接修改运行中的应用组件。
- 不在本目录反向持有 `ConfigService` 或通过 `self._service.xxx` 回调宿主。
- 读取 pending/active 配置状态必须直接走状态库，不得经由 `PendingRestartCoordinator`，避免 `ReloadStatus` 与 `Pending` 协作者形成环形回调。

# 规范

- 监听器只报告变化，候选配置的校验与提交由调用方负责。
- 配置加载失败必须向上抛出，不能吞掉异常或伪造成功状态。
- 待重启协作者的状态库和配置域必须通过构造参数显式注入，回读状态只允许通过注入的 reload status provider。
- 事件游标协作者只做逐字转发，after/limit/consumer_id 语义与越界异常必须由状态库承担，不得在本目录改写分页或吞掉 CursorGone。
- LLM 解析协作者只接收无参、返回不可变值的叶子提供者（生效配置与默认 Agent id），不得注入整个 `ConfigService` 或快照存储。
- 会话默认值协作者复用宿主传入的 `workspace_root`，不得另造 `.boxteam` 根路径拼接，也不得自行向上推导工作区根目录。
- 会话默认值文件必须校验可解析、是对象且 schema 版本匹配；损坏或过期文件直接报错，不得静默回落默认值。
- 重载状态协作者只接收快照状态的叶子提供者（无参、返回不可变 `ConfigReloadStatus`），不得注入 `ConfigSnapshotStore`，以免把候选快照构建反向拖入本目录。
- 重载状态聚合不得抹掉快照基底的 `healthy`/`reason`/`last_error`，叠加 active/pending 只能补充字段。
