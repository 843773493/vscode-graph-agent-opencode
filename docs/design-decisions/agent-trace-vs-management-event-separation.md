# Agent 执行轨迹与管理事件分离设计决策

> **决策状态**：拟定
> 
> **适用范围**：会话消息发送、DeepAgent 执行、前端进度展示、轨迹回放

---

## 1. 背景

当前后端已经具备两类不同语义的数据：

1. **管理事件**
   - `message_created`
   - `job_created`
   - `status_change`
   - `job_started`
   - `job_completed`
   - `job_failed`

2. **执行轨迹**
   - `agent_start`
   - `llm_request`
   - `tool_call_start`
   - `tool_call_end`
   - `agent_end`

其中，管理事件用于描述任务生命周期和调度状态，执行轨迹用于描述 agent 实际执行过程。

前端当前更需要的是后者，因为用户看到的不是“后端调度怎么变了”，而是“agent 正在做什么、调用了什么工具、返回了什么结果”。

同时，仓库里已经存在轨迹持久化能力：

- [`app/agents/agent_middleware.py`](c:\Users\kunlunmeta\code\20260430_copilot_workspace\vscode-graph-agent-opencode\app\agents\agent_middleware.py) 的 `ExecutionTraceMiddleware` 会写入 `trace_{session_id}.jsonl`
- [`app/services/session_service.py`](c:\Users\kunlunmeta\code\20260430_copilot_workspace\vscode-graph-agent-opencode\app\services\session_service.py) 已经可以读取轨迹文件
- [`app/api/sessions.py`](c:\Users\kunlunmeta\code\20260430_copilot_workspace\vscode-graph-agent-opencode\app\api\sessions.py) 已经暴露了 `GET /sessions/{session_id}/traces`
- [`src/webview-ui/src/types/protocol.ts`](c:\Users\kunlunmeta\code\20260430_copilot_workspace\vscode-graph-agent-opencode\src\webview-ui\src\types\protocol.ts) 已经预留了 `traceEvents`

因此，本次设计不是重做一套新系统，而是把已有能力整理成两条清晰的数据通道。

---

## 2. 决策目标

本方案要同时满足以下目标：

1. 前端展示 agent 的执行进度，而不是后台管理状态
2. 轨迹数据既能实时推送，也能落盘回放
3. 管理事件继续保留，用于 job/session 状态机
4. 前端和后端的数据边界清晰，避免把调度细节暴露为主流程

---

## 3. 核心决策

### 3.1 数据通道拆分

将后端输出拆分为两条独立通道：

- **管理通道**：面向后端状态机、job 控制、会话状态同步
- **轨迹通道**：面向前端进度展示和历史回放

#### 管理通道保留内容

- `message_created`
- `job_created`
- `status_change`
- `job_started`
- `job_completed`
- `job_failed`
- `job_cancelled`

#### 轨迹通道输出内容

- `agent_start`
- `llm_request`
- `tool_call_start`
- `tool_call_end`
- `agent_end`
- `error`

---

### 3.2 前端默认消费轨迹，不消费管理事件

前端会话页的主时间线只展示执行轨迹。

管理事件仍然存在，但只用于：

- 会话状态栏
- job 运行状态
- 恢复页面后的状态同步
- 后端调度排队信息

也就是说：

- **主视图**：轨迹时间线
- **辅助视图**：状态栏 / job 状态 / 排队状态

---

### 3.3 轨迹数据以 JSONL 持久化

继续使用 JSONL 作为轨迹持久化格式，原因是：

- 追加写入简单
- 适合流式生成
- 适合增量读取
- 便于后续回放和调试
- 与现有 `trace_{session_id}.jsonl` 文件兼容

轨迹文件仍然存储在日志目录下，文件名保持按 session 区分。

---

### 3.4 新增轨迹 SSE 流

在现有的历史查询接口基础上，再提供一个轨迹 SSE 端点，让前端可以实时订阅 agent 过程。

建议新增：

- `GET /api/v1/sessions/{session_id}/traces/stream`

行为约定：

1. 连接后先补发历史轨迹
2. 然后持续推送新增轨迹
3. 断线后可重新拉取历史轨迹继续渲染

---

## 4. 轨迹事件模型

### 4.1 轨迹事件建议字段

建议新增一个面向前端的轨迹 DTO，例如 `TraceEventDTO`，字段如下：

- `event_id`
- `session_id`
- `job_id`
- `type`
- `phase`
- `title`
- `content`
- `status`
- `tool_name`
- `step_id`
- `timestamp`
- `raw`

### 4.2 推荐事件类型

```text
agent_start
llm_request
tool_call_start
tool_call_end
agent_end
error
```

### 4.3 推荐阶段字段

```text
agent
llm
tool
error
```

### 4.4 设计原则

- `type` 用于具体事件语义
- `phase` 用于前端视觉分组
- `title` 用于列表标题
- `content` 用于正文展示
- `raw` 用于调试和回放，不作为主展示依据

---

## 5. 后端实现方案

### 5.1 保留现有轨迹落盘点

继续使用 `ExecutionTraceMiddleware` 作为轨迹写入入口。

对应实现位置：

- [`app/agents/agent_middleware.py`](c:\Users\kunlunmeta\code\20260430_copilot_workspace\vscode-graph-agent-opencode\app\agents\agent_middleware.py)

这里负责把 agent 执行过程中产生的轨迹写入 `trace_{session_id}.jsonl`。

---

### 5.2 新增轨迹映射层

新增一个轨迹映射器，把底层 JSONL 事件转成前端友好的 DTO。

建议新增文件：

- `app/services/trace_event_mapper.py`

职责：

- 读取原始 JSONL 事件
- 过滤掉纯管理事件
- 将原始事件映射成 UI 轨迹事件
- 统一补齐 `phase`、`title`、`content`、`status`

---

### 5.3 扩展会话轨迹读取接口

现有接口：

- `GET /sessions/{session_id}/traces`

建议保持不变，但返回值升级为前端可直接消费的轨迹 DTO 列表。

对应实现位置：

- [`app/api/sessions.py`](c:\Users\kunlunmeta\code\20260430_copilot_workspace\vscode-graph-agent-opencode\app\api\sessions.py)
- [`app/services/session_service.py`](c:\Users\kunlunmeta\code\20260430_copilot_workspace\vscode-graph-agent-opencode\app\services\session_service.py)

---

### 5.4 新增轨迹 SSE 接口

建议新增：

- `GET /sessions/{session_id}/traces/stream`

实现方式有两种：

#### 方案 A：基于内存订阅

- 后端在写入轨迹时同时广播到 session 订阅者
- SSE 端点订阅 session 队列
- 实时性最好

#### 方案 B：基于文件轮询

- SSE 端点定时扫描 JSONL 新增行
- 实现简单
- 初期可以接受

推荐顺序：

1. 先做文件轮询版，快速打通前端展示
2. 再演进到内存订阅版

---

## 6. 前端实现方案

### 6.1 会话页主视图改为轨迹时间线

前端主界面展示：

- 当前 agent 在哪一步
- 正在调用哪个工具
- 工具调用是否成功
- 最终是否结束

不要把 `job_created`、`status_change` 当作主时间线内容展示。

---

### 6.2 状态栏与时间线分离

建议拆分成两个区域：

#### 状态栏

- session 是否忙碌
- 当前 job 是否运行
- 是否需要用户输入
- 是否失败

#### 时间线

- agent_start
- llm_request
- tool_call_start
- tool_call_end
- agent_end

---

### 6.3 前端协议复用 `traceEvents`

当前协议已经有：

- [`src/webview-ui/src/types/protocol.ts`](c:\Users\kunlunmeta\code\20260430_copilot_workspace\vscode-graph-agent-opencode\src\webview-ui\src\types\protocol.ts)

其中的 `traceEvents` 可以直接承接新的轨迹 DTO，无需另起一套主协议。

---

## 7. 推荐的字段映射

### 7.1 agent_start

- `phase`: `agent`
- `title`: `开始执行`
- `content`: `agent 启动，准备处理用户请求`

### 7.2 llm_request

- `phase`: `llm`
- `title`: `模型请求`
- `content`: `正在请求模型：{model}`

### 7.3 tool_call_start

- `phase`: `tool`
- `title`: `调用工具`
- `content`: `正在调用 {tool_name}`

### 7.4 tool_call_end

- `phase`: `tool`
- `title`: `工具返回`
- `content`: `工具 {tool_name} 已返回结果`

### 7.5 agent_end

- `phase`: `agent`
- `title`: `执行结束`
- `content`: `agent 已完成本轮处理`

### 7.6 error

- `phase`: `error`
- `title`: `执行失败`
- `content`: 错误摘要

---

## 8. 兼容策略

### 8.1 不破坏现有管理接口

以下接口保持现状：

- `POST /sessions/{session_id}/messages`
- `GET /jobs/{job_id}/events`
- `GET /jobs/{job_id}/events/stream`

原因：

- 这些接口对 job 状态机和内部调度仍然有用
- 不应为了前端展示直接删除

### 8.2 仅新增轨迹接口和轨迹 DTO

前端只需要换消费对象，不需要重构整个消息提交流程。

---

## 9. 实施顺序

### 第一阶段

- 保留现有 `trace_{session_id}.jsonl`
- 新增 `TraceEventDTO`
- 新增轨迹映射器
- 让 `GET /sessions/{session_id}/traces` 返回前端友好轨迹

### 第二阶段

- 新增 `GET /sessions/{session_id}/traces/stream`
- 前端接入 SSE
- 会话页改为轨迹时间线

### 第三阶段

- 优化轨迹粒度
- 增加 tool 输入输出摘要
- 增加可折叠步骤树和回放能力

---

## 10. 验收标准

当用户发送一次消息后，前端应该能看到：

- agent 开始执行
- 模型请求出现
- 工具调用开始/结束
- 执行结束
- 刷新后仍能从 JSONL 轨迹恢复出来

同时满足：

- 管理事件仍然存在
- 主时间线不以管理事件为主
- 前端不再主要展示 `message_created`、`job_created`、`status_change`

---

## 11. 结论

这个方案的核心不是“删除后台管理事件”，而是**把管理语义和展示语义分离**。

- 管理事件继续服务后端状态机
- 轨迹事件专门服务前端展示
- JSONL 保持为可靠的持久化轨迹源
- SSE 负责实时推送

这样前端看到的就是用户能理解的 agent 流程，而不是一堆后台调度细节。
