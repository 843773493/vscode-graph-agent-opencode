# 目录用途

`app/api/` 是工作区后端的 HTTP 接口适配层，承载 `/api/v1/*` 的全部路由。每个文件对应一个路由域，导出模块级 `router = APIRouter(prefix=..., tags=[...])`，由 `app/main.py` 统一以 `prefix="/api/v1"` 挂载。

本目录只做请求解析、参数校验、依赖注入与响应封装，不承载业务规则：

- `deps.py`：所有路由共享的 FastAPI 依赖提供者（从 `request.app.state.container` 取服务实例）与 `verify_local_token`；`get_request_id` 从 `app.core.trace_middleware` 重导出。
- `canonical_params.py`：`Annotated` 形式的 canonical `session_id` / `thread_id` 校验类型，复用 `app.core.session_catalog_store` 的唯一校验器。
- `sse_heartbeat.py`：把异步事件源包装成带空闲心跳的 SSE 流的统一实现。
- 其余 `*.py`：按业务域（`sessions`、`messages`、`workspace`、`jobs`、`tools`、`mcp`、`node_debug`、`runtime` 等）拆分的路由实现。

# 可修改内容

- 可以新增、调整或删除路由域的 `APIRouter` 与处理函数，并在 `app/main.py` 对应位置挂载。
- 可以在 `deps.py` 中补充服务依赖提供者，但必须从应用容器取实例，并保持初始化缺失时显式报错的行为。
- 可以调整请求/响应的 DTO 装配与 SSE 事件序列化调用，但 DTO 定义属于 `app/schemas`。

# 不可修改内容

- 不在本目录实现业务规则、会话状态计算或流程编排；这些属于 `app/services`，本目录只转发调用。
- 不重复实现 canonical ID 校验：路径/查询/请求体中的 `session_id`、`thread_id` 只能复用 `canonical_params.py` 指向的唯一验证器，不得另写清洗、截断或旧 ID 别名逻辑。
- 不得自行生成或补造 `request_id`：必须通过 `Depends(get_request_id)` 读取 TraceMiddleware 注入的权威值，禁止在响应里写入第二个请求 ID。
- 不绕过服务层直接读写 `${workspace_abs_path}/.boxteam/` 业务数据，也不在此实现 Gateway 的工作区路由与代理（属于 `app/gateway/`）。

# 规范

- 路由域文件名与 `APIRouter(prefix=..., tags=[...])` 的语义保持一致；新增路由域必须在 `app/main.py` 注册。
- 统一使用 `APIResponse(data=..., request_id=request_id)` 封装响应；SSE 端点统一走 `sse_heartbeat.stream_sse_with_heartbeat`，空闲间隔取 `SSE_HEARTBEAT_INTERVAL_SECONDS`。
- 处理函数签名中的依赖参数应按仓库既有顺序排列：路径/查询参数、请求体、`Depends(verify_local_token)`、`Depends(get_request_id)`、服务依赖。
- 失败必须显式抛出 `HTTPException` 或业务异常，禁止用虚假默认值或静默降级掩盖错误。
- 本目录的单元测试放在 `tests/unit/api/`，公开协议契约测试放在 `tests/contracts/api/`。
