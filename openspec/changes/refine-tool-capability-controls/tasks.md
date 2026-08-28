## 1. 工具能力状态与 API

- [x] 1.1 将工具 DTO、选择请求和选择响应改为 `execution_enabled`/`model_visible` 双状态，并补齐非法组合与原子更新校验。
- [x] 1.2 重写工作区工具选择存储，保存按 Agent 的执行和模型可见性显式布尔覆盖，静态默认交给策略解析器且不读取旧单一 `enabled` 格式。
- [x] 1.3 更新工具目录服务和选择服务，返回当前工具完整状态，正确表达组内混合状态并保证未知/重复工具不会部分写入。
- [x] 1.4 更新 Agent 执行服务和运行时缓存边界，使下一次 Agent 请求读取最新执行/模型可见状态。

## 2. Agent runtime 与扩展调用

- [x] 2.1 新增模型工具可见性 middleware：执行注册保留开启工具，但每次模型请求只携带可见且可执行的直接工具。
- [x] 2.2 扩展 `invoke_custom_tool`，把普通扩展工具和 MCP 工具放入同一目标映射，调用前使用真实公开 schema 校验参数。
- [x] 2.3 让固定扩展入口按 `model_visible` 选择性附带目标工具名称、描述和参数 schema，并保留无详细 schema 时的最小调用说明。
- [x] 2.4 移除 MCP 工具直接注入模型工具集合的路径，保证 Agent graph 只通过固定扩展入口调用 MCP 目标工具。

## 3. MCP 目录与 Gateway

- [x] 3.1 将 MCP 工具目录统一映射为 `kind=extension`、`group_id=mcp:{server_id}`、`group_name=扩展工具 · MCP · {server_id}`，保留冲突失败行为。
- [x] 3.2 更新 MCP/工具目录的公开 schema、OpenAPI 和生成类型，确保双状态和扩展分组字段贯穿本地工作区后端。
- [x] 3.3 补充 Gateway 本地工作区和远程 Gateway 工作区的工具目录/状态更新代理测试，验证字段、错误、认证和工作区路由不被篡改。

## 4. Web 工具面板

- [x] 4.1 将工具组和工具项的复选框替换为“工具能力”和“模型可见”两个图标按钮，补充悬停提示、`aria-pressed`、mixed/off/disabled 状态和键盘操作。
- [x] 4.2 实现组级双能力批量切换、工具级双能力单项切换，以及执行关闭后模型可见自动关闭的前端状态更新。
- [x] 4.3 更新工具目录 API 类型、错误回滚和面板统计，显示可执行/模型可见两种计数但不伪造后端状态。
- [x] 4.4 更新 MCP 扩展组的前端分组展示和默认状态，不再识别独立 `mcp` 工具分类。
- [x] 4.5 增加组件测试，覆盖默认值、组内混合状态、按钮联动、保存失败回滚、悬停提示和无障碍属性。

## 5. 验证与审查

- [x] 5.5 将 Source Debugging 工具组纳入扩展工具默认隐藏策略，并覆盖默认状态与显式开启测试。

- [x] 5.1 更新后端工具选择、Agent runtime、扩展入口和 MCP stub 测试，覆盖真实执行、隐藏 schema、非法参数和 MCP 调用。
- [x] 5.2 运行 Python 静态检查、后端 focused tests、Gateway focused tests、生成类型检查和 `bun run --cwd src/clients/web build`。
- [x] 5.3 使用独立审查视角检查实现与 OpenSpec 的完整性、协议一致性、架构边界和 Web 实际交互，记录所有 P1/P2/P3 findings。
- [x] 5.4 修复审查发现的问题并重复 focused tests、Web build、OpenSpec strict validation，直到没有未处理的 P1/P2。

## 6. Workspace 工具策略配置

- [x] 6.1 在 Workspace schema、`workspace_inline.jsonc` 和配置解析器中增加 `tooling.policy_defaults`、`policy_rules`、Agent 局部 `tools.policy` 与硬限制结构；删除不生效的 `tools.enabled` 示例字段。
- [x] 6.2 新增唯一 `ToolPolicyResolver`，实现默认值、origin/kind/group/tool 覆盖、Agent 局部规则、硬限制和执行关闭联动，并补充配置解析测试。
- [x] 6.3 为工具目录、运行时定义和 Gateway 公开 DTO 补充 `origin`，统一 builtin/custom/mcp/debugging 的 `kind`、`group_id` 与策略匹配元数据。
- [x] 6.4 让 ToolService、AgentFactory、固定扩展入口和模型可见性 middleware 统一使用 `ToolPolicyResolver`，移除 Source Debugging/MCP/普通扩展默认值的散落硬编码。
- [x] 6.5 保留当前工作区 ToolSelectionStore 作为运行时覆盖，不实现跨电脑同步；覆盖与静态硬限制的优先级、执行/模型联动和错误响应补充测试。
- [x] 6.6 更新 OpenAPI/前端生成类型与工具面板状态来源，补充配置默认、组级/工具级覆盖、硬限制和 MCP 目录的前后端集成测试。
- [x] 6.7 运行配置 schema、后端 focused tests、Gateway 代理测试、Web 单元测试和 Web build，并执行 OpenSpec strict 校验。
