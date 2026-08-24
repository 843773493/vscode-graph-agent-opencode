## Context

当前工作区后端的工具目录把所有能力压缩成一个 `enabled` 字段，`ToolSelectionStore` 只保存禁用工具名；Agent 执行时再把这些名称作为 denylist 使用。这样无法表达“后端保留工具执行能力，但本轮模型请求不携带该工具定义”的状态。当前 MCP runtime 已经发现并持有远程 `BaseTool`，但 `AgentFactory` 将它们直接放入模型工具集合，和普通 `tools.custom` 扩展工具的 `invoke_custom_tool` 边界不一致。

Workspace Gateway 已经有 `/api/v1/{path:path}` 的透明工作区代理和远程 Gateway 路由，因此不新增 Gateway 业务工具注册表；本次只保证新的工具协议字段、认证、工作区选择和联邦转发不被代理层截断或改写。

## Goals / Non-Goals

**Goals:**

- 为每个目录工具提供 `execution_enabled` 与 `model_visible` 两个权威状态。
- 在 Agent graph 中保留执行开启的工具，在模型请求层过滤模型不可见的直接工具。
- 让扩展工具和 MCP 工具共同使用一个 `invoke_custom_tool`，并保留目标工具的真实 schema 做后端参数校验。
- 让模型可见的扩展工具 schema 以固定入口的补充描述形式出现；关闭时仍保留最小固定入口，供 Skill 驱动调用。
- 将 MCP 工具按 Server 归入 `扩展工具 · MCP · {server_id}`，不再把 MCP 作为独立工具组类型。
- 用后端单元/集成测试、Gateway 代理测试和 Web 组件测试覆盖两项开关的默认、联动、混合和错误路径。

**Non-Goals:**

- 不把工具选择状态提升为 Gateway 全局配置；状态仍属于当前工作区的 Agent 工具选择。
- 不为 Gateway 增加 MCP session 或工具执行逻辑；MCP 连接和调用仍属于工作区后端。
- 不改变 MCP Server 的连接配置格式、生命周期或远程工具实际参数 schema。
- 不实现独立的“只把工具描述作为普通 system prompt 文本发送、但不作为工具 schema”协议；模型可见性统一由模型工具定义和固定扩展入口描述承载。
- 不保留旧的单一 `enabled` API 字段或旧 MCP 顶层分类。

## Decisions

### 1. 使用 execution_enabled/model_visible，而不是继续扩展 enabled

API、持久化和前端 DTO 统一使用两个明确字段：

```text
execution_enabled  是否注册到 Agent runtime 并允许真实执行
model_visible      是否向模型请求提供工具说明和参数 schema
```

`model_visible=true` 必须以 `execution_enabled=true` 为前提。状态更新接口接收完整的双状态值，组级更新由前端展开成工具项变更，后端在同一事务中校验并提交。这样避免 `enabled` 在 UI、Agent graph 和模型请求中分别产生不同含义。

备选方案是用 `registered`、`executable`、`prompt_visible` 三个字段，信息更细但当前没有第三种独立运行时状态；不采用，避免制造无法验证的状态组合。

### 2. 持久化使用显式执行禁用集合和模型可见性覆盖

继续使用工作区 `.boxteam/settings/tool_selection.json` 作为本地 Agent 选择存储，但新结构按 Agent 保存：

```json
{
  "default": {
    "execution_disabled": ["write_file"],
    "model_visibility": {
      "mcp__tui_mcp__status": true,
      "read_file": false
    }
  }
}
```

未出现的工具按目录组类型计算默认值：普通默认/协作工具的 `model_visible=true`，普通扩展、MCP 扩展和 Source Debugging 扩展组的 `model_visible=false`；所有工具的 `execution_enabled=true`。显式 `model_visibility` 覆盖默认值。执行被关闭时解析结果强制不可见，并保留显式覆盖值，重新打开执行能力后仍需用户明确打开模型可见性。

备选方案是为每个新工具写入完整状态初始化记录，会让发现工具变成有副作用的读取操作，并且难以处理 MCP 工具动态变化；不采用。

### 3. 在模型调用 middleware 过滤可见的直接工具

`create_my_deep_agent` 仍把执行开启的直接工具注册到 graph，但新增轻量模型可见性 middleware，在每次模型请求调用下使用 `request.override(tools=...)` 过滤 `model_visible=false` 的直接工具。工具节点仍然保留，因此运行时和工具结果恢复不会因为模型隐藏而丢失；模型不会收到隐藏工具的名称、描述或参数 schema。

过滤发生在请求层而非直接从 graph 删除工具，原因是工具执行注册和模型工具列表本来就是两个生命周期：前者服务于工具结果路由，后者服务于当前模型请求。middleware 同时覆盖模型路由和重试产生的每次请求，避免 Agent 缓存导致状态只在首轮生效。

### 4. 扩展工具通过固定入口执行，schema 作为入口描述的可选部分

普通 `tools.custom` 工具和 MCP `BaseTool` 组成同一个目标工具映射，由 `create_custom_tool_invoker_tool` 创建唯一的 `invoke_custom_tool`。目标工具不直接加入模型工具列表；固定入口始终只暴露 `tool_name` 与 `arguments` 两个参数，并在调用前使用目标工具的公开 schema 校验参数。

当目标扩展工具 `model_visible=true` 时，固定入口描述附带目标工具名称、描述和 JSON schema；当其为 false 时不附带详细定义，只保留“可通过 Skill/上下文提供目标名后调用”的最小说明。这样既保留用户想要的 schema 可见性控制，又不要求 LangGraph 为每个动态扩展工具创建独立模型工具节点。

备选方案是把 MCP 工具和普通扩展工具全部直接注册，再通过 denylist 控制，无法满足隐藏 schema 仍可通过 Skill 调用的需求；不采用。

### 5. MCP 目录由同一 runtime manager 提供，但归入 extension

`McpRuntimeManager` 继续负责连接、工具发现和 session 生命周期；它返回的 MCP `BaseTool` 只作为扩展入口的目标工具。工具目录服务从这些目标工具的 metadata 和公开 schema 构建 DTO，使用：

```text
group_id   = mcp:{server_id}
group_name = 扩展工具 · MCP · {server_id}
kind       = extension
```

MCP 工具 ID 继续由现有长度限制和冲突检测函数生成。`AgentRuntimeDependencyProvider` 保留 MCP 工具访问接口，但 AgentFactory 不再把列表拼到直接模型工具数组，而是与普通 custom tools 一起传给固定入口。

### 6. Gateway 只做透明代理，测试覆盖协议完整性

不在 Gateway 复制工具状态或 MCP 目录。浏览器请求仍通过 `/api/v1/tools` 和 `/api/v1/tools/selection` 进入 Gateway，由通配工作区代理根据 `X-BoxTeam-Workspace-Id` 选择本地或远程工作区后端。代理必须原样转发双状态请求体，并保留后端的分组字段、错误状态和 request ID；远程 Gateway 路径继续使用既有 federation token，不把浏览器本地 token 传播到下游。

这样避免 Gateway 和工作区各存一份“工具是否启用”的状态，也保证多个 Gateway 访问同一工作区时以工作区后端为唯一权威。

### 7.1 选择状态的并发提交与公开 schema 校验

工具选择文件不是数据库，但它属于工作区后端的权威配置，不能把“临时文件替换”误认为完整并发控制。`ToolSelectionStore` 对同一配置使用旁边的 lock 文件和跨进程排他锁包住整个“读取—校验—合并—替换”过程；读取目录时使用共享锁。这样两个 Agent 或两个 Gateway 代理请求同时修改不同工具时，不会出现后一个写者覆盖前一个写者的状态。

扩展工具的 `tool_call_schema` 有两种合法形态：普通 LangChain 工具通常提供 Pydantic 模型，MCP 适配器可能直接提供 JSON Schema 字典。固定入口必须对两者都执行公开 schema 校验，并只把 schema 的公开 properties 转发给目标工具；不能因 MCP 没有 Pydantic 类就把参数校验整体跳过。无 schema 的工具继续快速失败并报告工具名和类型。

### 7. Web 使用两个 icon button 表达组级和工具级状态

工具组和工具项各显示两个图标按钮：

- 工具能力按钮：`codicon-tools`，提示“允许 Agent 调用此工具”；
- 模型可见按钮：`codicon-eye`，提示“把工具说明和参数提供给模型”。

按钮使用 `aria-pressed`、`data-state=on|off|mixed` 和 `title`，不再渲染 checkbox。执行能力关闭时模型可见按钮禁用且显示 off；组级 mixed 状态只表示组内真实状态的聚合，不向后端发送虚构的第三种布尔值。工具详情文本和测试按钮保持原有职责。

前端保存成功后以 API 返回的完整工具对象替换状态；保存失败重新拉取工具目录，遵守后端状态权威原则。组级操作由当前目录项展开，单工具操作只更新一个工具。

## Risks / Trade-offs

- [模型隐藏直接工具后仍可能从历史消息中看到旧工具调用] → 模型请求过滤只控制本次工具定义，不删除 checkpoint 历史；工具节点继续保留，历史一致性优先于伪造清理。
- [扩展入口描述包含多个 JSON schema 可能增大模型请求] → 默认扩展工具模型不可见；只有用户主动打开模型可见性才加入描述，并在入口描述中使用紧凑 JSON schema。
- [MCP 工具在扩展入口 fallback 调用时缺少自定义 coroutine 属性] → 优先使用目标工具公开的 `coroutine/func`，否则调用 `ainvoke`，并用真实 MCP stub 覆盖该分支。
- [动态 MCP 工具目录改变后选择状态残留] → 未出现在当前目录的持久化 ID 不参与运行时，目录读取只返回当前发现工具；重新发现同一 ID 时按保存状态恢复。
- [Gateway 远程路由继续持有长请求] → 工具目录和 PATCH 都是短请求，仍复用现有共享 HTTP client；只增加代理字段/错误测试，不复制连接管理逻辑。

## Migration Plan

1. 直接更新工作区工具选择存储格式和 API/DTO；原型阶段不读取旧 `enabled` 字段，也不迁移旧 MCP 顶层分类。
2. 更新 Agent runtime 和 MCP manager 后，先运行后端 focused tests，再刷新 OpenAPI/前端生成类型。
3. 更新 Web 工具面板并运行 Web build、组件测试和 Gateway 代理测试。
4. 若需要回滚，回滚整个变更版本；不提供新旧双格式并行读取，避免两套状态互相覆盖。
