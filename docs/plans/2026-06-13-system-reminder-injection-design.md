# 设计：基于 <system_reminder> 的 LLM 对话注入机制

## 目标

实现一个可扩展的系统提醒（`<system_reminder>...</system_reminder>`）注入机制：
- 在用户打断 tool_call / text_delta 时，在断点处注入上下文提醒。
- 在任务完成、后台 interrupt 等场景注入总结或状态提醒。
- 注入位置不固定，跟随 OpenAI completion message 的 role 流：哪里断了就加在对应消息后面。
- 标记语言使用 `<system_reminder>...</system_reminder>`，类似 Codex。

## 核心设计

### 1. 数据模型

```python
class SystemReminderPosition(str, Enum):
    AFTER_LAST_ASSISTANT = "after_last_assistant"      # 断在 assistant 生成过程中
    AFTER_TOOL_CALLS = "after_tool_calls"              # 工具调用刚结束
    AFTER_LAST_USER = "after_last_user"                # 紧跟用户新消息
    APPEND = "append"                                  # 放在列表末尾


class SystemReminder(BaseModel):
    content: str
    position: SystemReminderPosition
    priority: int = 0           # 同位置多条时按优先级排序
    dedup_key: str | None = None
```

### 2. 触发器框架

```python
class ReminderTriggerContext(BaseModel):
    session_id: str
    job_id: str
    agent_id: str
    messages: list[BaseMessage]          # 当前请求中的消息列表
    current_event_stream: list[dict] | None = None   # astream_events 当前事件
    last_turn_status: Literal["ok", "interrupted_tool", "interrupted_text", "completed"] = "ok"
    recent_tool_results: list[ToolResultSnapshot] = []


class SystemReminderTrigger(Protocol):
    async def produce(self, ctx: ReminderTriggerContext) -> list[SystemReminder]: ...
```

内置触发器：
- `InterruptReminderTrigger`：检测到上一 turn 被用户打断时，在 `after_last_assistant` 位置注入“用户打断了 XXX，请从该点继续/停止”。
- `TaskCompletionReminderTrigger`：在工具调用链结束后，于 `after_tool_calls` 注入任务完成摘要。
- `BackgroundMessageReminderTrigger`：将后台 `interrupt` 消息转成 `after_last_user` 的 reminder。
- `ContextWindowReminderTrigger`：消息过长时注入“请总结前文”提醒。

### 3. Middleware 注入

新增 `SystemReminderMiddleware`：

```python
class SystemReminderMiddleware(AgentMiddleware[StateT, Any, Any]):
    def __init__(
        self,
        *,
        trigger_registry: SystemReminderTriggerRegistry,
    ) -> None: ...

    async def awrap_model_call(self, request, handler):
        reminders = await self._collect_reminders(request)
        request.messages = self._inject_reminders(request.messages, reminders)
        return await handler(request)
```

注入算法：
1. 按 `position` 对 reminders 分组。
2. 遍历消息列表，找到最后一条 `assistant` 消息、`tool` 消息、`user` 消息。
3. 在对应断点后插入一个 `HumanMessage`，内容为：
   ```
   <system_reminder>
   {reminder.content}
   </system_reminder>
   ```
4. 若没有匹配位置，则 fallback 到列表末尾。

### 4. 注册与装配

- `app/container.py` 中创建 `SystemReminderTriggerRegistry`，注册默认触发器。
- `app/agents/agent_factory.py` 默认 middleware 列表追加 `SystemReminderMiddleware(...)`。
- 触发器可通过配置或代码动态增删。

## 第一期实现范围

1. `SystemReminder`、`SystemReminderPosition`、`ReminderTriggerContext` schema。
2. `SystemReminderTrigger` Protocol 与 `SystemReminderTriggerRegistry`。
3. `InterruptReminderTrigger`：基于 job 状态判断上一 turn 是否被打断。
4. `TaskCompletionReminderTrigger`：基于最近 tool_call_end 结果生成摘要。
5. `SystemReminderMiddleware` 实现注入逻辑。
6. 在 `AgentExecutionService` / `JobExecutionService` 中维护 `last_turn_status`，供触发器读取。
7. E2E 测试：模拟用户打断，验证 `<system_reminder>` 出现在正确位置。

## 与现有架构的边界

- 不替代 `LLMLoggingMiddleware`，二者可共存。
- 不修改 LangGraph checkpoint 机制；仅修改进入模型前的 `ModelRequest.messages`。
- `AgentExecutionService` 仍负责发布 trace 事件，新增系统提醒事件类型 `system_reminder_injected` 用于观测。

## 测试策略

- 单元测试：注入算法对各种消息列表的边界处理。
- 单元测试：各触发器对 `ReminderTriggerContext` 的响应。
- E2E 测试：真实请求后检查 persisted trace 中 `<system_reminder>` 内容。
