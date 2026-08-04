# Codex 兼容的终端工具与持续执行管理

> **决策状态**：拟定  
> **适用范围**：Agent 终端工具、持久 PTY、Workspace Terminal Manager、后台命令通知、Session 终端资源  
> **核心约束**：`exec_command` 与 `write_stdin` 保持 Codex 的模型可见契约；增强能力通过可选扩展字段或新增工具提供

---

## 1. 背景

当前项目已经具备独立的 Terminal Manager、持久 PTY、终端状态文件、WebSocket attach、Session Resource API，以及模型可调用的 `exec_command` 和 `write_stdin`。但是，当前实现仍存在以下问题：

1. `exec_command` 完成后，承载命令的交互 shell 可能继续运行，但模型不再获得 `session_id`。
2. `write_stdin` 以调用开始时的完整 buffer 为差量基准，可能跳过两次工具调用之间已经产生的输出。
3. 后台命令完成、异常退出或等待输入时，不会主动恢复 Agent 执行。
4. 模型无法列举或主动终止自己创建的持续终端。
5. Terminal Manager 缺少有界的活动执行管理。
6. `chunk_id`、空轮询等待范围、输出计数等细节与 Codex 契约仍有偏差。

本项目使用的模型主要通过 Codex 工具样本训练。因此，不能为了模仿 VS Code 的终端体验而改变这两个基础工具的默认语义。本文档决定：

> 以 Codex 的 `exec_command` / `write_stdin` 为兼容基线，修复当前实现偏差；终端发现、显式终止和事件驱动续跑作为额外扩展提供。

---

## 2. 决策目标

本设计需要同时满足以下目标：

1. 保持模型已经学习到的 Codex 工具名称、参数、默认值和调用循环。
2. 保证后台命令输出不会因为模型调用间隔而丢失。
3. 命令结束后及时释放隐藏 PTY 和进程树，不遗留不可达 shell。
4. 允许模型恢复遗忘的终端 ID，并主动终止不再需要的终端。
5. 后台命令完成后能够自动触发 Agent 继续原任务。
6. 每个 `workspace_id` 最多管理 64 个活动终端执行，防止一个工作区无限占用资源。
7. 终端状态以后端为唯一权威来源；前端只展示后端状态镜像。
8. 所有失败显式暴露，不返回虚假完成状态，不静默丢弃关键输出或终端事件。

---

## 3. 非目标

本文档明确不处理以下事项：

1. **不要求对齐 Codex 的 `tty=false` stdin 限制。** 当前实现可以继续在底层统一分配 PTY，并允许现有交互方式；后续如需收紧，另行制定决策。
2. 不把 `exec_command` 改成 VS Code 风格的默认无限同步等待。
3. 不让 `exec_command` 默认复用上一次调用的 shell、cwd 或环境变量。
4. 不用新的 `get_terminal_output` 替代 Codex 已训练的空 `write_stdin` 轮询。
5. 不把 Terminal Manager 的 PTY、进程树清理逻辑迁入 FastAPI 业务模块。
6. 不在 Gateway 中实现 Agent 终端业务规则。
7. 不保证 Terminal Manager 自身重启后继续附着到原 PTY；本阶段保持诚实清理并准确报告终止状态。

---

## 4. 术语

### 4.1 Workspace

一个由 Gateway 注册并路由到独立工作区后端的工作区。本文中的资源上限按稳定的 `workspace_id` 计算，不能按 Session、Agent、进程 PID、端口或当前激活工作区计算。

### 4.2 Terminal

Terminal Manager 管理的 PTY 容器，具有 `terminal_id`、shell 进程、输出 buffer、attach 客户端和进程树元数据。

### 4.3 Terminal Execution

一次 `exec_command` 产生的受管命令执行。它与一个 Terminal 一一关联，但二者语义不同：

- Terminal 是 PTY 资源。
- Terminal Execution 是模型可继续轮询和交互的命令生命周期。

本阶段不复用 Terminal，因此一次 `exec_command` 对应一个新 Terminal 和一个新 Terminal Execution。

### 4.4 `session_id`

`exec_command` 返回给模型、供 `write_stdin` 使用的不透明执行标识。为对齐模型训练，模型可见字段继续命名为 `session_id`。

当前实现可以继续让它与内部 `terminal_id` 使用同一字符串值，但业务代码必须区分两种语义，不能再把 `chunk_id` 当成终端 ID。

### 4.5 `chunk_id`

单次工具响应的输出块 ID。它只标识一次 `exec_command` 或 `write_stdin` 响应，不标识持续终端，也不能传给 `write_stdin`。

### 4.6 活动执行

状态为以下任一值的 Terminal Execution：

- `starting`
- `running`
- `waiting_input`
- `terminating`

`completed`、`failed`、`exited`、`terminated`、`deleted` 记录属于历史状态，不计入 64 个活动执行上限。

---

## 5. 模型可见工具契约

### 5.1 `exec_command`

基础工具名称保持：

```text
exec_command
```

参数保持现状和 Codex 兼容形态：

```python
exec_command(
    cmd: str,
    workdir: str | None = None,
    tty: bool = False,
    yield_time_ms: int = 10_000,
    max_output_tokens: int | None = None,
    shell: str | None = None,
    login: bool = True,
)
```

#### 参数语义

| 参数 | 决策 |
|---|---|
| `cmd` | 必填；拒绝空白命令 |
| `workdir` | 可选；默认工作区根目录 |
| `tty` | 保留现有参数和行为；本文档不处理 `tty=false` 差异 |
| `yield_time_ms` | 默认 10000 ms；有效范围 250～30000 ms |
| `max_output_tokens` | 默认 10000；只限制本次返回给模型的输出，不限制后台进程运行 |
| `shell` | 可选 shell 路径；不得为空字符串或包含空字符 |
| `login` | 保持当前默认值和命令派生行为 |

#### 执行语义

1. 每次调用创建独立 Terminal Execution。
2. 命令在 `yield_time_ms` 内完成时，返回最终输出和 `exit_code`，不返回 `session_id`。
3. 命令在等待窗口结束时仍在运行，返回当前输出和 `session_id`。
4. 返回 `session_id` 前，必须先把 execution 写入受管注册表，确保当前 Agent turn 被取消时不会丢失后台进程控制权。
5. 命令完成且最终输出已捕获后，自动释放隐藏 Terminal 和进程树。
6. 终端创建、命令写入、状态解析或进程清理失败时必须抛出详细错误。

#### 完成响应

```json
{
  "chunk_id": "a1b2c3",
  "wall_time_seconds": 1.42,
  "exit_code": 0,
  "original_token_count": 12,
  "output": "tests passed"
}
```

#### 持续执行响应

```json
{
  "chunk_id": "d4e5f6",
  "wall_time_seconds": 10.01,
  "session_id": "term_01...",
  "terminal_id": "term_01...",
  "original_token_count": 8,
  "output": "server starting"
}
```

其中 `terminal_id` 是 BoxTeam 扩展字段；模型继续使用 `session_id` 调用 `write_stdin`。

### 5.2 `write_stdin`

基础工具名称保持：

```text
write_stdin
```

参数保持：

```python
write_stdin(
    session_id: str,
    chars: str = "",
    yield_time_ms: int = 250,
    max_output_tokens: int | None = None,
)
```

#### 参数语义

| 参数 | 决策 |
|---|---|
| `session_id` | `exec_command` 返回的不透明执行 ID；必须属于当前 Agent Session |
| `chars` | 原始字符；空字符串表示只等待和读取输出 |
| `yield_time_ms` | 非空写入有效范围 250～30000 ms；空轮询有效范围 5000～300000 ms |
| `max_output_tokens` | 默认 10000；仅限制本次返回文本 |

参数默认值仍为 250 ms。空轮询的 5000 ms 最小值属于内部有效值规范，不改变模型 schema。

#### 执行语义

1. 校验 execution 属于当前 Session，禁止跨 Session 访问。
2. `chars` 非空时先写入，再等待新输出或完成状态。
3. `chars` 为空时只等待新输出或完成状态。
4. 返回自上一次模型消费之后的全部未消费输出，而不是只返回本次 HTTP 调用期间产生的输出。
5. execution 仍在运行时继续返回相同 `session_id`。
6. execution 已完成时返回最终未消费输出和 `exit_code`，不再返回 `session_id`，随后释放 execution。
7. execution 不存在、已被 LRU 淘汰或已删除时，返回明确的 unknown/stale session 错误。

#### 运行中响应

```json
{
  "chunk_id": "112233",
  "wall_time_seconds": 5.0,
  "session_id": "term_01...",
  "terminal_id": "term_01...",
  "original_token_count": 3,
  "output": "ready"
}
```

#### 完成响应

```json
{
  "chunk_id": "445566",
  "wall_time_seconds": 0.2,
  "exit_code": 1,
  "original_token_count": 20,
  "output": "final error output"
}
```

### 5.3 输出字段规范

| 字段 | 语义 |
|---|---|
| `chunk_id` | 每次响应新生成的短 ID |
| `wall_time_seconds` | 本次工具调用等待输出的墙钟时间 |
| `session_id` | 仅当 execution 仍可继续交互时返回 |
| `terminal_id` | BoxTeam 扩展资源 ID；可在完成响应中保留用于历史关联 |
| `exit_code` | 本次调用观察到 execution 完成时返回 |
| `original_token_count` | 截断前输出 token 数；无论是否截断都返回 |
| `output` | 本次消费的输出，按 `max_output_tokens` 截断 |

输出截断在原始输出完成消费后执行。模型游标推进到本次已从 execution buffer 取出的最后一个 sequence；被返回长度限制省略的内容使用明确 marker 表示，不得在后续轮询中重复返回，也不得改变命令状态。

---

## 6. 新增模型工具

新增工具不能改变 `exec_command` 和 `write_stdin` 的默认调用方式。

### 6.1 `list_terminal_sessions`

用途：让模型在上下文压缩、长任务切换或后台通知后恢复当前工作区内属于本 Session 的活动 execution。

建议 schema：

```python
list_terminal_sessions(
    include_completed: bool = False,
    limit: int = 64,
) -> dict
```

默认只返回当前 Agent Session 的活动 execution：

```json
{
  "workspace_id": "ws_01...",
  "terminals": [
    {
      "session_id": "term_01...",
      "terminal_id": "term_01...",
      "command": "bun run dev",
      "cwd": "/workspace",
      "status": "running",
      "created_at": "...",
      "last_used_at": "...",
      "has_unread_output": true
    }
  ]
}
```

规则：

1. 禁止通过参数指定其他业务 Session。
2. 不返回其他 Session 或其他工作区的 execution。
3. `include_completed=true` 只返回有界的近期历史，不返回无限记录。
4. 列表结果不包含完整历史输出；模型通过空 `write_stdin` 消费未读输出。

### 6.2 `kill_terminal`

用途：终止一个仍在运行的 execution，并返回终止前尚未消费的输出。

建议 schema：

```python
kill_terminal(
    session_id: str,
) -> dict
```

响应示例：

```json
{
  "session_id": "term_01...",
  "terminal_id": "term_01...",
  "status": "terminated",
  "release_reason": "model_requested",
  "original_token_count": 7,
  "output": "final buffered output"
}
```

规则：

1. 只能终止当前 Agent Session 所拥有的 execution。
2. 必须终止整个受管进程树，而不是只关闭 PTY reader。
3. 重复终止已经结束的 execution 返回明确的当前状态，不伪造第二次成功。
4. 通过该工具终止后，不再额外生成重复的“终端异常退出” steering。

---

## 7. 可靠输出消费

### 7.1 问题定义

不能再使用“工具调用开始时的完整 buffer”作为差量起点，因为命令可能在两次工具调用之间产生输出：

```text
exec_command 返回
    ↓
后台产生输出 A
    ↓
write_stdin 开始并读取当前 buffer
    ↓
如果以当前 buffer 为 previous_buffer，A 永远不会返回给模型
```

### 7.2 序号模型

Terminal Manager 已经为输出事件维护递增 `sequence`。每个 Terminal Execution 需要额外维护：

```text
command_start_sequence
model_consumed_sequence
latest_sequence
```

含义：

- `command_start_sequence`：本次命令开始前最后一个输出序号。
- `model_consumed_sequence`：已经返回给模型的最后一个输出序号。
- `latest_sequence`：Terminal Manager 当前已经产生的最后一个序号。

### 7.3 消费规则

1. `exec_command` 返回 `(command_start_sequence, returned_sequence]` 范围内的输出。
2. 返回成功后，把 `model_consumed_sequence` 推进到 `returned_sequence`。
3. 后续 `write_stdin` 返回 `(model_consumed_sequence, returned_sequence]`。
4. UI WebSocket attach 使用自己的客户端 ACK，不得推进模型游标。
5. 模型读取不得影响用户 attach 后查看完整终端 buffer。
6. 输出 buffer 截断时必须保留明确的 omitted marker，不能静默缺失。
7. execution 完成后，必须先让最终输出进入序列，再发布完成事件。

### 7.4 并发规则

同一个 execution 同一时间只允许一个模型消费操作：

- 两个并发 `write_stdin` 必须串行化。
- `kill_terminal` 与 `write_stdin` 必须通过同一 execution lock 协调。
- 状态读取、输出消费和游标推进必须形成原子操作。
- UI attach、resize 可以并发，但不能修改模型消费游标。

---

## 8. Terminal Execution 状态机

```text
allocated
    ↓
starting
    ↓
running ───────────────→ waiting_input
    │                         │
    │                         └────→ running
    │
    ├────→ completed
    ├────→ failed
    ├────→ exited
    └────→ terminating ─────→ terminated

任一终态 ─────→ deleted
```

### 状态定义

| 状态 | 含义 |
|---|---|
| `allocated` | ID 已保留，尚未启动 PTY |
| `starting` | PTY/进程正在启动 |
| `running` | 命令仍在执行 |
| `waiting_input` | 高置信度判断命令在等待输入 |
| `terminating` | 已开始终止进程树 |
| `completed` | 命令正常完成，包括非零 exit code |
| `failed` | Terminal Manager 或执行基础设施失败 |
| `exited` | PTY 或 shell 在命令完成 marker 前退出 |
| `terminated` | 用户、模型、LRU、Session 清理或管理器关闭导致终止 |
| `deleted` | 运行资源和活动索引已删除，只保留必要历史 |

非零 exit code 属于 `completed`，不是基础设施 `failed`。

---

## 9. 每工作区 64 个活动执行上限

### 9.1 限制单位

限制键必须是：

```text
workspace_id
```

不是：

- `session_id`
- `agent_id`
- 当前激活工作区布尔值
- Terminal Manager PID
- 后端端口
- 工作区路径字符串的临时别名

同一 `workspace_id` 下所有根 Session、子 Session、团队成员和 Agent 共享同一个 64 执行上限。不同 `workspace_id` 分别计算。

### 9.2 计数对象

只统计活动 execution：

```text
starting + running + waiting_input + terminating
```

以下内容不计入上限：

- 已完成、失败、退出、终止或删除的 execution 历史；
- 仅用于 UI 展示的历史资源 DTO；
- tool call trace；
- 已落盘的输出 artifact；
- 用户在工作区外部自行创建的普通系统终端。

### 9.3 分配与淘汰策略

创建新 execution 时，在 `workspace_id` 对应的注册表锁内执行：

1. 清理已经进入终态但尚未从活动索引移除的 entry。
2. 如果活动数小于 64，保留新 ID 并继续启动。
3. 如果活动数等于 64，按 `last_used_at` 从新到旧保护最近 8 个 execution。
4. 在未保护 execution 中选择最久未使用的 execution。
5. 将被淘汰 execution 标记为 `terminating`，设置：

```text
release_reason = workspace_lru_eviction
```

6. 终止其完整进程树，发布终止事件，再允许新 execution 进入活动注册表。
7. 如果终止失败或进程树仍存活，新 execution 创建失败；不得假装已经释放容量。

`last_used_at` 在以下操作成功开始时更新：

- `exec_command` 初始等待；
- `write_stdin` 写入或轮询；
- `kill_terminal`；
- 用户通过终端资源面板 attach 或发送输入。

普通后台输出到达不更新 `last_used_at`，否则一个无人消费但持续刷日志的进程会永久规避 LRU。

### 9.4 并发保证

上限检查、ID 保留和 LRU 候选选择必须以 `workspace_id` 为粒度原子执行，防止多个 Session 并发创建时共同越过 64 上限。

可以为不同 `workspace_id` 使用独立锁，避免一个工作区的慢清理阻塞其他工作区。

### 9.5 淘汰可见性

LRU 淘汰不能静默发生：

1. Terminal Manager 状态记录必须保存 `release_reason`。
2. Session Resource API 必须展示 execution 已被工作区资源上限终止。
3. 如果所属 Session 仍存在，向该 Session 投递一次终止 steering。
4. 后续使用旧 `session_id` 调用 `write_stdin` 时，错误中必须包含 `workspace_lru_eviction`。

---

## 10. 后台事件与 Agent 自动续跑

### 10.1 原则

后台命令不能要求模型通过 sleep 或高频空轮询才能完成任务。Terminal Manager 应发布原始生命周期事件，`app/` 负责把业务相关事件转换为 Session steering Job。

Terminal Manager 不直接调用 LLM，也不直接创建 Session 消息。

### 10.2 原始终端事件

至少支持：

```text
execution_started
output_available
input_required
execution_completed
execution_failed
terminal_exited
execution_terminated
```

公共字段：

```json
{
  "event_id": "tev_01...",
  "workspace_id": "ws_01...",
  "owner_session_id": "ses_01...",
  "owner_agent_id": "default",
  "terminal_id": "term_01...",
  "execution_id": "term_01...",
  "sequence": 42,
  "timestamp": "..."
}
```

事件必须先持久化状态，再通知订阅者，保证消费者读取到的快照不早于事件。

### 10.3 Steering 触发条件

以下事件可以触发 Agent steering：

- 后台 execution 完成；
- 后台 execution 基础设施失败；
- PTY 在命令完成前异常退出；
- 高置信度检测到非敏感输入请求；
- execution 被工作区 LRU 淘汰。

以下情况不生成重复 steering：

- 模型已经通过 `write_stdin` 同步观察到同一完成事件；
- `kill_terminal` 正常返回了终止结果；
- 用户在资源面板主动取消或删除，并已有独立 system reminder；
- Session 已删除；
- 用户明确取消当前 Agent 且终端事件策略要求停止自动续跑；
- 同一 `event_id` 已经派发。

### 10.4 Steering 内容

完成通知应使用模型熟悉的 Codex 表述：

```text
系统通知：后台 exec_command session_id=term_01... 已完成，exit_code=1。

以下是自上次 exec_command/write_stdin 后尚未消费的输出：
...

请继续完成原任务；不要再次轮询已经完成的 session_id。
```

通知必须携带 execution ID、命令、cwd、exit code、未消费输出和 release reason。通知只提供继续决策所需的有界输出，完整输出通过 artifact 或 Session Resource 查看。

### 10.5 调度方式

`app/` 使用现有 Session Orchestrator 创建内部消息，并指定：

```python
dispatch_mode="steering"
```

行为：

- Session 空闲时立即启动新的 Agent Job。
- Session 正在运行时排到 steering 队列前部，并在安全工具边界请求当前 Job yield。
- 多个相邻终端事件可以合并为一个 steering Job，但每个事件必须保留独立 `event_id` 和 execution 状态。
- steering 派发意图必须持久化并幂等，后端重启后不能重复通知或永久漏通知。

### 10.6 输入请求

输入检测只在高置信度时发布 `input_required`。普通 shell prompt、持续日志静默或单纯无输出不能直接判定为等待输入。

敏感输入规则：

- 密码、passphrase、token、API key、OTP 等不得进入模型上下文。
- 检测到敏感输入时，只通知用户在 attach 终端中直接输入。
- 不允许 Agent 通过新增工具或 `write_stdin` 猜测敏感值。

本文档规定事件边界，不要求第一阶段完成复杂的输入识别器。

---

## 11. 组件职责

### 11.1 `src/terminal/`

负责：

- 创建和管理 PTY；
- 进程树终止；
- 输出序号与有界 buffer；
- Terminal Execution 原始状态；
- Workspace 活动 execution 注册表；
- 每 `workspace_id` 64 上限和 LRU；
- 原始终端生命周期事件；
- HTTP/WebSocket attach、input、resize、kill、delete。

不负责：

- 创建 Agent Job；
- 生成 LLM 提示；
- 决定 Session 是否应该自动续跑；
- 读取或写入其他 Session 业务数据。

### 11.2 `app/`

负责：

- 构造模型工具；
- 校验 execution 所属 Session、Agent 和 workspace；
- 把 Terminal Manager DTO 映射为模型工具结果；
- 订阅终端事件并创建幂等 steering Job；
- Session Resource 查询和控制；
- tool result、trace、artifact 和业务历史持久化；
- Session 删除时请求清理相关 execution。

### 11.3 Gateway

只负责工作区注册、目标选择、生命周期和透明代理。Gateway 不实现终端模型工具语义，不读取工作区 `.boxteam/` 终端业务状态。

### 11.4 前端

前端只保存展示态和后端终端状态镜像：

- 列出终端资源；
- attach/detach；
- 用户输入和 resize；
- cancel/delete；
- 展示完成、失败、LRU 淘汰和敏感输入提示。

所有状态变更必须调用后端或 Terminal Manager API，并用完整返回对象替换本地资源状态。

---

## 12. 数据模型

Terminal Execution 的最小权威字段：

```json
{
  "workspace_id": "ws_01...",
  "owner_session_id": "ses_01...",
  "owner_agent_id": "default",
  "terminal_id": "term_01...",
  "execution_id": "term_01...",
  "command": "bun run dev",
  "cwd": "/workspace",
  "status": "running",
  "exit_code": null,
  "created_at": "...",
  "started_at": "...",
  "updated_at": "...",
  "last_used_at": "...",
  "ended_at": null,
  "release_reason": null,
  "command_start_sequence": 4,
  "model_consumed_sequence": 12,
  "latest_sequence": 18,
  "completion_event_id": null,
  "completion_observed_by_model": false,
  "steering_dispatched": false
}
```

要求：

1. `workspace_id`、owner 字段和 ID 创建后不可变。
2. 显示标题不得参与路径或身份判断。
3. `model_consumed_sequence` 只能单调增加。
4. 终态不能回到活动状态。
5. `release_reason` 必须区分模型终止、用户终止、Session 清理、LRU、管理器关闭和异常退出。

---

## 13. 存储边界

### Workspace Terminal Manager 运行状态

存储于当前工作区：

```text
${workspace_abs_path}/.boxteam/terminal-manager/
```

这里可以保存：

- PTY/进程身份元数据；
- 活动 execution 注册表；
- 输出序号和有界恢复 buffer；
- 未消费终端事件；
- Workspace LRU 元数据。

### Session 业务历史

以下内容必须聚合到对应 Session 节点目录：

- 模型 tool call/result；
- Agent trace；
- steering 消息和 Job；
- 完整或截断输出 artifact；
- 用户可见的历史资源记录。

Terminal Manager 的 Workspace 运行状态不能成为 Session 对话历史的第二权威来源。Session 历史也不能被用来推断一个进程当前仍然存活。

---

## 14. Terminal Manager API 要求

在现有 API 基础上，Terminal Manager 至少需要支持以下能力：

```text
POST   /api/terminals
GET    /api/terminals/{terminal_id}
GET    /api/terminals?workspace_id=...&session_id=...
POST   /api/terminals/{terminal_id}/write
POST   /api/terminals/{terminal_id}/read
POST   /api/terminals/{terminal_id}/kill
DELETE /api/terminals/{terminal_id}
GET    /api/terminal-events/stream
```

`read` 请求需要支持模型消费边界：

```json
{
  "consumer": "model",
  "after_sequence": 12,
  "wait_ms": 5000
}
```

返回：

```json
{
  "terminal": {},
  "from_sequence": 13,
  "to_sequence": 18,
  "output": "...",
  "omitted_bytes": 0
}
```

Python `TerminalManagerClient` 不再直接读取状态文件作为活动状态查询主路径。活动查询、上限判断和控制操作必须通过 Terminal Manager API；状态文件只用于 Terminal Manager 自身恢复和只读诊断。

---

## 15. 重启与恢复

### 15.1 Workspace 后端重启

Terminal Manager 仍运行时：

- 后端重新订阅终端事件；
- 根据持久化 execution 状态恢复 Session 关联；
- 补发尚未确认的完成/失败事件；
- 不杀死仍在运行的 PTY。

### 15.2 Terminal Manager 正常关闭

必须：

1. 终止所有活动 execution 的进程树；
2. 写入终态和 `terminal_manager_shutdown` release reason；
3. 持久化最终输出与事件；
4. 不留下运行中假状态。

### 15.3 Terminal Manager 异常退出后重启

本阶段保持现有诚实清理原则：

1. 校验 PID、process session 和 start time，防止误杀复用 PID。
2. 清理上次记录的进程树。
3. 将 execution 标记为 `terminated` 或 `exited`。
4. `release_reason` 记录为 `terminal_manager_startup_cleanup`。
5. 生成可由后端 reconciliation 消费的持久终端事件。
6. 后端恢复后为仍存在的 Session 派发一次明确 steering。

不得把已经被启动清理终止的 execution 恢复成 `running`。

---

## 16. 错误与失败语义

以下情况必须显式失败：

- `workspace_id` 缺失或与 Terminal Manager 当前工作区不一致；
- `session_id` 不属于当前 Agent Session；
- 输出 sequence 回退、越界或状态文件损坏；
- 进程树终止后仍存活；
- 达到上限但 LRU 清理失败；
- completion marker 与 execution 身份不一致；
- Terminal Manager API 返回缺少关键字段的 DTO；
- steering 事件持久化或派发失败。

禁止：

- 返回空输出掩盖 read 失败；
- 把未知 exit code 伪造为 0；
- 把终端不存在解释为已成功完成；
- 因订阅队列满而静默丢弃完成事件；
- 只从 Map 删除 execution 而不终止其进程树；
- 在 LRU 淘汰失败后继续创建第 65 个活动 execution。

---

## 17. 可观测性

每个终端事件和工具 trace 至少关联：

```text
request_id
workspace_id
owner_session_id
owner_agent_id
job_id
tool_call_id
terminal_id
execution_id
event_id
```

建议指标：

- 每工作区活动 execution 数；
- execution 创建、完成、失败、终止数量；
- Workspace LRU 淘汰数量；
- 输出 omitted bytes；
- 后台完成到 steering Job 创建的延迟；
- 重复终端事件抑制数量；
- Terminal Manager 重启清理数量；
- 进程树强制 SIGKILL 数量；
- unknown/stale `session_id` 调用数量。

日志必须包含详细错误，不得输出敏感输入正文。

---

## 18. 测试要求

### 18.1 单元测试

覆盖：

1. `exec_command` 和 `write_stdin` schema、参数默认值保持兼容。
2. 命令 10 秒窗口内完成时不返回 `session_id`。
3. 命令仍运行时返回 `session_id`。
4. `chunk_id` 每次响应变化且不等于终端 ID。
5. 未截断输出也返回 `original_token_count`。
6. 空轮询和非空写入使用不同等待范围。
7. 调用间输出不会丢失。
8. UI ACK 不推进模型输出游标。
9. 完成后先返回最终输出，再释放 execution。
10. Session 所有权校验。
11. `list_terminal_sessions` 只返回当前 Session。
12. `kill_terminal` 终止进程树并抑制重复 steering。
13. 同一 execution 并发读取串行化。
14. 同一 workspace 并发创建不会超过 64。
15. 不同 workspace 各自可以拥有最多 64 个活动 execution。
16. 最近 8 个 execution 受到 LRU 保护。
17. 后台持续输出不会刷新 `last_used_at`。

### 18.2 Terminal Manager 集成测试

覆盖：

- marker 完成与 exit code；
- 输出 sequence 连续性；
- read-since 和 buffer 截断；
- attach/detach 与模型游标隔离；
- kill/delete 并发；
- LRU 终止完整进程树；
- manager shutdown 和 startup cleanup；
- PID 复用保护；
- 终态事件先持久化后发布；
- 状态文件损坏时明确启动失败。

### 18.3 Agent E2E

至少包含：

1. 长测试超过初始等待窗口后完成，Agent 被 steering 唤醒并继续修复。
2. 输出在两次工具调用之间产生，模型仍能完整读到。
3. 模型通过列表工具恢复遗忘的 `session_id`。
4. 模型启动开发服务器、完成验证后调用 `kill_terminal` 清理。
5. 第 65 个 execution 触发当前 workspace 的 LRU，但不影响其他 workspace。
6. LRU 被淘汰的 Session 收到明确通知。
7. 后端重启后仍能收到 Terminal Manager 已持久化的完成事件。
8. Terminal Manager 重启清理后，Agent 不会继续轮询虚假的运行状态。
9. 用户 attach 不影响 Agent 获取未消费输出。
10. Session 删除后相关 execution 全部清理且不会再次 steering。

正式测试产物必须按仓库测试目录规范写入对应 `out/tests/...`，不得写入项目根目录。

---

## 19. 迁移顺序

### 阶段一：Codex 契约修复

1. 分离 `chunk_id` 与 `terminal_id`。
2. 始终返回 `original_token_count`。
3. 区分空轮询与非空写入的有效等待范围。
4. 命令完成后释放隐藏 Terminal。

### 阶段二：可靠输出

1. 为输出事件提供稳定 sequence 查询。
2. 增加 execution 的模型消费游标。
3. `write_stdin` 改为读取所有未消费输出。
4. 增加并发锁和完成后最终 drain。

### 阶段三：Workspace 有界管理

1. 引入稳定 `workspace_id` 字段。
2. 建立每 workspace 活动 execution 注册表和锁。
3. 实现 64 上限、最近 8 个保护和 LRU 终止。
4. 增加淘汰原因、指标和 Session 通知。

### 阶段四：模型扩展工具

1. 新增 `list_terminal_sessions`。
2. 新增 `kill_terminal`。
3. 更新工具策略、描述和 E2E。

### 阶段五：事件驱动自动续跑

1. 增加持久终端事件流。
2. 增加 app 侧订阅和幂等 reconciliation。
3. 接入 Session steering Job。
4. 增加完成、异常退出和 LRU 通知。
5. 后续按独立设计完善输入请求检测。

迁移期间旧工具结果中 `chunk_id == terminal_id` 的记录继续由历史读取器识别；新记录不得继续依赖该兼容行为。兼容代码上方必须保留 TODO，并在旧记录迁移完成后删除。

---

## 20. 验收标准

本设计完成的最低验收条件：

1. 模型仍可使用原有 Codex 调用模式，不需要学习新的必填参数。
2. `exec_command` 默认 10 秒、`write_stdin` 默认 250 ms 保持不变。
3. 两次工具调用之间产生的输出不会丢失。
4. 已完成命令不留下模型不可达的运行 shell。
5. 每个 `workspace_id` 的活动 execution 永远不超过 64。
6. 一个工作区达到上限不会占用或淘汰其他工作区的 execution。
7. 模型可以列出并终止自己 Session 的活动终端。
8. 后台命令完成后可以自动触发 Agent 继续处理结果。
9. LRU、异常退出、重启清理和用户终止均有明确状态与原因。
10. Session Resource、模型工具结果、Terminal Manager 状态和进程真实状态保持一致。

---

## 21. 最终决策

采用以下终端工具方向：

1. `exec_command` 和 `write_stdin` 继续作为 Codex 兼容基础工具。
2. 不改变两者的默认执行循环，不引入默认 shell 复用或默认无限等待。
3. 当前阶段不处理 `tty=false` 行为差异。
4. 内部通过 execution 注册表、输出 sequence 和模型消费游标保证可靠性。
5. 命令完成后自动释放隐藏终端。
6. 每个 `workspace_id` 最多允许 64 个活动 execution，并采用最近 8 个保护的 LRU 淘汰策略。
7. 新增 `list_terminal_sessions` 和 `kill_terminal` 作为模型扩展工具。
8. 借鉴 VS Code 的事件驱动方式，在后台完成、失败、异常退出和 LRU 淘汰后使用 steering Job 自动恢复 Agent。
9. Terminal Manager 负责 PTY 与 Workspace 资源边界，`app/` 负责 Agent 业务规则，Gateway 继续保持透明代理职责。
