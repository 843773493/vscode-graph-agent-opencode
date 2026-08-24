## Context

当前 Web 请求先到 Gateway，再由 Gateway 选择并透明代理到目标 Workspace Backend；Gateway 不直接读写工作区 `.boxteam/` 业务数据。现有 Gateway 控制面状态和 Web UI 设置分别使用 JSON 文件，Workspace 的共享配置仍按现有配置加载边界读取 JSONC；每个会话 rollout 下已经有独立的 `index.sqlite`，它只负责该会话的 checkpoint/record 索引。

本变更需要增加 Gateway 层的用户视图身份，但不能把用户身份变成工作区业务权限，也不能把 Gateway 用户数据库塞进会话 rollout。两台电脑必须访问同一个 Gateway 实例，才能共享该 Gateway 的用户租约和用户视图；不同 Gateway 之间的 Git 配置同步不属于本变更。

## Goals / Non-Goals

**Goals:**

- 在 Gateway 控制面提供无密码普通用户、游客、单访问租约和接管。
- 将用户视图与工作区共享功能配置分离；同一用户在同一 Gateway 的不同电脑之间能够恢复自己的视图。
- 将 Gateway/Workspace 的共享可变状态迁移到各自边界内的 SQLite，并保留默认 JSONC、schema、目录索引和 rollout 索引的职责。
- 为 Workspace Backend 增加工作区级、可追赶的会话活动通知，并由 Gateway 透明代理给 Web。
- 创建普通用户 profile 目录和应用管理的 `.gitignore`，但不实现 Git 远程操作。

**Non-Goals:**

- 不提供密码、OAuth、细粒度权限、多租户隔离或安全认证体系；无密码用户 ID 只是本地 Gateway 的视图身份。
- 不让 Gateway 直接访问工作区 `.boxteam/`，不把用户 ID 作为浏览器可伪造的 Workspace 权限头转发。
- 不改变 rollout JSONL/segment 物理格式，不合并各会话的 `index.sqlite`。
- 不实现 Git remote、pull、push、分支、冲突解决或跨 Gateway 用户身份同步。
- 不停止、迁移或重启因用户接管而正在运行的 Agent 任务。

## Decisions

### 1. 用户身份由 Gateway 持有，Workspace 业务身份保持不变

Gateway 增加用户访问会话 Cookie；浏览器继续使用现有 Gateway 本地访问凭据访问 Gateway API。普通用户选择、游客创建、心跳、接管和用户视图 API 都在 Gateway 控制面完成。

Workspace `/api/v1` 继续由 Gateway 透明代理，工作区后端不依赖浏览器提交的 `user_id` 判断权限。这样可以保持当前本地 Gateway、远程 Gateway 联邦和 Workspace Backend 的边界：外层 Gateway 的用户视图不会污染远程 Gateway 或工作区业务数据。

接管通过递增访问租约代数并使旧访问会话失效实现。Gateway 代理的 SSE 流同时监听工作区路由失效和用户租约失效；旧客户端的普通请求、心跳或后续连接得到 `user_session_taken_over`，前端清除当前视图并回到用户入口。运行中的 Job 不和浏览器访问租约绑定。

备选方案是只在前端使用用户 ID，不让 Gateway 强制校验；这种方案不能真正实现“单个 ID 最多一个访问”，也无法可靠关闭被接管客户端，因此不采用。

### 2. 一个 Gateway 使用一个控制面 SQLite

Gateway 使用 `${BOXTEAM_HOME}/state/gateway/gateway.sqlite` 作为控制面权威数据库，建议的逻辑表包括：

```text
gateway_config
gateway_workspace_registry
user_account
user_access_lease
user_view_state
guest_tracking
schema_migrations
```

`user_access_lease` 对普通 `user_id` 建立唯一有效租约；申请、心跳、释放和接管在事务中完成。SQLite 开启 WAL 和有限 busy timeout，所有写入通过 Gateway 进程完成；不支持两个 Gateway 进程直接通过 NFS/SMB 共同打开同一数据库。

`user_view_state` 以 `user_id + workspace_id + session_id` 为键，保存 Turn 锚点、偏移、`follow_latest` 和投影版本。由于普通用户同一时间只有一个访问租约，跨电脑切换时可以恢复同一用户的单一视图；接管不是两个客户端并发合并，而是显式地终止旧视图会话。

游客只写 `guest_tracking`，不创建 `user_account` 或 profile 目录。游客记录有 `expires_at`，Gateway 启动和周期清理都执行 7 天 TTL 删除。

### 3. Workspace Backend 使用独立的 Workspace SQLite

每个工作区在 `${workspace}/.boxteam/state/workspace.sqlite` 保存工作区共享可变配置和工作区级活动事件。Gateway 只通过已有 `/api/v1/{path}` 透明代理访问它。

建议的逻辑表包括：

```text
workspace_config
workspace_activity
workspace_event_cursors 或等价的事件保留元数据
schema_migrations
```

工作区目录拓扑的权威文件 `navigation/session-catalog-index.json` 继续由目录索引维护；`workspace.sqlite` 不取代它。单会话 `sessions/{session_id}/rollout/index.sqlite` 继续只负责 rollout checkpoint 索引，避免工作区配置、活动通知和高频 checkpoint 写入互相污染。

### 4. 配置持久化只迁移共享可变状态

发行包默认值和 schema 仍保留 `gateway_inline.jsonc`、`workspace_inline.jsonc` 及对应 schema。运行时加载器把默认 JSONC 与 SQLite 中的共享覆盖合并后校验最终结构；Gateway 和 Workspace 配置域分别维护自己的 `config_version`。

现有状态的迁移边界如下：

```text
Gateway registry JSON       → gateway.sqlite.gateway_workspace_registry
Gateway mutable overrides   → gateway.sqlite.gateway_config
web_ui_settings.json        → 用户 profile 和 gateway.sqlite 的运行时视图状态
Workspace mutable overrides → workspace.sqlite.workspace_config
```

旧文件迁移使用显式、可重复的迁移步骤、备份和原子提交；迁移成功后不进行双写。迁移失败保留原文件并拒绝使用部分结果。`session-catalog-index.json`、rollout manifest、JSONL 和单会话 rollout `index.sqlite` 不参与此配置迁移。

共享功能配置和用户 profile 分开：Gateway 的 history loading、工作区注册、运行参数等属于共享配置；主题、布局和个人偏好属于用户 profile。现有共享 `custom_themes` 在迁移时必须转入指定普通用户 profile 或明确作为内置主题处理，不能继续作为所有用户共享的个人主题。

### 5. 用户 profile 是未来 Git 边界，但本次只生成 `.gitignore`

普通用户目录位于：

```text
${BOXTEAM_HOME}/state/gateway/users/{user_id}/
```

目录名严格使用稳定 `user_id`，显示名称不参与路径。目录中保留 `profile.jsonc` 和 `themes/` 等未来可以同步的个人配置，并在创建时写入 `.gitignore`。

Gateway SQLite、凭据、Gateway 连接、Workspace 路径、会话数据、游客记录和运行时缓存放在用户目录之外；`.gitignore` 仍作为防止未来扩展误收集的第二道边界。本次不存储 Git 远程 URL，不创建 Git 仓库，也不执行任何 Git 命令。

用户 profile JSONC 使用独立的 profile 配置版本和 schema，不能复用 Gateway 共享功能配置的 schema；当前 UI 设置从全局文件拆分到当前用户 profile 时，机器路径和运行时数据必须留在 Gateway SQLite 或机器本地状态中。

### 6. 工作区活动事件采用持久游标 + SSE

当前会话级 trace SSE 继续用于当前会话 Turn 细节和实时输出；新增工作区级摘要活动接口，例如：

```text
GET /api/v1/session-catalog/events?after=<event_seq>
GET /api/v1/session-catalog/events/stream?after=<event_seq>
```

工作区后端在 Agent 完成、失败、取消等影响会话列表的状态变化时写入轻量活动事件，事件内容只包含 `event_seq`、`event_id`、`session_id`、状态、摘要和时间。提交成功后再发布到内存订阅者；客户端重连使用最后游标补取，游标过期则完整刷新会话摘要。

Gateway 不解析事件业务，只沿用现有 Workspace Proxy 的流式响应转发和路由租约处理。Web 前端收到非当前会话事件时只更新会话列表和提示，不请求完整历史；用户打开会话后再使用用户视图锚点和 rollout 历史接口加载内容。

备选方案是继续依赖当前会话 SSE 加低频列表刷新。该方案无法保证没有打开会话时的及时提示，也无法在断线后按工作区统一补偿，因此只保留为事件流故障时的显式恢复机制，不作为主要同步机制。

## Risks / Trade-offs

- **[无密码身份不是安全认证]** → 在界面和配置诊断中明确它只是单 Gateway 的用户视图机制；继续要求现有本地/联邦 Gateway 凭据，暂不把 Gateway 暴露为公共认证服务。
- **[接管可能导致旧客户端未及时发现失效]** → Gateway 统一校验访问会话，SSE 监听租约失效，心跳和普通请求返回明确的接管错误；前端不静默继续使用旧状态。
- **[SQLite 被多个 Gateway 进程或网络文件系统共同打开]** → 将每个 Gateway 控制数据库限定为单一 Gateway 进程拥有，启动时检测并拒绝不安全的共享运行方式；跨电脑通过同一 Gateway 访问，不通过复制 SQLite 同步。
- **[用户 profile 与共享配置迁移边界错误]** → 使用显式白名单和独立 schema；旧共享主题迁移时要求明确归属，不把工作区连接、凭据或路径导出到 profile。
- **[活动事件保留不足导致游标失效]** → 保留明确的事件窗口；游标失效时触发会话摘要全量刷新，并报告恢复状态，不静默跳过通知。
- **[workspace.sqlite 与 rollout 写入无法跨文件完全原子]** → 活动事件只承诺提交后的摘要通知；历史正文仍以 rollout 恢复为准，启动时通过会话状态和 rollout 索引执行对账，避免把通知丢失误判为历史丢失。
- **[任务完成事件过多造成列表更新压力]** → 活动事件只发送轻量摘要，前端批量合并同一会话更新，完整 Turn 详情仍按当前历史加载策略按需读取。

## Migration Plan

1. 创建 Gateway 和 Workspace SQLite schema，并实现版本表、事务访问和诊断信息。
2. 迁移 Gateway registry、共享 mutable config 和现有 Web UI 状态；迁移前创建备份，成功后停止旧 JSON 的运行时写入。
3. 为普通用户初始化用户目录和 `.gitignore`，将主题/布局等 profile 状态与运行时视图状态分开。
4. 增加 Gateway 用户访问、游客和接管 API，再接入 Web 启动流程。
5. 增加 Workspace 活动事件持久化、事件读取/SSE 和 Gateway 透明代理验证。
6. 接入 Web 视图恢复、未打开会话提示、断线追赶和接管后的退出处理。
7. 迁移失败时保留源 JSON 和原有 rollout；在迁移完成前不删除任何会话数据。测试确认迁移后的 Gateway/Workspace 仍能读取原有 rollout，但本变更不提供旧 checkpoint 格式兼容层。

## Open Questions

无。Git 远程同步操作已明确延期到独立 OpenSpec。
