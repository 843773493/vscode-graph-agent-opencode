# API 接口命名与数据流规范

## 1. 概述

本规范定义 `app/schemas` 目录下所有 Pydantic Schema 类的命名规则，确保数据流向清晰、命名一致、易于理解。

**核心原则**：

- **客户端 → 服务器**：请求数据使用 `Request` 后缀
- **服务器 → 客户端**：响应数据使用 `DTO` 后缀
- **业务实体**：完整的数据对象使用 `{Entity}DTO` 模式
- **操作响应**：非CRUD操作的响应使用 `{Action}ResponseDTO` 模式

---

## 2. 数据流方向定义

### 2.1 客户端 → 服务器（入方向）

客户端发送给服务器的数据，用于创建、更新、控制等操作。

**命名规则**：`{Purpose}Request`

**常见场景**：

- 创建资源：`SessionCreateRequest`、`MessageCreateRequest`
- 更新资源：`SessionUpdateRequest`、`ConfigUpdateRequest`
- 执行操作：`JobControlRequest`、`ToolInvokeRequest`
- 发起请求：`MessageRunRequest`

### 2.2 服务器 → 客户端（出方向）

服务器返回给客户端的数据，包含查询结果、状态、确认等。

**命名规则**：`{Purpose}DTO`

**常见场景**：

- 查询结果：`JobDTO`、`SessionDTO`、`MessageDTO`
- 批量列表：通过 `CursorPage[T]` 包装
- 状态对象：`SessionAutoContinueStatusDTO`
- 操作响应：`JobControlResponseDTO`、`ConfigDTO`

---

## 3. 命名模式分类

### 3.1 业务实体 DTO

**模式**：`{Entity}DTO`

**说明**：代表系统中核心业务对象的完整数据表示。

**示例**：

```python
class JobDTO(BaseModel):          # 作业实体
class StepDTO(BaseModel):         # 步骤实体
class SessionDTO(BaseModel):      # 会话实体
class MessageDTO(BaseModel):      # 消息实体
class AgentDTO(BaseModel):        # Agent实体
class ConfigDTO(BaseModel):       # 配置实体
class ToolDTO(BaseModel):         # 工具实体
class ArtifactDTO(BaseModel):     # 产物实体
class WorkspaceDTO(BaseModel):    # 工作区实体
```

**特点**：

- 包含实体的所有关键字段（id、状态、时间戳等）
- 用于 GET 查询、列表返回、嵌套响应等场景
- 独立于特定API端点，是通用数据载体

---

### 3.2 创建/更新请求

**模式**：`{Entity}{Action}Request`

**说明**：客户端发起创建或更新操作的请求体。

**示例**：

```python
class SessionCreateRequest(BaseModel):    # 创建会话
class SessionUpdateRequest(BaseModel):    # 更新会话
class MessageCreateRequest(BaseModel):    # 创建消息（已存在 MessageCreate，需重命名）
```

**字段特点**：

- 使用 `Optional` 字段表示可更新属性
- 创建请求通常包含必填字段，更新请求字段多为可选
- 不包含系统生成的字段（如 `id`、`created_at`）

---

### 3.3 操作请求（非CRUD）

**模式**：`{Action}Request`

**说明**：执行特定业务操作的控制类请求。

**示例**：

```python
class JobControlRequest(BaseModel):        # 作业控制（暂停/恢复/取消等）
class ToolInvokeRequest(BaseModel):       # 工具调用
class SessionAutoContinueStartRequest(BaseModel):  # 启动自动继续
```

**特点**：

- 动词开头或包含动作关键词（`Control`、`Invoke`、`Start`、`Stop`）
- 参数与操作语义匹配
- 通常包含 `agent_id`、`step_id` 等目标标识

---

### 3.4 操作响应 DTO

**模式**：`{Action}ResponseDTO` 或 `{Action}ResultDTO`

**说明**：非CRUD操作的服务器响应数据。

**示例**：

```python
class JobControlResponseDTO(BaseModel):    # 控制操作结果（当前为 JobControlResponse，需修改）
# 可能的其他响应类型：
# class SessionResetResponseDTO(BaseModel):
# class JobRetryResponseDTO(BaseModel):
```

**特点**：

- 明确标注 `ResponseDTO` 以区分业务实体 DTO
- 返回操作结果、影响的对象、执行状态
- 可能包含 `job_id`、`status`、`control_state` 等结果字段

---

### 3.5 状态/配置 DTO

**模式**：`{Entity}{Status}DTO` 或 `{Config}DTO`

**说明**：表示特定状态集合或配置信息。

**示例**：

```python
class SessionAutoContinueStatusDTO(BaseModel):   # 会话自动继续状态
class ConfigDTO(BaseModel):                     # 系统配置
```

---

### 3.6 事件相关类（独立模式）

**说明**：事件系统使用独立的命名模式，不受 Request/DTO 约束。

**模式**：

```python
# 基类
class BaseEvent(BaseModel): ...

# Payload（纯数据载体，无后缀）
class MessageCreatedPayload(BaseModel): ...
class JobCreatedPayload(BaseModel): ...
class JobStartedPayload(BaseModel): ...
class AgentStartPayload(BaseModel): ...
class ErrorPayload(BaseModel): ...

# 具体事件（带type字面量）
class MessageCreatedEvent(BaseEvent): ...
class JobCreatedEvent(BaseEvent): ...
class AgentStartEvent(BaseEvent): ...
# 事件类型采用 PascalCase 动词过去式，type 字段为 snake_case

# Discriminated Union
Event = Union[MessageCreatedEvent, JobCreatedEvent, ...]
```

**命名逻辑**：

- `Payload` — 事件携带的数据，纯数据结构
- `Event` — 完整事件（含 `event_id`、`timestamp`、`type`、`payload`）
- `type` 字段使用 snake_case 字符串字面量（如 `"message_created"`）

---

## 4. 命名映射表（当前需调整项）

| 当前类名               | 文件       | 数据流向         | 建议修改为                | 理由                      |
| ---------------------- | ---------- | ---------------- | ------------------------- | ------------------------- |
| `JobControlResponse` | job.py     | 服务器 → 客户端 | `JobControlResponseDTO` | 操作响应，需 DTO 后缀     |
| `MessageCreate`      | message.py | 客户端 → 服务器 | `MessageCreateRequest`  | 创建请求，需 Request 后缀 |

---

## 5. API 端点命名参照

基于上述 Schema 命名，API 端点的 RESTful 设计建议：

### 5.1 资源端点

```python
# 会话
GET    /api/v1/sessions              → List[SessionDTO]
POST   /api/v1/sessions              → SessionCreateRequest → SessionDTO
GET    /api/v1/sessions/{id}         → SessionDTO
PATCH  /api/v1/sessions/{id}         → SessionUpdateRequest → SessionDTO
DELETE /api/v1/sessions/{id}         → None

# 消息
POST   /api/v1/messages              → MessageCreateRequest → MessageDTO
GET    /api/v1/messages/{id}         → MessageDTO

# 作业
GET    /api/v1/jobs/{id}             → JobDTO
POST   /api/v1/jobs/{id}/control     → JobControlRequest → JobControlResponseDTO
POST   /api/v1/jobs/{id}/retry       → JobRetryRequest → JobRetryResponseDTO（如需）
```

### 5.2 操作端点

```python
# 工具调用
POST   /api/v1/tools/{id}/invoke     → ToolInvokeRequest → ToolInvokeResponseDTO

# 自动继续
POST   /api/v1/sessions/{id}/auto-continue/start
    → SessionAutoContinueStartRequest → SessionAutoContinueStatusDTO
```

### 5.3 流式端点

```python
# 消息运行（SSE）
POST   /api/v1/messages/run
    → MessageRunRequest
    ← 202 Accepted + MessageRunAccepted（受理确认）
    ← SSE 流：JobDTO、MessageDTO 等状态更新
```

---

## 6. 命名一致性检查清单

创建新的 Schema 类时，请确认：

- [ ] 数据流向是 **客户端 → 服务器**？ → 使用 `Request` 后缀
- [ ] 数据流向是 **服务器 → 客户端**？ → 使用 `DTO` 后缀
- [ ] 是 **业务实体** 的完整表示？ → 使用 `{Entity}DTO` 模式
- [ ] 是 **操作响应** 而非业务实体？ → 使用 `{Action}ResponseDTO` 模式
- [ ] 是 **事件 Payload**？ → 使用 `{EventName}Payload`（无后缀）
- [ ] 是 **事件本身**？ → 继承 `BaseEvent`，类名以 `Event` 结尾
- [ ] 类名是否与已有命名模式一致？（如 `Create`/`Update`/`Control`/`Invoke`）

---

## 7. 反面示例（应避免）

```python
# ❌ 模糊不清：不知道是请求还是响应
class JobControlData(BaseModel): ...

# ❌ 错误方向：Response 应该是服务器→客户端，但未使用 DTO 后缀
class JobControlResponse(BaseModel): ...  # 应改为 JobControlResponseDTO

# ❌ 错误方向：Create 应该是客户端→服务器，但未使用 Request 后缀
class MessageCreate(BaseModel): ...  # 应改为 MessageCreateRequest

# ❌ 冗余：ResponseDTO 重复（Response 已隐含响应语义）
class JobControlResponseResponseDTO(BaseModel): ...

# ❌ 不一致：其他都用 Request，这个用 Form
class SessionCreateForm(BaseModel): ...
```

---

## 8. 与事件系统的区别

注意：**事件（Event）系统**独立于 API 请求/响应流：

- **API Schema**：`Request` / `DTO` — 用于 HTTP 接口
- **Event Schema**：`Event` / `Payload` — 用于 SSE 事件总线

两者数据结构和用途不同：

```python
# API：客户端与服务器间
MessageCreateRequest  → 创建消息（HTTP POST）
MessageDTO            ← 查询消息（HTTP GET）

# 事件：服务器内部/到客户端（SSE）
MessageCreatedEvent   → 消息已创建事件（事件总线）
MessageCreatedPayload → 事件负载
```

---

## 9. 参考资料

- 类似设计：[kilocode/packages/opencode/src/session/status.ts](http://kilocode/packages/opencode/src/session/status.ts)（事件 discriminated union 实现）
- Pydantic 文档：Discriminated Unions（用于 Event 类型安全）
- RESTful API 设计指南：请求/响应命名规范

---

**维护者**：Backend Team
**最后更新**：2026-04-25
**状态**：草案
