# 统一 Trace 事件流与回放接口重构计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 让后端的事件总线、Trace 持久化、实时 SSE 与历史回放接口使用同一套事件模型，前端只需按 `session_id` 订阅一条 SSE 流即可实时渲染 Agent 完整执行过程。

**Architecture:**
- 以 `app.schemas.event.Event` discriminated union 作为唯一事件模型。
- `TraceEventStore` 负责按 `session_id` 把事件追加到 `trace_{session_id}.jsonl`，并支持"历史读取 + 实时等待新事件"。
- `TraceEventRecorder` 通过 `JobEventBus` 新增的全局订阅监听所有事件，推断 `session_id` 后写入 `TraceEventStore`。
- `ExecutionTraceMiddleware` 不再直接写文件；它把 `tool_call_start/end`、`agent_start/end`、`llm_request` 等事件发布到 `JobEventBus`。
- `/api/v1/sessions/{session_id}/traces/stream` 输出统一 envelope 的 SSE，字段与 `/api/v1/sessions/{session_id}/traces` 完全一致。
- e2e 测试改为：创建 session → 订阅 session trace stream → 发送消息 → 通过 SSE 断言 `tool_call_start`、`tool_call_end`、`agent_end` 顺序出现。

**Tech Stack:** FastAPI, Pydantic v2, pytest-asyncio, uvicorn.

---

## Task 1: 扩展 JobEventBus 支持全局事件订阅

**Files:**
- Modify: `app/abstractions/job_event_bus.py`
- Modify: `app/core/job_event_bus.py`
- Test: `tests/unit/core/test_job_event_bus.py`（新建）

**Step 1: 写失败测试**

```python
import asyncio
import pytest
from app.core.job_event_bus import JobEventBus, EventType


@pytest.mark.asyncio
async def test_subscribe_all_receives_all_events():
    bus = JobEventBus()
    queue = await bus.subscribe_all()

    event = await bus.publish(
        job_id="job_1",
        event_type=EventType.JOB_CREATED,
        payload={"session_id": "ses_1", "message": "hi", "agent_id": "default"},
        agent_id="test",
    )

    received = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert received.event_id == event.event_id
    assert received.type == "job_created"
```

**Step 2: 运行测试确认失败**

Run: `pytest tests/unit/core/test_job_event_bus.py::test_subscribe_all_receives_all_events -v`
Expected: FAIL `AttributeError: 'JobEventBus' object has no attribute 'subscribe_all'`

**Step 3: 实现协议与全局订阅**

在 `app/abstractions/job_event_bus.py` 增加：

```python
async def subscribe_all(self) -> asyncio.Queue[Event]: ...
async def unsubscribe_all(self, queue: asyncio.Queue[Event]) -> None: ...
```

在 `app/core/job_event_bus.py` `__init__` 增加：

```python
self._global_subscribers: Set[asyncio.Queue[Event]] = set()
```

增加方法：

```python
async def subscribe_all(self) -> asyncio.Queue[Event]:
    queue = asyncio.Queue(maxsize=1000)
    async with self._lock:
        self._global_subscribers.add(queue)
        self._listener_count += 1
    return queue

async def unsubscribe_all(self, queue: asyncio.Queue[Event]) -> None:
    async with self._lock:
        self._global_subscribers.discard(queue)
        self._listener_count -= 1
```

在 `publish` 的广播逻辑里，把事件也推给 `_global_subscribers`：

```python
if self._listener_count > 0:
    for queue in list(self._subscribers.get(job_id, [])):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
    for queue in list(self._global_subscribers):
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass
```

**Step 4: 运行测试确认通过**

Run: `pytest tests/unit/core/test_job_event_bus.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/abstractions/job_event_bus.py app/core/job_event_bus.py tests/unit/core/test_job_event_bus.py
git commit -m "feat(job_event_bus): add global event subscription for trace recording"
```

---

## Task 2: 创建 TraceEventStore（事件持久化与实时读取）

**Files:**
- Create: `app/services/infrastructure/trace_event_store.py`
- Test: `tests/unit/services/infrastructure/test_trace_event_store.py`（新建）

**Step 1: 写失败测试**

```python
import asyncio
import pytest
from pathlib import Path
from datetime import datetime, timezone

from app.schemas.event import AgentStartEvent, AgentStartPayload
from app.services.infrastructure.trace_event_store import TraceEventStore


@pytest.mark.asyncio
async def test_store_append_and_stream(tmp_path: Path):
    store = TraceEventStore(logs_dir=tmp_path)
    session_id = "ses_1"

    event = AgentStartEvent(
        event_id="evt_1",
        job_id="job_1",
        session_id=session_id,
        agent_id="default",
        timestamp=datetime.now(timezone.utc),
        payload=AgentStartPayload(message="start", agent_id="default"),
    )
    store.append(session_id, event)

    assert len(store.read_events(session_id)) == 1

    stream = store.stream_events(session_id)
    received = await asyncio.wait_for(stream.asend(None), timeout=1.0)
    assert received.event_id == "evt_1"
```

**Step 2: 运行测试确认失败**

Run: `pytest tests/unit/services/infrastructure/test_trace_event_store.py::test_store_append_and_stream -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.services.infrastructure.trace_event_store'`

**Step 3: 实现 TraceEventStore**

```python
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import AsyncGenerator

from app.schemas.event import Event


class TraceEventStore:
    def __init__(self, logs_dir: Path) -> None:
        self._logs_dir = logs_dir
        self._conditions: dict[str, asyncio.Condition] = defaultdict(asyncio.Condition)

    def _trace_file(self, session_id: str) -> Path:
        return self._logs_dir / "traces" / f"trace_{session_id}.jsonl"

    def append(self, session_id: str, event: Event) -> None:
        file = self._trace_file(session_id)
        file.parent.mkdir(parents=True, exist_ok=True)
        with open(file, "a", encoding="utf-8") as f:
            f.write(event.model_dump_json() + "\n")
        condition = self._conditions[session_id]
        asyncio.get_running_loop().call_soon_threadsafe(condition.notify_all)

    def read_events(self, session_id: str) -> list[Event]:
        file = self._trace_file(session_id)
        if not file.exists():
            return []

        events: list[Event] = []
        with open(file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                events.append(self._parse_event(line))
        return events

    async def stream_events(self, session_id: str) -> AsyncGenerator[Event, None]:
        seen = 0
        condition = self._conditions[session_id]
        while True:
            events = self.read_events(session_id)
            new_events = events[seen:]
            for event in new_events:
                yield event
            seen = len(events)
            async with condition:
                try:
                    await asyncio.wait_for(condition.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    @staticmethod
    def _parse_event(line: str) -> Event:
        from pydantic import RootModel
        class _AnyEvent(RootModel[Event]):
            pass
        return _AnyEvent.model_validate_json(line).root
```

注意：`append` 可能在非异步上下文被调用（middleware 同步写文件），所以使用 `call_soon_threadsafe`。更好的方式是要求 `append` 为 async。这里先保持 sync 兼容，但需要在 loop 已存在时才能 notify。如果 loop 不存在则跳过 notify，stream 会靠 1s 超时轮询兜底。

**Step 4: 运行测试确认通过**

Run: `pytest tests/unit/services/infrastructure/test_trace_event_store.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add app/services/infrastructure/trace_event_store.py tests/unit/services/infrastructure/test_trace_event_store.py
git commit -m "feat(trace_event_store): persistent session event store with streaming"
```

---

## Task 3: 创建 TraceEventRecorder（全局监听并持久化事件）

**Files:**
- Create: `app/services/orchestration/trace_event_recorder.py`
- Modify: `app/container.py`
- Test: `tests/unit/services/orchestration/test_trace_event_recorder.py`（新建）

**Step 1: 写失败测试**

```python
import asyncio
import pytest
from pathlib import Path

from app.core.job_event_bus import JobEventBus, EventType
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.trace_event_recorder import TraceEventRecorder


@pytest.mark.asyncio
async def test_recorder_persists_job_events(tmp_path: Path):
    bus = JobEventBus()
    store = TraceEventStore(logs_dir=tmp_path)
    recorder = TraceEventRecorder(bus=bus, store=store)
    await recorder.start()

    try:
        await bus.publish(
            job_id="job_1",
            event_type=EventType.JOB_CREATED,
            payload={"session_id": "ses_1", "message": "hi", "agent_id": "default"},
            agent_id="test",
        )
        await bus.publish(
            job_id="job_1",
            event_type=EventType.AGENT_START,
            payload={"message": "start", "agent_id": "default"},
            agent_id="default",
        )

        await asyncio.sleep(0.2)
        events = store.read_events("ses_1")
        assert [e.type for e in events] == ["job_created", "agent_start"]
    finally:
        await recorder.stop()
```

**Step 2: 运行测试确认失败**

Run: `pytest tests/unit/services/orchestration/test_trace_event_recorder.py::test_recorder_persists_job_events -v`
Expected: FAIL `ModuleNotFoundError`

**Step 3: 实现 TraceEventRecorder**

```python
from __future__ import annotations

import asyncio
from typing import Any

from app.abstractions.job_event_bus import JobEventBusProtocol
from app.schemas.event import Event
from app.services.infrastructure.trace_event_store import TraceEventStore


class TraceEventRecorder:
    def __init__(self, *, bus: JobEventBusProtocol, store: TraceEventStore) -> None:
        self._bus = bus
        self._store = store
        self._job_sessions: dict[str, str] = {}
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        queue = await self._bus.subscribe_all()
        try:
            while True:
                event = await queue.get()
                self._handle_event(event)
        finally:
            await self._bus.unsubscribe_all(queue)

    def _handle_event(self, event: Event) -> None:
        session_id = self._resolve_session_id(event)
        if not session_id:
            return

        if event.type == "job_created":
            self._job_sessions[event.job_id] = session_id

        self._store.append(session_id, event)

    def _resolve_session_id(self, event: Event) -> str | None:
        payload = event.payload
        if hasattr(payload, "session_id"):
            value = getattr(payload, "session_id")
            if isinstance(value, str) and value:
                return value

        raw = event.model_dump(mode="json")
        payload_raw = raw.get("payload") or {}
        for key in ("session_id", "thread_id"):
            value = payload_raw.get(key) or raw.get(key)
            if isinstance(value, str) and value:
                return value

        return self._job_sessions.get(event.job_id)
```

**Step 4: 修改 container 启动 recorder**

在 `app/container.py` `build_app_container` 中，创建 `TraceEventStore` 与 `TraceEventRecorder`：

```python
from app.core.path_utils import get_logs_dir
from app.services.infrastructure.trace_event_store import TraceEventStore
from app.services.orchestration.trace_event_recorder import TraceEventRecorder

def build_app_container() -> AppContainer:
    ...
    trace_event_store = TraceEventStore(logs_dir=get_logs_dir())
    trace_event_recorder = TraceEventRecorder(bus=job_event_bus, store=trace_event_store)
    asyncio.create_task(trace_event_recorder.start())
    ...
    return AppContainer(
        ...
        trace_event_store=trace_event_store,
        trace_event_recorder=trace_event_recorder,
    )
```

并在 `AppContainer` dataclass 中增加字段。

**Step 5: 运行测试确认通过**

Run: `pytest tests/unit/services/orchestration/test_trace_event_recorder.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add app/services/orchestration/trace_event_recorder.py app/container.py tests/unit/services/orchestration/test_trace_event_recorder.py
git commit -m "feat(trace_event_recorder): persist all job events to session trace files"
```

---

## Task 4: ExecutionTraceMiddleware 改为发布事件而不是写文件

**Files:**
- Modify: `app/agents/agent_middleware.py`

**Step 1: 分析当前代码**

当前 `ExecutionTraceMiddleware._save_trace_event` 直接写文件。需要改为发布到 `JobEventBus`。

**Step 2: 修改 `_save_trace_event` 为 `_publish_trace_event`**

```python
def _publish_trace_event(self, session_id: str, event_type: str, data: dict[str, Any]) -> None:
    try:
        if self._job_event_bus is None:
            raise RuntimeError("ExecutionTraceMiddleware 未绑定 JobEventBus")
        event = self._build_trace_event(session_id=session_id, event_type=event_type, data=data)
        job_id = event.job_id
        asyncio.create_task(self._job_event_bus.publish(
            job_id=job_id,
            event_type=event_type,
            payload=data,
            step_id=event.step_id,
            agent_id=event.agent_id,
        ))
    except Exception as exc:
        logger.exception(...)
        raise
```

注意：`wrap_tool_call` 和 `awrap_tool_call` 当前没有 `job_id`。需要像 `_get_session_id` 一样增加 `_get_job_id(runtime)`。

**Step 3: 增加 `_get_job_id`**

```python
def _get_job_id(self, runtime: Runtime[Any]) -> str:
    configurable = getattr(runtime, 'configurable', None)
    if isinstance(configurable, dict):
        job_id = configurable.get('job_id')
        if job_id:
            return job_id
    return self._get_session_id(runtime)
```

**Step 4: 更新调用点**

- `before_agent`: `self._publish_trace_event(session_id, "agent_start", {...})`
- `after_agent`: `self._publish_trace_event(session_id, "agent_end", {...})`
- `wrap_tool_call`/`awrap_tool_call`: 使用 `job_id = self._get_job_id(request.runtime)`，发布 `tool_call_start/end`。

**Step 5: 删除写文件逻辑**

移除 `_save_trace_event`、`_build_trace_event` 中写文件的部分。保留 `_build_trace_event` 仅作为格式辅助，或直接 inline。

更干净的做法：直接调用 `bus.publish`，不再需要 `_build_trace_event`。

**Step 6: 运行相关测试**

Run: `pytest tests/unit/agents/test_agent_middleware.py -v`（如果有）
如果无单元测试，运行：
Run: `pytest tests/e2e/test_deepagent_integration.py -v -s`
Expected: 当前测试可能仍通过，但 trace 文件现在由 recorder 写入。

**Step 7: Commit**

```bash
git add app/agents/agent_middleware.py
git commit -m "refactor(agent_middleware): publish trace events to bus instead of writing files"
```

---

## Task 5: SessionService 使用 TraceEventStore 提供 trace 接口

**Files:**
- Modify: `app/services/business/session_service.py`
- Modify: `app/api/deps.py`
- Modify: `app/api/sessions.py`
- Test: `tests/unit/services/business/test_session_service.py`（已有或新建）

**Step 1: 修改 SessionService 依赖 TraceEventStore**

```python
from app.services.infrastructure.trace_event_store import TraceEventStore

class SessionService:
    def __init__(self, *, config_service: ConfigService, trace_event_store: TraceEventStore):
        self._config_service = config_service
        self._trace_event_store = trace_event_store
```

**Step 2: 重写 list_trace_events 和 stream_trace_events**

```python
async def list_trace_events(self, session_id: str) -> list[Event]:
    await self.get(session_id)  # 验证 session 存在
    return self._trace_event_store.read_events(session_id)

async def stream_trace_events(self, session_id: str):
    await self.get(session_id)
    async for event in self._trace_event_store.stream_events(session_id):
        yield event
```

注意：这里返回 `Event` 列表而不是 `TraceEventDTO`。需要同步修改 `app/api/sessions.py` 的 response_model。

**Step 3: 修改 container 中 SessionService 初始化**

```python
session_service = SessionService(config_service=config_service, trace_event_store=trace_event_store)
```

**Step 4: 修改 API 返回类型**

`app/api/sessions.py`：

```python
from app.schemas.event import Event

@router.get("/{session_id}/traces", response_model=APIResponse[list[Event]], summary="获取会话执行轨迹")
async def list_session_traces(...):
    result = await session_service.list_trace_events(session_id)
    return APIResponse(data=result, request_id=request_id)

@router.get("/{session_id}/traces/stream", summary="订阅会话执行轨迹流")
async def stream_session_traces(...):
    async def event_generator():
        async for event in session_service.stream_trace_events(session_id):
            yield f"event: trace\n"
            yield f"data: {event.model_dump_json()}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**Step 5: 运行测试**

Run: `pytest tests/e2e/test_deepagent_integration.py -v -s`
Expected: 可能通过（因为测试最后拉 traces），也可能因返回格式改变而失败。

**Step 6: Commit**

```bash
git add app/services/business/session_service.py app/api/sessions.py app/api/deps.py app/container.py
git commit -m "feat(sessions): serve unified events from TraceEventStore"
```

---

## Task 6: 新增 `/sessions/{session_id}/events/stream` 统一事件 SSE 接口

**Files:**
- Modify: `app/api/sessions.py`
- Test: `tests/e2e/test_deepagent_integration.py`

**Step 1: 在 sessions.py 增加新端点**

```python
@router.get("/{session_id}/events/stream", summary="订阅会话统一事件流")
async def stream_session_events(
    session_id: str,
    _: str = Depends(verify_local_token),
    session_service: SessionService = Depends(get_session_service),
):
    async def event_generator():
        async for event in session_service.stream_trace_events(session_id):
            yield f"event: {event.type}\n"
            yield f"data: {event.model_dump_json()}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

**Step 2: 重构 e2e 测试使用新接口**

修改 `tests/e2e/test_deepagent_integration.py`：

```python
async with client.stream("GET", f"/api/v1/sessions/{session_id}/events/stream") as stream_response:
    assert stream_response.status_code == 200

    message_response = await client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={...},
    )
    assert message_response.status_code == 200
    job_id = message_response.json()["data"]["job_id"]

    received_types = []
    async for line in stream_response.aiter_lines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line.removeprefix("event:").strip()
            received_types.append(event_type)
        # 当收到 agent_end 且 job 完成时退出
        if "agent_end" in received_types:
            result = await wait_for_job_done(client, job_id)
            if result["status"] in {"completed", "succeeded"}:
                break
```

更简洁的方式：写一个 helper `_read_sse_events_until`，读取 SSE 直到某个条件或超时。

**Step 3: 断言事件序列**

```python
traces_response = await client.get(f"/api/v1/sessions/{session_id}/traces")
traces = traces_response.json()["data"]
trace_types = [t["type"] for t in traces]
assert "tool_call_start" in trace_types
assert "tool_call_end" in trace_types
assert "agent_end" in trace_types

tool_end = next(t for t in traces if t["type"] == "tool_call_end")
assert "2333" in tool_end["payload"]["result"] or "2333" in json.dumps(tool_end["payload"], ensure_ascii=False)
```

**Step 4: 运行 e2e 测试**

Run: `pytest tests/e2e/test_deepagent_integration.py -v -s`
Expected: PASS

**Step 5: Commit**

```bash
git add app/api/sessions.py tests/e2e/test_deepagent_integration.py
git commit -m "feat(api): add /sessions/{id}/events/stream and update e2e test"
```

---

## Task 7: 清理旧的 observation 映射与不兼容接口

**Files:**
- Modify: `app/services/event_service.py`
- Modify: `app/api/jobs.py`
- Optional: Keep `/jobs/{job_id}/events/stream` but switch to unified envelope.

**Step 1: 决定 `/jobs/{job_id}/events/stream` 行为**

可选方案：
A. 保留 observation 语义（为了兼容前端），但底层基于 trace 事件生成。
B. 改为统一 envelope 语义。

建议选择 A 暂时保留，因为前端当前依赖 observation 事件类型。但底层不再用 `map_event_to_observation_sse`，而是用 `TraceEventStore` 读取该 job 的事件并映射。

为了简化，本计划只要求 `/jobs/{job_id}/events`（列表）改为返回统一 `Event` 列表；`/jobs/{job_id}/events/stream` 保持 observation 语义不变，避免前端大面积修改。

**Step 2: 更新 `/jobs/{job_id}/events` response_model**

```python
from app.schemas.event import Event

@router.get("/{job_id}/events", response_model=APIResponse[list[Event]], summary="获取任务事件")
async def list_job_events(...):
    result = await event_service.list(job_id=job_id, after=after, limit=limit)
    return APIResponse(data=result, request_id=request_id)
```

**Step 3: 运行测试**

Run: `pytest tests/e2e/ -v -s`
Expected: PASS

**Step 4: Commit**

```bash
git add app/api/jobs.py
git commit -m "refactor(jobs): list_job_events returns unified Event list"
```

---

## Task 8: 运行全量 e2e 与单元测试

**Step 1: 运行全量测试**

Run: `pytest tests/e2e/test_deepagent_integration.py tests/unit/ -v`
Expected: PASS

**Step 2: 如果失败，使用 systematic-debugging 定位**

**Step 3: Commit（如果测试通过）**

```bash
git commit -m "test: verify unified trace event flow end-to-end"
```

---

## 风险与回退

- `ExecutionTraceMiddleware` 同步调用中发布事件时如果 `asyncio.create_task` 失败（无 running loop），会导致事件丢失。可以在 `TraceEventRecorder` 启动前预先把事件写到一个同步缓冲区，recorder 启动后刷入 store。
- `Event` union 解析失败会打断 stream。需要确保所有写入 trace 文件的事件都是合法的 `Event` 子类。
- `/sessions/{id}/traces/stream` 旧格式前端可能依赖。由于本重构目标就是让前端改用统一事件，可以接受旧格式变化。

## 需要新增的 AGENTS.md

新增目录需要 `AGENTS.md`：
- `app/services/infrastructure/AGENTS.md` 已存在，补充 `trace_event_store.py` 说明。
- `app/services/orchestration/AGENTS.md` 已存在，补充 `trace_event_recorder.py` 说明。
- `tests/unit/core/AGENTS.md` 如果不存在，新建。
- `tests/unit/services/infrastructure/AGENTS.md` 如果不存在，新建。
- `tests/unit/services/orchestration/AGENTS.md` 如果不存在，新建。
