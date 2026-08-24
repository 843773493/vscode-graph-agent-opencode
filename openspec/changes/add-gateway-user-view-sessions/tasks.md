## 1. 数据边界与 SQLite 基础

- [x] 1.1 定义 Gateway 控制面 SQLite 的 schema 版本、迁移表和连接初始化策略，覆盖 `gateway_config`、工作区注册、用户、租约、用户视图和游客追踪。
- [x] 1.2 定义 Workspace SQLite 的 schema 版本、迁移表、共享配置表和工作区活动事件表。
- [x] 1.3 为 Gateway 和 Workspace SQLite 配置事务、WAL、busy timeout、外键校验和单进程所有权检查。
- [x] 1.4 增加数据库诊断信息，明确数据库路径、schema 版本、迁移状态和失败原因。
- [x] 1.5 验证 Gateway SQLite、Workspace SQLite、session rollout `index.sqlite` 和 `session-catalog-index.json` 的职责边界不会交叉。

## 2. Gateway 共享配置和控制状态迁移

- [x] 2.1 将 Gateway workspace registry 的 JSON 持久化迁移到 Gateway SQLite，保留远程 Gateway、连接凭据和本地工作区运行时的现有路由语义。
- [x] 2.2 将 Gateway 共享 mutable config 迁移到 SQLite 覆盖文档，保留发行默认 JSONC、schema、配置版本和最终结构校验。
- [x] 2.3 将 Workspace 共享 mutable config 迁移到 Workspace SQLite 覆盖文档，并保持 Gateway 不直接读写工作区 `.boxteam/`。
- [x] 2.4 将现有 `web_ui_settings.json` 拆分为用户 profile 配置和 Gateway SQLite 运行时视图状态。
- [x] 2.5 为旧 JSON 状态实现显式、可重复、带备份和原子提交的迁移入口；迁移失败时保留原始文件并拒绝部分启动。
- [x] 2.6 为配置迁移补充仅新状态、旧状态、损坏状态、重复执行和迁移中断后的恢复测试。

## 3. Gateway 用户与访问租约

- [x] 3.1 定义普通用户、游客、用户访问会话、访问租约和接管错误的后端模型与 API schema。
- [x] 3.2 实现普通用户创建、列表、选择和删除 API，确保 `user_id` 稳定唯一且显示名称不参与物理路径。
- [x] 3.3 实现无密码访问会话 Cookie，并将用户会话校验接入 Gateway 用户 API、控制面 API 和 Workspace 透明代理入口。
- [x] 3.4 实现访问租约申请、心跳、主动释放、过期清理和占用摘要查询。
- [x] 3.5 实现接管事务：递增租约代数、撤销旧访问会话、创建新访问会话并返回接管结果。
- [x] 3.6 让 Workspace Proxy 的普通请求和流式响应同时响应用户租约失效与工作区路由失效。
- [x] 3.7 确认用户接管只影响浏览器访问，不停止或修改 Workspace Backend 中运行的 Agent Job。
- [x] 3.8 增加 Gateway 用户并发选择、心跳竞争、接管、旧会话失效和异常退出租约回收测试。

## 4. 游客身份与追踪清理

- [x] 4.1 实现游客访问入口和临时游客 ID，确保游客不创建普通用户记录或用户 profile 目录。
- [x] 4.2 将游客配置限制为追踪字段，禁止游客视图写入普通用户持久化状态。
- [x] 4.3 实现游客追踪记录的 7 天 TTL 清理，并在 Gateway 启动和周期任务中执行。
- [x] 4.4 增加游客刷新、重启、过期清理和普通用户状态隔离测试。

## 5. 用户 profile 与 `.gitignore`

- [x] 5.1 实现普通用户目录初始化，路径固定为 Gateway 用户状态目录下的稳定 `user_id`。
- [x] 5.2 生成应用管理的 `.gitignore`，排除 SQLite、运行时、凭据、Gateway 连接、工作区、缓存和本地覆盖文件。
- [x] 5.3 定义用户 profile JSONC 和主题文件的版本、schema 与可写范围。
- [x] 5.4 将主题、布局和个人 UI 偏好从全局 UI 设置拆分到当前用户 profile。
- [x] 5.5 确认本次不生成 Git remote、分支、同步状态，不执行 Git 命令，并增加用户目录边界测试。

## 6. 用户视图和历史位置恢复

- [x] 6.1 实现用户视图状态 API 与 SQLite 持久化，覆盖当前工作区、当前会话、Turn 锚点、偏移、跟随尾部和投影版本。
- [x] 6.2 将 Web 启动流程改为先恢复普通用户或游客访问上下文，再加载对应的用户 profile 和共享功能配置。
- [x] 6.3 实现用户选择页、创建/删除入口、占用展示、接管按钮和接管后回到用户入口的状态处理。
- [x] 6.4 为虚拟列表实现按 Turn 锚点恢复视口，避免只保存原始 `scrollTop`。
- [x] 6.5 在会话切换、滚动、历史分页、工具详情展开和新消息到达时保存正确的用户视图状态。
- [x] 6.6 实现投影变化或锚点失效时的最近位置/尾部回退，并向用户显示恢复结果或错误。
- [x] 6.7 增加不同用户主题、布局、当前会话、历史游标和滚动位置互不共享的前端测试。
- [x] 6.8 增加同一用户在同一 Gateway 的两台浏览器顺序切换后恢复视图位置的集成测试。

## 7. Workspace 会话活动事件

- [x] 7.1 实现 Workspace SQLite 的会话活动事件模型、单调递增事件游标和保留策略。
- [x] 7.2 在 Agent 完成、失败和取消等会话状态变化后写入轻量摘要事件，事件不得包含完整消息、工具调用或工具结果。
- [x] 7.3 实现工作区级活动事件列表接口和 SSE 接口，支持 `after` 游标补取与游标失效错误。
- [x] 7.4 通过现有 Gateway Workspace Proxy 验证工作区活动 SSE 的本地目标、远程 Gateway 目标和路由失效处理。
- [x] 7.5 让前端订阅工作区级活动流，非当前会话事件只更新会话摘要、完成状态和未读提示。
- [x] 7.6 实现活动流断线重连、游标补取、游标失效后的会话摘要刷新和重复事件去重。
- [x] 7.7 增加未打开会话任务完成、任务失败、断线补取、游标失效和摘要事件不泄漏完整内容的集成测试。

## 8. 前端游客与自动化入口

- [x] 8.1 将 Playwright 测试夹具默认改为游客入口，禁止依赖普通用户 localStorage 或固定用户 profile。
- [x] 8.2 增加普通用户选择、接管、游客登录、游客过期和接管后旧页面退出的浏览器测试。
- [x] 8.3 增加两浏览器访问同一 Gateway 的占用状态显示和接管交互测试。
- [x] 8.4 增加未打开会话完成提示、点击会话后按用户锚点恢复历史的浏览器集成测试。

## 9. 验证与交付

- [x] 9.1 更新受影响的 Gateway、Workspace Backend、Web API schema 和配置诊断测试。
- [x] 9.2 运行 SQLite 迁移、并发租约、用户隔离、活动事件和 Workspace Proxy 的 focused pytest 测试。
- [x] 9.3 运行 Web 用户入口、视图恢复和游客自动化的 focused Bun/Playwright 测试。
- [x] 9.4 修改 `src/clients/web` 后运行 `bun run --cwd src/clients/web build`，并修复新增的静态分析或构建错误。
- [x] 9.5 使用本地隔离工作区验证两台浏览器通过同一 Gateway 的完整链路，正式测试产物写入 `out/tests/integration/clients/web/test_gateway_user_view_sessions/`，不修改 `asset/` 或项目根目录。
- [x] 9.6 运行 `openspec validate add-gateway-user-view-sessions --strict`，确认 proposal、specs、design 和 tasks 一致。
