## Context

当前 `app/schemas/public_v2` 是 Python Pydantic DTO，`scripts/generate_public_types.mjs` 逐文件调用 `pydantic2ts`，导致被引用类型在多个 TypeScript 文件中重复展开。当前运行时由 Workspace Backend、Workspace Gateway、Terminal Manager、Browser Manager 和多个前端组成，协议边界已经按进程存在，但没有统一的跨语言协议源。

本设计只迁移公开的跨进程协议。Workspace 内部事件总线、Agent 业务模型和本地存储模型继续由后端自行维护；它们通过适配器连接到公开协议。

## Goals / Non-Goals

**Goals:**

- 用 `.proto` 定义公共、Gateway、Workspace、Terminal、Browser 五个协议域。
- 通过 Buf 管理 package、版本、import、格式和兼容性检查。
- 生成 Python、Node/JavaScript 和 Web TypeScript 绑定，并保留协议源的目录层次与跨文件 import。
- 为 HTTP JSON、SSE、Terminal WebSocket 和 Browser WebSocket 提供明确的边界 codec/adapter。
- 首阶段保持现有 JSON/SSE 外部形状，降低前端和进程迁移风险。
- 在设计中保留少量 Message、Job、SSE、Terminal、Browser 示例，作为后续协议拆分的样板。

**Non-Goals:**

- 首阶段不把 Workspace 内部 `app.schemas.event.Event` 或 Agent 内部模型全部改为 Protobuf。
- 不把 Gateway 变成 Workspace、Terminal 或 Browser 业务协议的拥有者。
- 不在本次设计中列出所有现有业务 DTO 的字段级迁移清单。
- 不要求首阶段把 HTTP/SSE 改成二进制 Protobuf 传输。

## Decisions

### 1. `.proto` 是公开协议唯一来源

已迁移跨进程协议域以 `proto/` 下的 `.proto` 为唯一来源。尚未纳入本阶段的业务 DTO 可以暂时保留，用于兼容适配和回归验证，但不得在已迁移协议域继续新增并行协议字段。

协议 package 按进程域和版本拆分：

```text
proto/
└── boxteam/
    ├── common/v1/
    ├── gateway/v1/
    ├── workspace/v2/
    ├── terminal/v1/
    └── browser/v1/
```

目录树只表达协议边界，业务文件保留少量代表性入口：

```text
common/v1/       error.proto, pagination.proto, service_lifecycle.proto
gateway/v1/      health.proto, workspace_registry.proto, proxy.proto
workspace/v2/    message.proto, job.proto, session_interaction.proto, session_stream.proto
terminal/v1/     terminal.proto, terminal_input.proto, terminal_output.proto
browser/v1/      browser.proto, browser_page.proto, browser_input.proto
```

`common` 只放错误、时间、分页、请求上下文和服务生命周期等稳定基础类型；`Message`、`Job`、Session 事件属于 Workspace；PTY 消息属于 Terminal；页面、输入、帧和下载消息属于 Browser。

### 2. Gateway、Workspace、Terminal、Browser 共享基础协议但不共享业务归属

Gateway 进程只依赖 `common/v1` 和 `gateway/v1`。它可以管理 Terminal Manager、Browser Manager 的地址、健康和生命周期，但不拥有 Terminal/Browser 消息，也不解析 Workspace 业务事件。

Workspace Backend 通过 Terminal Manager Client 和 Browser Manager Client 使用对应协议；业务层只依赖抽象服务，协议绑定留在 `app/protocol` 或基础设施适配层。

Terminal Frontend 和 Browser Frontend 是各自 Manager 协议的客户端。浏览器 DOM、终端快捷键等只在前端本地处理，不进入公开协议；只有跨进程传输的 attach、input、output、resize、page、frame、download 等消息进入对应 `.proto`。

### 3. 生成代码按语言和运行时分层

建议保持以下生成布局：

```text
app/protocol/generated/boxteam/...                         # Python Workspace/Gateway 绑定
src/workspace-services/protocol/generated/boxteam/...       # Node/JavaScript 服务绑定
src/clients/web/src/types/protocol_generated/boxteam/...    # Web JSON/TypeScript 绑定
src/clients/web/src/types/protocol_buf_generated/boxteam/... # Web Protobuf runtime 绑定
```

各服务的协议 codec 和 adapter 位于服务边界附近：

```text
app/protocol/codecs/                               # Python JSON/SSE/WS codec
app/gateway/protocol/                              # Gateway 控制面适配
src/workspace-services/terminal/protocol/          # Terminal HTTP/WS 适配
src/workspace-services/browser/protocol/           # Browser HTTP/WS 适配
src/clients/web/src/protocol/                      # Web JSON/SSE/WS 适配
```

生成代码不得手工修改；生成脚本负责清理删除后的旧产物，并由契约测试验证生成结果。

### 4. 使用 typed oneof 表达事件和服务消息分支

Workspace 的会话事件、Terminal 的控制/输出消息、Browser 的页面/输入消息使用 Protobuf `oneof` 或明确的事件消息分支。少量样例：

```text
workspace/v2/session_stream.proto
  SessionExecutionEvent
    ├── message_updated
    ├── job_updated
    └── session_error

terminal/v1/terminal_output.proto
  TerminalServerMessage
    ├── output_chunk
    ├── state_changed
    └── completed

browser/v1/browser_page.proto
  BrowserServerMessage
    ├── page_state
    ├── frame
    └── download_ready
```

首阶段的 SSE JSON 仍通过适配器输出当前 `{type, payload}` 形状；oneof 是内部协议表达，不强迫现有 Web 客户端立即改用新的 JSON 结构。

### 5. 保留 JSON/SSE 边界兼容层

FastAPI、Gateway HTTP、Terminal HTTP/WS、Browser HTTP/WS 现有接口先保持路径、字段名、错误语义和事件顺序。Protobuf 绑定负责类型和跨语言依赖，codec 负责 JSON/WS 线格式转换。

OpenAPI 和 SSE runtime schema 不再新增逐文件 Pydantic 类型来源；迁移阶段由协议 descriptor 加路由元数据补充公开 schema，现有未迁移业务 DTO 仍作为兼容输入保留。现有快照和接口测试作为兼容性基线。

### 6. 选择 Buf 加多语言生成插件

Buf 负责协议 workspace、lint、依赖和 breaking change 检查；TypeScript 使用支持跨文件 import 的 Protobuf 生成插件，Python 使用对应的官方运行时绑定，Node/JavaScript 服务生成可被当前 ESM 服务加载的绑定。

生成配置必须支持 source-relative 输出、显式 import 映射和删除产物清理。具体插件参数在实现阶段以当前 Bun/Node、Python 和 Web 构建环境验证结果为准。

## Risks / Trade-offs

- [Pydantic 约束无法一比一表达] → 为动态对象、时间、optional 和范围/长度约束建立明确映射；必要时在协议层引入独立验证规则，并保留边界契约测试。
- [Protobuf oneof 的 JSON 形状不同于当前 SSE] → 首阶段保留 JSON/SSE adapter，不直接切换外部线格式。
- [全量业务 DTO 尚未完成协议化] → 本次只交付五个协议域的框架和代表性样例；删除 `pydantic2ts` 及全部重复 DTO 需要在其余业务域补齐 `.proto` 后作为发布门槛执行。
- [Gateway 误依赖 Workspace/Terminal/Browser 业务类型] → CI 检查 package 依赖方向，Gateway 只允许依赖 `common`、`gateway` 及通用服务生命周期类型。
- [Node/JavaScript 辅助服务无法直接加载 TypeScript 生成物] → 单独生成可加载的 ESM JavaScript 绑定，或在生成流程中固定 TypeScript 编译产物，不让服务运行时引用 Web 源码。
- [迁移期间存在 Pydantic 与 Protobuf 双份模型] → 设置迁移清单和删除门槛；新协议字段只允许进入 `.proto`，完成领域迁移后删除旧公共 DTO 生成链路。
- [OpenAPI schema 与 Protobuf schema 漂移] → 将 OpenAPI/SSE schema 生成纳入同一协议命令，并用 HTTP/SSE/WS 契约测试验证字段和错误形状。
