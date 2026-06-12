# BoxTeam 本地工作区后端接口文档

## 1. 文档范围

本文档定义 BoxTeam 的本地工作区 Python 后端接口。

运行方式如下：

- 用户打开 VSCode 工作区
- 前端插件自动在本地拉起 Python 后端进程
- 后端仅服务当前机器、当前用户、当前工作区
- 前端通过 HTTP + SSE 与本地后端通信

后端能力包含：

- 单 Agent 执行任务
- 多 Agent 协同执行任务
- 异步任务运行
- 流式事件输出
- 任务暂停、恢复、取消、插话、跳步
- 工作区上下文感知
- 本地产物落盘
- 本地配置管理

---

## 2. 系统约束

### 2.1 运行范围

本后端运行于传入工作区环境，不面向云端部署，不面向多租户，不面向分布式集群。

### 2.2 存储位置

本地状态、日志、产物、缓存统一存储于传入工作区下的 `./boxteam` 目录。

目录结构如下：

```text
./boxteam/
├── logs/
├── artifacts/
├── cache/
└── config.json
```

### 2.3 网络范围

后端仅监听本地环回地址。

### 2.4 鉴权方式

接口使用本地 token 鉴权。

请求头：

```http
X-Local-Token: <random-secret>
```

---

## 3. 总体架构

```text
VSCode / Electron / Local Client
            |
            v
      Local FastAPI Server
            |
            +-- Workspace Manager
            +-- Session Manager
            +-- Job Manager
            +-- Agent Runtime
            +-- Orchestrator
            +-- Tool Registry
            +-- Event Stream Service
            +-- Local Storage (./workspace)
```

---

## 4. 核心资源模型

## 4.1 Workspace

```json
{
  "workspace_id": "ws_demo",
  "root_path": "/Users/name/project",
  "name": "demo-project",
  "project_type": "python",
  "git": {
    "enabled": true,
    "root": "/Users/name/project",
    "branch": "main"
  },
  "runtime": {
    "pid": 10234,
    "started_at": "2026-04-17T10:00:00Z"
  }
}
```

## 4.2 Session

```json
{
  "session_id": "sess_123",
  "workspace_id": "ws_demo",
  "title": "异步多 Agent 调试",
  "created_at": "2026-04-17T10:00:00Z",
  "updated_at": "2026-04-17T10:30:00Z"
}
```

## 4.3 Message

```json
{
  "message_id": "msg_001",
  "session_id": "sess_123",
  "role": "user",
  "content": "帮我分析这个工作区里的异步任务编排问题",
  "attachments": [],
  "created_at": "2026-04-17T10:01:00Z"
}
```

## 4.4 Job

```json
{
  "job_id": "job_001",
  "session_id": "sess_123",
  "mode": "multi_agent",
  "status": "queued",
  "entry_agent": "planner",
  "created_at": "2026-04-17T10:01:05Z"
}
```

## 4.5 Agent

```json
{
  "agent_id": "planner",
  "name": "PlannerAgent",
  "description": "负责任务拆解",
  "model": "gpt-4.1",
  "tools": ["workspace_search", "read_file"],
  "capabilities": ["planning", "routing"]
}
```

## 4.6 Event

```json
{
  "event_id": "evt_001",
  "job_id": "job_001",
  "type": "token",
  "agent_id": "planner",
  "payload": {
    "text": "我将先查看当前工作区的关键入口文件"
  },
  "timestamp": "2026-04-17T10:01:07Z"
}
```

## 4.7 Artifact

```json
{
  "artifact_id": "art_001",
  "job_id": "job_001",
  "type": "markdown",
  "name": "analysis_report.md",
  "path": "./workspace/artifacts/analysis_report.md"
}
```

---

## 5. 运行模式

## 5.1 单 Agent 模式

用于简单问答、单步代码分析、单工具调用、小范围文件处理。

## 5.2 多 Agent 模式

用于复杂任务分解、并行子任务处理、代码库分析、reviewer 汇总、summarizer 输出。

角色包含：

- `planner`
- `executor`
- `reviewer`
- `summarizer`

## 5.3 打断控制模式

后端必须支持：

- 取消任务
- 暂停任务
- 恢复任务
- 插入新的高优先级用户指令
- 跳过某一步或某个子 agent

## 5.4 Session 级调度约束

- 同一个 `session_id` 下，Job 必须串行排队执行。
- 仅当上一个 Job 进入终态（`completed/succeeded/failed/cancelled/timed_out`）后，才可启动下一个 Job。
- 不同 `session_id` 的 Job 可以异步并行执行。

---

## 6. 状态机

## 6.1 Job 状态

```text
queued -> running -> streaming -> succeeded
                  -> waiting_input
                  -> paused
                  -> interrupt_pending
                  -> cancelling
                  -> failed
                  -> cancelled
                  -> timed_out
```

## 6.2 Step 状态

```text
pending -> running -> completed
                 -> failed
                 -> skipped
                 -> cancelled
```

---

## 7. 数据与存储

### 7.1 存储范围

以下数据统一存储于 `./workspace`：

- 运行日志
- 任务产物
- 缓存文件
- 本地配置

### 7.2 目录用途

- `./workspace/logs/`：运行日志
- `./workspace/artifacts/`：任务输出文件
- `./workspace/cache/`：索引、摘要、repo map、临时缓存
- `./workspace/config.json`：本地配置文件

---

## 8. API 基础规范

- Base URL：`/api/v1`
- 数据格式：`application/json`
- 流式输出：`text/event-stream`
- 时间格式：ISO 8601 UTC

统一响应结构：

```json
{
  "code": 0,
  "message": "ok",
  "data": {},
  "request_id": "req_123"
}
```

统一错误结构：

```json
{
  "code": 401001,
  "message": "invalid local token",
  "details": {},
  "request_id": "req_123"
}
```

---

## 9. Workspace 接口

## 9.1 获取当前工作区信息

**GET** `/workspace`

响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "workspace_id": "ws_demo",
    "root_path": "/Users/name/project",
    "name": "demo-project",
    "project_type": "python",
    "git": {
      "enabled": true,
      "root": "/Users/name/project",
      "branch": "main"
    }
  }
}
```

## 9.2 获取工作区上下文

**GET** `/workspace/context`

响应字段包含：

- `workspace_id`
- `root_path`
- `project_type`
- `languages`
- `git`
- `index_status`
- `config`

## 9.3 获取索引状态

**GET** `/workspace/index`

## 9.4 重建索引

**POST** `/workspace/index/rebuild`

---

## 10. Runtime 接口

## 10.1 获取运行时状态

**GET** `/runtime/status`

响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "pid": 10234,
    "uptime_seconds": 3600,
    "workspace_id": "ws_demo",
    "active_jobs": 2,
    "loaded_agents": ["planner", "executor", "reviewer"],
    "storage": {
      "root": "./workspace",
      "artifact_dir": "./workspace/artifacts",
      "log_dir": "./workspace/logs",
      "cache_dir": "./workspace/cache"
    }
  }
}
```

## 10.2 关闭运行时

**POST** `/runtime/shutdown`

---

## 11. Session 接口

## 11.1 创建会话

**POST** `/sessions`

请求：

```json
{
  "title": "新任务"
}
```

## 11.2 获取会话列表

**GET** `/sessions?limit=20&cursor=...`

## 11.3 获取会话详情

**GET** `/sessions/{session_id}`

## 11.4 更新会话

**PATCH** `/sessions/{session_id}`

请求：

```json
{
  "title": "异步链路分析"
}
```

## 11.5 删除会话

**DELETE** `/sessions/{session_id}`

---

## 12. Message 接口

## 12.1 发送消息并启动任务

**POST** `/sessions/{session_id}/messages`

请求：

```json
{
  "message": {
    "role": "user",
    "content": "分析当前工作区的异步任务执行链路"
  },
  "run": {
    "mode": "multi_agent",
    "agent_id": "planner",
    "response_mode": "stream",
    "async": true,
    "max_steps": 20,
    "timeout_seconds": 600,
    "context": {
      "use_workspace_context": true
    }
  }
}
```

响应：

```json
{
  "code": 0,
  "message": "accepted",
  "data": {
    "message_id": "msg_101",
    "job_id": "job_001",
    "status": "queued"
  }
}
```

## 12.2 获取消息列表

**GET** `/sessions/{session_id}/messages`

## 12.3 获取单条消息

**GET** `/sessions/{session_id}/messages/{message_id}`

---

## 13. Job 接口

## 13.1 获取任务详情

**GET** `/jobs/{job_id}`

## 13.2 获取任务步骤

**GET** `/jobs/{job_id}/steps`

## 13.3 获取任务事件

**GET** `/jobs/{job_id}/events?after=evt_100&limit=100`

## 13.4 订阅任务事件流

**GET** `/jobs/{job_id}/events/stream`

事件示例：

```text
event: job.status
data: {"job_id":"job_001","status":"running"}

event: agent.thought
data: {"agent_id":"planner","text":"开始分析工作区入口文件"}

event: tool.start
data: {"tool":"workspace_search","input":{"query":"asyncio.create_task"}}

event: tool.end
data: {"tool":"workspace_search","ok":true}

event: token
data: {"agent_id":"summarizer","text":"当前任务链路分为三段"}

event: job.paused
data: {"job_id":"job_001"}

event: job.interrupted
data: {"job_id":"job_001","action":"replace_instruction"}

event: job.completed
data: {"job_id":"job_001","status":"succeeded"}
```

## 13.5 获取任务产物列表

**GET** `/jobs/{job_id}/artifacts`

---

## 14. Job 控制接口

## 14.1 统一任务控制

**POST** `/jobs/{job_id}/control`

请求结构：

```json
{
  "scope": "job",
  "action": "pause",
  "agent_id": null,
  "step_id": null,
  "message": null,
  "input": {}
}
```

### action

- `pause`
- `resume`
- `cancel`
- `skip`
- `replace_instruction`
- `append_instruction`
- `retry`

### scope

- `job`
- `agent`
- `step`

### 示例 1：暂停任务

```json
{
  "scope": "job",
  "action": "pause"
}
```

### 示例 2：恢复任务

```json
{
  "scope": "job",
  "action": "resume",
  "input": {
    "approved": true
  }
}
```

### 示例 3：取消任务

```json
{
  "scope": "job",
  "action": "cancel"
}
```

### 示例 4：替换当前指令

```json
{
  "scope": "job",
  "action": "replace_instruction",
  "message": "停止详细代码扫描，直接总结当前任务执行链路"
}
```

### 示例 5：取消某个子 Agent

```json
{
  "scope": "agent",
  "agent_id": "executor_b",
  "action": "cancel"
}
```

### 示例 6：跳过某一步

```json
{
  "scope": "step",
  "step_id": "step_003",
  "action": "skip"
}
```

响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "job_id": "job_001",
    "status": "paused",
    "control_state": "accepted"
  }
}
```

---

## 15. Agent 接口

## 15.1 获取 Agent 列表

**GET** `/agents`

## 15.2 获取 Agent 详情

**GET** `/agents/{agent_id}`

---

## 16. Tool 接口

## 16.1 获取 Tool 列表

**GET** `/tools`

## 16.2 获取 Tool 详情

**GET** `/tools/{tool_id}`

## 16.3 调试工具调用

**POST** `/tools/{tool_id}/invoke`

---

## 17. Artifact 接口

## 17.1 获取产物内容或下载

**GET** `/artifacts/{artifact_id}`

---

## 18. Config 接口

## 18.1 获取当前配置

**GET** `/config`

响应字段包含：

- `default_model`
- `default_orchestration`
- `max_concurrent_agents`
- `allow_shell_tools`
- `ignored_paths`
- `auto_summarize`

## 18.2 更新配置

**PATCH** `/config`

请求：

```json
{
  "default_model": "gpt-4.1",
  "max_concurrent_agents": 4,
  "allow_shell_tools": false,
  "ignored_paths": ["node_modules", ".git"],
  "auto_summarize": true
}
```

---

## 19. OpenAPI 元信息

```yaml
openapi: 3.1.0
info:
  title: BoxTeam Local Workspace API
  version: 1.0.0
  description: |
    BoxTeam 本地工作区异步多智能体 Python 后端接口。
servers:
  - url: /api/v1
security:
  - localTokenAuth: []
components:
  securitySchemes:
    localTokenAuth:
      type: apiKey
      in: header
      name: X-Local-Token
```

---

## 20. Pydantic Schema 定义

## 20.1 公共枚举与响应模型

```python
from enum import Enum
from typing import Generic, Optional, TypeVar
from pydantic import BaseModel, Field

T = TypeVar("T")

class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"
    system = "system"
    tool = "tool"

class RunMode(str, Enum):
    single_agent = "single_agent"
    multi_agent = "multi_agent"

class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    streaming = "streaming"
    waiting_input = "waiting_input"
    paused = "paused"
    interrupt_pending = "interrupt_pending"
    cancelling = "cancelling"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    timed_out = "timed_out"

class StepStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    skipped = "skipped"
    cancelled = "cancelled"

class ControlScope(str, Enum):
    job = "job"
    agent = "agent"
    step = "step"

class ControlAction(str, Enum):
    pause = "pause"
    resume = "resume"
    cancel = "cancel"
    skip = "skip"
    replace_instruction = "replace_instruction"
    append_instruction = "append_instruction"
    retry = "retry"

class APIResponse(BaseModel, Generic[T]):
    code: int = Field(default=0)
    message: str = Field(default="ok")
    data: Optional[T] = None
    request_id: Optional[str] = None

class CursorPage(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: Optional[str] = None
    has_more: bool = False
```

## 20.2 Workspace Schema

```python
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field

class WorkspaceDTO(BaseModel):
    workspace_id: str
    root_path: str
    name: str
    project_type: Optional[str] = None
    git: dict[str, Any] = Field(default_factory=dict)
    runtime: dict[str, Any] = Field(default_factory=dict)

class WorkspaceContextDTO(BaseModel):
    workspace_id: str
    root_path: str
    project_type: Optional[str] = None
    languages: list[str] = Field(default_factory=list)
    git: dict[str, Any] = Field(default_factory=dict)
    index_status: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
```

## 20.3 Session Schema

```python
from datetime import datetime
from typing import Optional
from pydantic import BaseModel

class SessionCreateRequest(BaseModel):
    title: Optional[str] = "新会话"

class SessionUpdateRequest(BaseModel):
    title: Optional[str] = None

class SessionDTO(BaseModel):
    session_id: str
    workspace_id: str
    title: str
    created_at: datetime
    updated_at: datetime
```

## 20.4 Message 与 Run Schema

```python
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field

class AttachmentRef(BaseModel):
    file_id: str
    name: Optional[str] = None
    content_type: Optional[str] = None

class MessageCreateRequest(BaseModel):
    role: MessageRole = MessageRole.user
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

class RunOptions(BaseModel):
    mode: RunMode = RunMode.single_agent
    agent_id: str
    response_mode: str = "stream"
    async_run: bool = Field(default=True, alias="async")
    max_steps: int = 20
    timeout_seconds: int = 600
    context: dict[str, Any] = Field(default_factory=dict)

class MessageRunRequest(BaseModel):
    message: MessageCreateRequest
    run: RunOptions

class MessageDTO(BaseModel):
    message_id: str
    session_id: str
    role: MessageRole
    content: str
    attachments: list[AttachmentRef] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

class MessageRunAccepted(BaseModel):
    message_id: str
    job_id: str
    status: JobStatus
```

## 20.5 Job、Event、Control、Artifact Schema

```python
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field, model_validator

from app.schemas.event import Event

class JobDTO(BaseModel):
    job_id: str
    session_id: str
    mode: RunMode
    status: JobStatus
    entry_agent: str
    progress: int = 0
    current_step: Optional[str] = None
    error_message: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    ended_at: Optional[datetime] = None

class StepDTO(BaseModel):
    step_id: str
    job_id: str
    parent_step_id: Optional[str] = None
    agent_id: Optional[str] = None
    step_type: str
    status: StepStatus
    input_payload: dict[str, Any] = Field(default_factory=dict)
    output_payload: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

class JobControlRequest(BaseModel):
    scope: ControlScope = ControlScope.job
    action: ControlAction
    agent_id: Optional[str] = None
    step_id: Optional[str] = None
    message: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_scope_target(self):
        if self.scope == ControlScope.agent and not self.agent_id:
            raise ValueError("agent scope requires agent_id")
        if self.scope == ControlScope.step and not self.step_id:
            raise ValueError("step scope requires step_id")
        if self.action in {ControlAction.replace_instruction, ControlAction.append_instruction} and not self.message:
            raise ValueError("instruction action requires message")
        return self

class JobControlResponseDTO(BaseModel):
    job_id: str
    status: JobStatus
    control_state: str

class ArtifactDTO(BaseModel):
    artifact_id: str
    job_id: str
    type: str
    name: str
    path: str
    metadata: dict[str, Any] = Field(default_factory=dict)
```

## 20.6 Config Schema

```python
from typing import Any
from pydantic import BaseModel, Field

class ConfigDTO(BaseModel):
    default_model: str
    default_orchestration: str
    max_concurrent_agents: int = 4
    allow_shell_tools: bool = False
    ignored_paths: list[str] = Field(default_factory=list)
    auto_summarize: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

class ConfigUpdateRequest(BaseModel):
    default_model: str | None = None
    default_orchestration: str | None = None
    max_concurrent_agents: int | None = None
    allow_shell_tools: bool | None = None
    ignored_paths: list[str] | None = None
    auto_summarize: bool | None = None
```

---

## 21. Routers 代码骨架

## 21.1 目录结构

```text
app/
├── api/
│   ├── deps.py
│   ├── workspace.py
│   ├── runtime.py
│   ├── sessions.py
│   ├── messages.py
│   ├── jobs.py
│   ├── agents.py
│   ├── tools.py
│   ├── artifacts.py
│   └── config.py
├── schemas/
│   ├── common.py
│   ├── workspace.py
│   ├── session.py
│   ├── message.py
│   ├── job.py
│   ├── agent.py
│   ├── tool.py
│   ├── artifact.py
│   └── config.py
├── services/
│   ├── workspace_service.py
│   ├── runtime_service.py
│   ├── session_service.py
│   ├── message_service.py
│   ├── job_service.py
│   ├── agent_service.py
│   ├── tool_service.py
│   ├── artifact_service.py
│   ├── config_service.py
│   └── event_service.py
├── core/
│   ├── exceptions.py
│   └── security.py
└── main.py
```

## 21.2 app/api/deps.py

```python
from __future__ import annotations

from fastapi import Header, HTTPException


def get_request_id(x_request_id: str | None = Header(default=None)) -> str | None:
    return x_request_id


def verify_local_token(x_local_token: str | None = Header(default=None)) -> str:
    expected = "local-dev-token"
    if x_local_token != expected:
        raise HTTPException(status_code=401, detail="invalid local token")
    return x_local_token
```

## 21.3 app/api/workspace.py

```python
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.workspace import WorkspaceContextDTO, WorkspaceDTO
from app.services.workspace_service import WorkspaceService

router = APIRouter(prefix="/workspace", tags=["workspace"])


@router.get("", response_model=APIResponse[WorkspaceDTO], summary="获取当前工作区信息")
async def get_workspace(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await WorkspaceService().get()
    return APIResponse(data=result, request_id=request_id)


@router.get("/context", response_model=APIResponse[WorkspaceContextDTO], summary="获取工作区上下文")
async def get_workspace_context(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await WorkspaceService().get_context()
    return APIResponse(data=result, request_id=request_id)


@router.get("/index", response_model=APIResponse[dict], summary="获取工作区索引状态")
async def get_workspace_index(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await WorkspaceService().get_index_status()
    return APIResponse(data=result, request_id=request_id)


@router.post("/index/rebuild", response_model=APIResponse[dict], summary="重建工作区索引")
async def rebuild_workspace_index(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await WorkspaceService().rebuild_index()
    return APIResponse(data=result, request_id=request_id)
```

## 21.4 app/api/runtime.py

```python
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.public_v2.common import APIResponse
from app.services.runtime_service import RuntimeService

router = APIRouter(prefix="/runtime", tags=["runtime"])


@router.get("/status", response_model=APIResponse[dict], summary="获取运行时状态")
async def get_runtime_status(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await RuntimeService().status()
    return APIResponse(data=result, request_id=request_id)


@router.post("/shutdown", response_model=APIResponse[dict], summary="关闭运行时")
async def shutdown_runtime(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await RuntimeService().shutdown()
    return APIResponse(data=result, request_id=request_id)
```

## 21.5 app/api/sessions.py

```python
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.public_v2.common import APIResponse, CursorPage
from app.schemas.public_v2.session import SessionCreateRequest, SessionDTO, SessionUpdateRequest
from app.services.business.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=APIResponse[SessionDTO], summary="创建会话")
async def create_session(
    payload: SessionCreateRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await SessionService().create(payload)
    return APIResponse(data=result, request_id=request_id)


@router.get("", response_model=APIResponse[CursorPage[SessionDTO]], summary="获取会话列表")
async def list_sessions(
    limit: int = 20,
    cursor: str | None = None,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await SessionService().list(limit=limit, cursor=cursor)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}", response_model=APIResponse[SessionDTO], summary="获取会话详情")
async def get_session(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await SessionService().get(session_id)
    return APIResponse(data=result, request_id=request_id)


@router.patch("/{session_id}", response_model=APIResponse[SessionDTO], summary="更新会话")
async def update_session(
    session_id: str,
    payload: SessionUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await SessionService().update(session_id, payload)
    return APIResponse(data=result, request_id=request_id)


@router.delete("/{session_id}", response_model=APIResponse[dict], summary="删除会话")
async def delete_session(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await SessionService().delete(session_id)
    return APIResponse(data=result, request_id=request_id)
```

## 21.6 app/api/messages.py

```python
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.public_v2.common import APIResponse, CursorPage
from app.schemas.public_v2.message import MessageDTO, MessageRunAccepted, MessageRunRequest
from app.services.message_service import MessageService

router = APIRouter(prefix="/sessions", tags=["messages"])


@router.post("/{session_id}/messages", response_model=APIResponse[MessageRunAccepted], summary="发送消息并创建任务")
async def create_message_and_run(
    session_id: str,
    payload: MessageRunRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await MessageService().create_and_run(session_id, payload)
    return APIResponse(message="accepted", data=result, request_id=request_id)


@router.get("/{session_id}/messages", response_model=APIResponse[CursorPage[MessageDTO]], summary="获取消息列表")
async def list_messages(
    session_id: str,
    limit: int = 50,
    cursor: str | None = None,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await MessageService().list(session_id=session_id, limit=limit, cursor=cursor)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{session_id}/messages/{message_id}", response_model=APIResponse[MessageDTO], summary="获取单条消息")
async def get_message(
    session_id: str,
    message_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await MessageService().get(session_id=session_id, message_id=message_id)
    return APIResponse(data=result, request_id=request_id)
```

## 21.7 app/api/jobs.py

```python
from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.api.deps import get_request_id, verify_local_token
from app.schemas.public_v2.artifact import ArtifactDTO
from app.schemas.event import Event as SSEEvent
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.job import JobControlRequest, JobControlResponseDTO, JobDTO, StepDTO
from app.services.artifact_service import ArtifactService
from app.services.event_service import EventService
from app.services.job_service import JobService

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=APIResponse[JobDTO], summary="获取任务详情")
async def get_job(
    job_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await JobService().get(job_id)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{job_id}/steps", response_model=APIResponse[list[StepDTO]], summary="获取任务步骤")
async def list_job_steps(
    job_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await JobService().list_steps(job_id)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{job_id}/events", response_model=APIResponse[list[SSEEvent]], summary="获取任务事件")
async def list_job_events(
    job_id: str,
    after: str | None = None,
    limit: int = 100,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await EventService().list(job_id=job_id, after=after, limit=limit)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{job_id}/events/stream", summary="订阅任务事件流")
async def stream_job_events(
    job_id: str,
    _: str = Depends(verify_local_token),
):
    async def event_generator():
        async for chunk in EventService().stream_sse(job_id):
            yield chunk

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.post("/{job_id}/control", response_model=APIResponse[JobControlResponseDTO], summary="控制任务")
async def control_job(
    job_id: str,
    payload: JobControlRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await JobService().control(job_id, payload)
    return APIResponse(data=result, request_id=request_id)


@router.get("/{job_id}/artifacts", response_model=APIResponse[list[ArtifactDTO]], summary="获取任务产物列表")
async def list_job_artifacts(
    job_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await ArtifactService().list_by_job(job_id)
    return APIResponse(data=result, request_id=request_id)
```

## 21.8 app/api/agents.py

```python
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.public_v2.agent import AgentDTO
from app.schemas.public_v2.common import APIResponse
from app.services.agent_service import AgentService

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=APIResponse[list[AgentDTO]], summary="获取 Agent 列表")
async def list_agents(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await AgentService().list()
    return APIResponse(data=result, request_id=request_id)


@router.get("/{agent_id}", response_model=APIResponse[AgentDTO], summary="获取 Agent 详情")
async def get_agent(
    agent_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await AgentService().get(agent_id)
    return APIResponse(data=result, request_id=request_id)
```

## 21.9 app/api/tools.py

```python
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.tool import ToolDTO, ToolInvokeRequest
from app.services.tool_service import ToolService

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("", response_model=APIResponse[list[ToolDTO]], summary="获取 Tool 列表")
async def list_tools(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await ToolService().list()
    return APIResponse(data=result, request_id=request_id)


@router.get("/{tool_id}", response_model=APIResponse[ToolDTO], summary="获取 Tool 详情")
async def get_tool(
    tool_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await ToolService().get(tool_id)
    return APIResponse(data=result, request_id=request_id)


@router.post("/{tool_id}/invoke", response_model=APIResponse[dict], summary="调用 Tool")
async def invoke_tool(
    tool_id: str,
    payload: ToolInvokeRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await ToolService().invoke(tool_id, payload)
    return APIResponse(data=result, request_id=request_id)
```

## 21.10 app/api/artifacts.py

```python
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import verify_local_token
from app.services.artifact_service import ArtifactService

router = APIRouter(prefix="/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}", summary="获取任务产物")
async def get_artifact(
    artifact_id: str,
    _: str = Depends(verify_local_token),
):
    return await ArtifactService().get_response(artifact_id)
```

## 21.11 app/api/config.py

```python
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_request_id, verify_local_token
from app.schemas.public_v2.common import APIResponse
from app.schemas.public_v2.config import ConfigDTO, ConfigUpdateRequest
from app.services.infrastructure.config_service import ConfigService

router = APIRouter(prefix="/config", tags=["config"])


@router.get("", response_model=APIResponse[ConfigDTO], summary="获取配置")
async def get_config(
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await ConfigService().get()
    return APIResponse(data=result, request_id=request_id)


@router.patch("", response_model=APIResponse[ConfigDTO], summary="更新配置")
async def update_config(
    payload: ConfigUpdateRequest,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
):
    result = await ConfigService().update(payload)
    return APIResponse(data=result, request_id=request_id)
```

---

## 22. 服务边界

后端服务包含：

- `WorkspaceService`
- `RuntimeService`
- `SessionService`
- `MessageService`
- `JobService`
- `AgentService`
- `ToolService`
- `ArtifactService`
- `ConfigService`
- `EventService`

多智能体编排作为 `JobService / Runtime` 内部能力实现。

---

## 23. 执行控制要求

## 23.1 JobManager

进程内 `JobManager` 负责：

- 保存当前活跃 job
- 维护 job -> asyncio.Task 映射
- 维护 job 状态
- 分发取消、暂停、恢复、插话信号

## 23.2 EventBuffer

进程内事件缓冲区负责：

- 为每个 job 维护 ring buffer
- 支持 SSE 推流
- 支持轮询补偿
- 仅保留最近 N 条事件

## 23.3 中断检查点

运行时在以下边界检查控制信号：

- LLM 调用前
- 工具调用前
- 工具调用后
- step 完成后
- 子 agent fan-out 前
- summarizer 汇总前

---

## 24. main.py

```python
from __future__ import annotations

from fastapi import FastAPI

from app.api.agents import router as agents_router
from app.api.artifacts import router as artifacts_router
from app.api.config import router as config_router
from app.api.jobs import router as jobs_router
from app.api.messages import router as messages_router
from app.api.runtime import router as runtime_router
from app.api.sessions import router as sessions_router
from app.api.tools import router as tools_router
from app.api.workspace import router as workspace_router

app = FastAPI(
    title="BoxTeam Local Workspace API",
    version="1.0.0",
    openapi_url="/api/v1/openapi.json",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
)

app.include_router(workspace_router, prefix="/api/v1")
app.include_router(runtime_router, prefix="/api/v1")
app.include_router(sessions_router, prefix="/api/v1")
app.include_router(messages_router, prefix="/api/v1")
app.include_router(jobs_router, prefix="/api/v1")
app.include_router(agents_router, prefix="/api/v1")
app.include_router(tools_router, prefix="/api/v1")
app.include_router(artifacts_router, prefix="/api/v1")
app.include_router(config_router, prefix="/api/v1")
```

---

## 25. 实现范围

后端实现以下接口：

- `GET /workspace`
- `GET /workspace/context`
- `GET /workspace/index`
- `POST /workspace/index/rebuild`
- `GET /runtime/status`
- `POST /runtime/shutdown`
- `POST /sessions`
- `GET /sessions`
- `GET /sessions/{session_id}`
- `PATCH /sessions/{session_id}`
- `DELETE /sessions/{session_id}`
- `POST /sessions/{session_id}/messages`
- `GET /sessions/{session_id}/messages`
- `GET /sessions/{session_id}/messages/{message_id}`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/steps`
- `GET /jobs/{job_id}/events`
- `GET /jobs/{job_id}/events/stream`
- `POST /jobs/{job_id}/control`
- `GET /jobs/{job_id}/artifacts`
- `GET /agents`
- `GET /agents/{agent_id}`
- `GET /tools`
- `GET /tools/{tool_id}`
- `POST /tools/{tool_id}/invoke`
- `GET /artifacts/{artifact_id}`
- `GET /config`
- `PATCH /config`

---

## 26. 文档结论

本接口文档定义 BoxTeam 的本地工作区异步多智能体后端。

后端具有以下特征：

- 本地运行
- 单工作区隔离
- HTTP + SSE 通信
- 统一使用 `./workspace` 目录存储本地状态与产物
- 统一使用 `X-Local-Token` 进行本地鉴权
- 统一使用 Job 控制接口实现暂停、恢复、取消、插话与跳步
- 多智能体编排由运行时内部实现

该文档作为 BoxTeam 本地后端接口与代码实现的标准契约。
