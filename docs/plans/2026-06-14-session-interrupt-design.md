# 设计：Session 级用户打断与消息历史注入

## 背景

当前后端已有 `JobService.control(..., cancel)` 可以取消单个 Job，但缺少“用户主动打断当前会话”这一语义：
- 不知道当前 Job 被打断在什么阶段（text 输出 / tool 调用）。
- 不会把 `<system_reminder>` 直接写回消息历史。
- 下一次 Job 运行时，需要依赖 `SystemReminderMiddleware` 重新注入提醒。

本设计新增 `POST /api/v1/sessions/{session_id}/interrupt`：在取消当前 active job 的同时，**直接把 `<system_reminder>` 追加到消息历史里**，不把额外处理留给下一次会话。

## 目标

1. 提供一个 session 级打断接口。
2. 根据当前执行阶段判断打断位置（text / tool）。
3. 直接在 `messages.jsonl` 中对应消息的 content 末尾追加 `<system_reminder>`。
4. 取消当前 active job，并发布 `job_cancelled` / `session_interrupted` 事件。
5. 后续 Job 启动时不需要再做打断相关处理。

## 核心设计

### 1. 接口

```http
POST /api/v1/sessions/{session_id}/interrupt
```

响应：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "session_id": "ses_xxx",
    "job_id": "job_xxx",
    "status": "cancelling",
    "phase": "text",
    "interrupted_at": "2026-06-14T14:31:14+08:00"
  }
}
```

若 session 当前没有 running 的 job，返回 `409 Conflict` 或 `400 Bad Request`。

### 2. 执行阶段暴露

`AgentExecutionService` 在运行 `agent.astream_events` 时维护当前阶段：

```python
# app/core/job_context.py 新增
_current_interruptible_phase: ContextVar[str | None] = ContextVar(...)

# app/services/orchestration/agent_execution_service.py
# 在 TEXT_START 时 set_current_interruptible_phase("text")
# 在 TOOL_CALL_START 时 set_current_interruptible_phase("tool")
# 在 finally 中 reset
```

新增 context var 函数：

```python
get_interruptible_phase()
set_interruptible_phase(phase: str | None)
reset_interruptible_phase(token)
```

阶段值：
- `"text"`：模型正在输出文本（收到 `TEXT_START` 后尚未 `TEXT_END`）。
- `"tool"`：模型已发起 tool call，正在等待/处理工具响应（收到 `TOOL_CALL_START` 后尚未 `TOOL_CALL_END`）。
- `None`：当前没有可打断的执行阶段。

### 3. 打断位置判断

打断接口实现时读取 `get_interruptible_phase()`：
- `"text"`：被打断在 assistant 文本生成阶段。
- `"tool"`：被打断在 tool 调用阶段。
- `None`：如果当前 Job 还没进入模型调用，可视为 text 阶段 fallback 或直接拒绝。

同时从 `AgentExecutionService` 暴露当前活跃 tool call 名称，供动态文案使用：

```python
_current_active_tool_name: ContextVar[str | None]
```

在 `TOOL_CALL_START` 时设置，在 `TOOL_CALL_END` / `finally` 时重置。

### 4. 消息历史修改策略

由 `MessageService` 新增方法：

```python
async def append_system_reminder_to_last_message(
    self,
    session_id: str,
    *,
    phase: str,
    tool_name: str | None = None,
    interrupted_at: datetime,
) -> MessageDTO:
    ...
```

实现步骤：
1. 读取 `messages.jsonl` 全部消息。
2. 根据 `phase` 找到目标消息：
   - `phase == "text"`：取最后一条 `role == "assistant"` 的消息。
   - `phase == "tool"`：取最后一条 `role == "tool"` 的消息。
3. 若未找到，fallback 到列表最后一条消息（任意 role）。
4. 生成文案：
   ```
   <system_reminder>
   用户在 {phase_desc} 过程中于 {interrupted_at} 打断。
   {tool_info}
   请停止当前操作，根据已有信息回应用户最新请求。
   </system_reminder>
   ```
   其中：
   - `phase_desc`：文本生成 / 工具调用。
   - `tool_info`：tool 阶段时追加 `当前工具调用：{tool_name} 已被取消。`
5. 在目标消息 content 末尾换行追加上述文案。
6. 重写整个 `messages.jsonl`。
7. 返回更新后的消息。

### 5. 打断流程

新增 `SessionInterruptService`（或放在 `SessionService` 中）：

```python
class SessionInterruptService:
    def __init__(
        self,
        *,
        job_service: JobServiceProtocol,
        message_service: MessageService,
        job_event_bus: JobEventBusProtocol,
    ) -> None: ...

    async def interrupt(self, session_id: str) -> SessionInterruptResultDTO:
        # 1. 获取当前 session active job
        jobs = await self._job_service.list(session_id=session_id)
        active_job = next(
            (job for job in jobs if job.status in {JobStatus.running, JobStatus.streaming, JobStatus.waiting_input}),
            None,
        )
        if active_job is None:
            raise ValueError(f"Session {session_id} 当前没有正在运行的任务")

        # 2. 读取当前阶段
        phase = get_interruptible_phase()
        tool_name = get_active_tool_name()
        interrupted_at = datetime.now(timezone.utc)

        # 3. 直接修改消息历史
        await self._message_service.append_system_reminder_to_last_message(
            session_id,
            phase=phase or "text",
            tool_name=tool_name,
            interrupted_at=interrupted_at,
        )

        # 4. 取消当前 job
        await self._job_service.control(
            active_job.job_id,
            JobControlRequest(action=ControlAction.cancel),
        )

        # 5. 发布事件
        await self._job_event_bus.publish(
            job_id=active_job.job_id,
            event_type=EventType.SESSION_INTERRUPTED,
            payload={
                "session_id": session_id,
                "phase": phase,
                "tool_name": tool_name,
                "interrupted_at": interrupted_at.isoformat(),
            },
            agent_id="session_service",
        )

        return SessionInterruptResultDTO(...)
```

### 6. 事件

新增事件类型 `session_interrupted`（或复用 `job_cancelled` 并附加 payload）。

Schema：

```python
class SessionInterruptedPayload(BaseModel):
    session_id: str
    job_id: str
    phase: str
    tool_name: str | None
    interrupted_at: datetime
```

### 7. API 层

在 `app/api/sessions.py` 新增路由：

```python
@router.post("/{session_id}/interrupt", response_model=APIResponse[SessionInterruptResultDTO])
async def interrupt_session(
    session_id: str,
    _: str = Depends(verify_local_token),
    request_id: str | None = Depends(get_request_id),
    session_interrupt_service: SessionInterruptService = Depends(get_session_interrupt_service),
):
    result = await session_interrupt_service.interrupt(session_id)
    return APIResponse(data=result, request_id=request_id)
```

并在 `app/api/deps.py` 添加 `get_session_interrupt_service`。

## 第一期实现范围

1. `app/core/job_context.py`：新增 `interruptible_phase` / `active_tool_name` context var。
2. `app/services/orchestration/agent_execution_service.py`：在事件流中设置阶段与工具名。
3. `app/schemas/public_v2/session.py`：新增 `SessionInterruptResultDTO`。
4. `app/schemas/event/__init__.py`：新增 `SessionInterruptedEvent` / `SessionInterruptedPayload`。
5. `app/services/business/message_service.py`：新增 `append_system_reminder_to_last_message`。
6. `app/services/business/session_interrupt_service.py`：新增打断服务。
7. `app/api/deps.py`：新增依赖注入。
8. `app/api/sessions.py`：新增 `POST /{session_id}/interrupt`。
9. `tests/e2e/test_session_interrupt.py`：两个 E2E 测试：
   - 在 assistant text 阶段打断，验证 `<system_reminder>` 追加到最后一条 assistant 消息末尾。
   - 在 tool_call 阶段打断，验证 `<system_reminder>` 追加到最后一条 tool 消息末尾。

## 与现有机制的边界

- `SystemReminderMiddleware` 仍保留，用于处理其他场景（如任务完成、后台 interrupt）。
- 本设计不修改 `SystemReminderMiddleware` 的注入逻辑；它只处理“运行中通过 context 触发”的场景。
- 用户主动调 `interrupt` 时，由 `SessionInterruptService` 直接写历史，`SystemReminderMiddleware` 不需要再为同一次打断注入。
- `JobService.control(action=cancel)` 行为不变；`SessionInterruptService` 内部调用它。

## 测试策略

- 单元测试：
  - `append_system_reminder_to_last_message` 对各种 role 列表的修改。
  - context var 设置/重置。
- E2E 测试：
  - text 阶段打断：发送一条会流式输出长文本的 prompt，在 `TEXT_START` 后调用 interrupt，检查 messages.jsonl 中最后一条 assistant 消息包含 `<system_reminder>`。
  - tool 阶段打断：发送一条会触发 tool call 的 prompt，在 `TOOL_CALL_START` 后调用 interrupt，检查 messages.jsonl 中最后一条 tool 消息包含 `<system_reminder>`。
