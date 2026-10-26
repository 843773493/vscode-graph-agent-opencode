# 目录用途

`app/api/` 是工作区后端的 HTTP 接口适配层，承载 `/api/v1/*` 的全部路由。每个文件对应一个路由域，导出模块级 `router = APIRouter(prefix=..., tags=[...])`，由 `app/main.py` 统一以 `prefix="/api/v1"` 挂载。

本目录只做请求解析、参数校验、依赖注入与响应封装，不承载业务规则：

- `deps.py`：所有路由共享的 FastAPI 依赖提供者（从 `request.app.state.container` 取服务实例）与 `verify_local_token`；`get_request_id` 从 `app.core.trace_middleware` 重导出。
- canonical `session_id` / `thread_id` 的 `Annotated` 校验类型由 `app.schemas.canonical_params` 唯一定义，并复用 `app.core.session_catalog_store` 的唯一校验器。
- `sse_heartbeat.py`：把异步事件源包装成带空闲心跳的 SSE 流的统一实现。
- `runtime_logging.py`：Backend 与 Gateway 共用的进程级内部日志控制路由；不挂到 `workspace_api`，不依赖工作区容器，不按工作区复制共享 Backend 的日志设置。入口只接受无浏览器来源头的 loopback 请求，并核对 Gateway 私有凭据；Backend 单服务未显式配置控制凭据时拒绝管理，不接受固定开发 token。
- `workspace_file_response.py`：复用 Starlette 文件响应的 Range 与头部实现，协调延迟 I/O 错误、发头时点和临时下载清理；文件解析与归档准备仍归 Workspace 文件 owner。
- 其余 `*.py`：按业务域（`sessions`、`messages`、`workspace`、`jobs`、`tools`、`mcp`、`node_debug`、`runtime` 等）拆分的路由实现。同层不再有前端 TS 文件；浏览器前端的 gateway API 客户端位于 `src/clients/web/src/gatewayApi.ts`，pending request 客户端位于 `src/clients/web/src/pendingRequestsApi.ts`，二者不属于本目录。
- `messages.py` 与 `session_turns.py` 分别适配 `sessions/{session_id}/threads/{thread_id}/messages` 和同 thread 的 `/history`，复用消息准入和唯一历史 reader；Session-only 入口仍由业务 owner 解析 main thread。

## 按职责查找

下面是逻辑索引；所有文件仍直接位于 `app/api/`，分组表示请求入口的职责边界。

```text
app/api/
├── HTTP 公共边界
│   ├── deps.py                 服务依赖与请求身份
│   ├── errors.py               统一错误响应
│   └── sse_heartbeat.py        SSE 空闲心跳
├── 会话与执行
│   ├── sessions.py             会话创建、查询与生命周期
│   ├── session_navigation.py   会话目录导航
│   ├── session_activity.py     会话 catalog 事件流
│   ├── messages.py             Thread 消息
│   ├── message_stream.py       消息执行流
│   ├── session_turns.py        Thread 执行历史
│   ├── jobs.py                 后台执行任务
│   ├── agents.py               Agent 目录与选择
│   ├── context.py              上下文资源
│   └── artifacts.py            执行产物
├── 工作区能力
│   ├── workspace.py            工作区信息与设置
│   ├── workspace_observation.py 工作区观察事件
│   ├── resources.py            可发布资源
│   ├── tools.py                工具目录与选择
│   └── mcp.py                  MCP 配置与状态
└── 控制与配置
    ├── config.py               应用配置
    ├── runtime.py              运行时能力
    └── node_debug.py           Node 调试会话
```

路由按请求域拆分，跨域状态转换仍交给服务层；同属 `/sessions` 的 `messages.py`、`message_stream.py` 与 `session_turns.py` 分别负责读取、执行流和历史查询，不因共享 URL 前缀而合并为第二个会话 owner。

# 可修改内容

- 可以新增、调整或删除路由域的 `APIRouter` 与处理函数，并在 `app/main.py` 对应位置挂载。
- 可以在 `deps.py` 中补充服务依赖提供者，但必须从应用容器取实例，并保持初始化缺失时显式报错的行为。
- 可以调整请求/响应的 DTO 装配与 SSE 事件序列化调用，但 DTO 定义属于 `app/schemas`。

# 不可修改内容

- 不在本目录实现业务规则、会话状态计算或流程编排；这些属于 `app/services`，本目录只转发调用。
- 不重复实现 canonical ID 校验：路径/查询/请求体中的 `session_id`、`thread_id` 只能复用 `app.schemas.canonical_params` 指向的唯一验证器，不得另写清洗、截断或旧 ID 别名逻辑。
- 不得自行生成或补造 `request_id`：必须通过 `Depends(get_request_id)` 读取 TraceMiddleware 注入的权威值，禁止在响应里写入第二个请求 ID。
- 不绕过服务层直接读写 `${workspace_abs_path}/.boxteam/` 业务数据，也不在此实现 Gateway 的工作区路由与代理（属于 `app/gateway/`）。

# 规范

- 路由域文件名与 `APIRouter(prefix=..., tags=[...])` 的语义保持一致；新增路由域必须在 `app/main.py` 注册。
- 统一使用 `APIResponse(data=..., request_id=request_id)` 封装响应；SSE 端点统一走 `sse_heartbeat.stream_sse_with_heartbeat`，空闲间隔取 `SSE_HEARTBEAT_INTERVAL_SECONDS`。
- 处理函数签名中的依赖参数应按仓库既有顺序排列：路径/查询参数、请求体、`Depends(verify_local_token)`、`Depends(get_request_id)`、服务依赖。
- 失败必须显式抛出 `HTTPException` 或业务异常，禁止用虚假默认值或静默降级掩盖错误。
- 本目录的单元测试放在 `tests/unit/api/`，公开协议契约测试放在 `tests/contracts/api/`。
- 精确 thread 历史请求的 execution 选择器及游标合同归 DTO/history owner；API 只校验与转发，不建立第二历史投影、伪造 Turn 或唤醒 cold runtime。
