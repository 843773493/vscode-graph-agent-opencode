## Why

当前浏览器只有 Gateway 的本地访问凭据，没有应用层的用户视图身份；主题、布局、当前会话和历史阅读位置无法按用户隔离，也无法在同一 Gateway 的不同电脑之间恢复。与此同时，未打开会话的任务完成状态主要依赖当前会话事件流和低频会话列表刷新，缺少工作区级的可追赶活动通知；现有 Gateway 与 Workspace 的可变配置和 UI 状态也分散在 JSON 文件中，难以安全地处理并发访问。

## What Changes

- 增加无密码的 Gateway 用户选择、创建、删除和游客访问入口。
- 为普通用户维护单访问租约、心跳、占用展示和“接管”机制；接管后旧客户端明确退出用户视图，但不停止工作区后端中的 Agent 任务。
- 为每个用户隔离主题、布局、会话选择、历史阅读锚点和相关 Web 视图状态。
- 为游客生成不持久化的临时身份，只保存追踪所需信息并在 7 天后清理；Playwright 默认使用游客入口。
- 将 Gateway 和 Workspace 的共享可变功能配置、Gateway 控制状态和用户运行时状态迁移到职责边界清晰的 SQLite 存储。
- 保留 rollout 单会话 `index.sqlite`、工作区目录权威 `session-catalog-index.json`、发行默认 JSONC 和 schema 的既有职责。
- 增加工作区级会话活动事件与可追赶游标，使未打开会话的任务完成能够及时更新会话列表和提示。
- 为 Gateway 用户目录生成默认 `.gitignore`，仅为后续用户配置 Git 同步保留安全边界；本次不实现远程仓库、分支、拉取、推送或冲突处理。

## Capabilities

### New Capabilities

- `gateway-user-access`: 无密码普通用户、游客身份、单访问租约、占用状态、心跳和接管。
- `user-scoped-web-view`: 用户隔离的主题、布局、当前会话和会话历史阅读位置恢复。
- `sqlite-config-storage`: Gateway 与 Workspace 共享可变配置、控制状态和用户运行时状态的 SQLite 持久化及迁移边界。
- `workspace-session-activity`: 工作区级会话活动事件、持久游标、SSE 推送和断线追赶。
- `gateway-user-profile-directory`: 用户目录、用户 profile 文件边界和默认 `.gitignore`；不包含 Git 远程同步操作。

### Modified Capabilities

无。

## Impact

- Gateway 用户认证/会话、控制面路由、工作区透明代理和 Gateway SSE 生命周期。
- Gateway registry、现有 `web_ui_settings.json`、Gateway/Workspace 可变配置加载与迁移。
- Workspace 后端会话活动、任务完成摘要、SQLite 状态库和工作区事件 API。
- Web 前端启动登录态、游客入口、用户选择页、占用提示、接管后的退出处理、用户视图恢复和未打开会话通知。
- 集成测试、Playwright 测试夹具、SQLite schema/迁移测试和跨 Gateway 代理链路测试。
- 本次不改变 rollout JSONL/segment 物理格式，不实现 Git 远程同步，也不引入密码或多租户权限模型。
