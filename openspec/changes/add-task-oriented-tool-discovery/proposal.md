## Why

当前 MCP 已有唯一目录 owner、固定调用信封和上下文封存，但模型主要获得截断的参数摘要，缺少按任务发现完整工具定义、补齐连接授权和等待恢复的统一流程。执行绑定仍依赖 Agent 捕获的工具对象及当前目录查找，连接配置只能重启生效，无法完整表达复杂 schema、连接代际与旧调用之间的关系。

## What Changes

- 新增通用只读入口 `search_tools`：按任务检索或按稳定工具 ID 获取完整定义，复用已有目录发布、策略、资源快照和上下文激活机制；首期覆盖已有 MCP、本地扩展和本地连接控制目标。
- 将完整公开 JSON Schema 作为目录 revision、schema hash、发现正文、封存和执行校验的共同输入；移除只对 `tool.args/properties` 摘要计算契约身份、无条件拒绝额外字段和静默裁剪合法字段的路径。
- 保留 `invoke_extension_tool(tool_name, arguments)` 的输入契约；普通并发沿用同轮多个信封调用，不新增 multi-execute、schema-fetch、MCP 专用搜索或专用等待入口。
- 在同一扩展目录注册本地 `connections.manage` 控制目标，支持查询、发起授权、状态与有界等待、断开；区分 MCP Server 授权与 Server 内上游应用授权。
- 长授权等待通过持久操作和既有精确 thread 后台通知／内部唤醒恢复；补齐去重、超时、取消、重启对账和未知执行结果处理。
- 在工作区后端内重构唯一 MCP owner 的连接监督与调用 handle 管理，实现按 Server 的候选准备、原子切换和旧代际排空；不新增 MCP 服务进程，不重写协议 SDK。
- **BREAKING（模型上下文行为）**：`model_visible` 保持自动注入开关，显式 `search_tools` 可按需返回可执行目标定义；隐藏自动注入不再禁止显式发现。默认 MCP 全量摘要改为能力导航和按需完整定义，不改变既有显式开启注入、Skill 调用和执行权限语义。
- 修改 MCP 配置生效策略：完成 supervisor 验收的连接字段由重启工作区改为受控热应用；整体候选原子性、不支持字段的重启分类与失败状态继续沿用现有配置流水线。

## Capabilities

### New Capabilities

- `task-oriented-tool-discovery`: 通用任务搜索、精确 schema 获取、确定性有界结果、显式发现与自动注入分离、定义恢复和前缀稳定边界。
- `extension-connection-management`: 本地连接控制目标、双层授权、状态机、用户授权入口与精确 thread 等待恢复。
- `mcp-connection-supervision`: 工作区连接监督、完整目录发布、连接代际、执行 lease、热替换、排空与故障核实。

### Modified Capabilities

- `extension-tool-invocation`: 完整 schema 校验、显式发现、冻结定义与执行身份一致、现有信封及逐调用行为保持。
- `configuration-domains`: 将可安全监督替换的 MCP 连接配置分类为工作区级受控热应用。
- `configuration-bootstrap`: MCP 候选准备／切换／恢复与既有整体提交状态机衔接。
- `configuration-evolution`: 配置结果准确区分已应用 MCP 热变更、整体待重启与应用失败。

## Impact

- `app/services/infrastructure/mcp/`、`app/agents/agent_factory.py`、`model_tool_schema.py`、`tools/custom_invocation.py`、工具策略／目录与确认中间件；所有旧调用方和测试整体迁移，移除旧目标解析路径。
- 复用 ResourceRegistry、ResourceActivationCoordinator、CSM、Saver/ContextStore、ToolSet/prefix compiler；不建立第二份目录、独立激活 writer 或 Provider 私有工具协议。
- 配置仅涉及 Workspace 既有 JSONC 配置族、schema、分层合并和配置应用 owner；新增字段须遵守配置技能。连接运行态不回写配置，凭据不进入模型、会话正文或日志。
- 连接操作状态属于工作区领域存储；等待关联与恢复事实按 `(session_id, thread_id)` 存入既有会话节点与后台工作机制。Gateway 透明代理；远程授权回调由实际连接 owner 接收和核实。
- Web 授权入口与状态展示走实际 Gateway／目标工作区；本地服务不依赖 Composio，Composio 只作为显式可配置的外部 MCP Server。
- 衔接 `add-context-injection-lifecycle` 的唯一发布／激活／封存合同、`add-itemized-rollout-context` 的恢复／准入／内部唤醒和 `add-multi-workspace-backend-mounting` 的工作区作用域；本变更不修改这些变更的规划文件。
- 与 `unify-product-runtime-logging` 的配置差分在实施、归档时逐 requirement 合并：该变更不改 MCP，本变更不改日志契约，禁止用旧全文覆盖对方结果。
