## 1. 配置与运行时契约

- [x] 1.1 在 `workspace_schema.jsonc` 增加 `runtime.debug`、Node、Python 预留配置和 launch profile 的严格 schema
- [x] 1.2 在 `workspace_inline.jsonc` 增加安全的 Node Inspector 默认配置，使用 loopback 和动态端口
- [x] 1.3 在 `ConfigService` 增加规范化 debug runtime 配置读取与 adapter/profile/端口校验
- [x] 1.4 为 debug 配置默认值、工作区覆盖、未知字段、非法端口和非法 timeout 增加配置单元测试

## 2. Node 调试服务扩展

- [x] 2.1 为 Node 调试运行时接入 debug 配置，保持未配置时现有行为和动态 Inspector 端口
- [x] 2.2 扩展 Node 调试断点模型，支持条件断点元数据并在 Inspector 安装时传递 condition
- [x] 2.3 为 Node adapter 实现不会暂停目标程序的 logpoint，并将日志写入调试输出
- [x] 2.4 增加按 session 的 Agent 工具动作审计，记录工具名、tool call identity、结果和时间
- [x] 2.5 增加安全的路径、工作目录、profile 和 adapter 解析，拒绝 workspace 外路径及未实现 adapter

## 3. Agent 调试工具组

- [x] 3.1 新增调试扩展目标工具输入模型和 JSON 结果包装，严格实现 16 个 DebugMCP 兼容工具 schema
- [x] 3.2 实现启动、停止、重启、继续、暂停和三种单步工具，并返回 authoritative debug state
- [x] 3.3 实现普通断点、条件断点、断点移除、断点列举和全部清理工具
- [x] 3.4 实现变量名、指定变量值和表达式求值工具，支持 scope 校验和暂停上下文校验
- [x] 3.5 将 `ToolInvocationContext` 和 `NodeDebugService` 注入扩展工具 factory，隐藏 session 和运行时内部字段

## 4. Agent 注册与策略

- [x] 4.1 在 `tools.custom` 注册首批 16 个调试执行工具并加入 debugging catalog 分组，默认 Agent runtime 只注册固定 `invoke_custom_tool`
- [x] 4.2 将 NodeDebugService 注入扩展工具构建链，不加入默认工具构建链
- [x] 4.3 让调试工具遵守 denylist、allowlist 和 `confirmation_required`，验证 expression 工具确认行为
- [x] 4.4 更新工具目录和运行时工具 schema 测试，确认模型只看到 `invoke_custom_tool`，且目标 schema 不暴露 session、端口、thread/frame 或 VS Code 字段

## 5. 纯后端 E2E 测试

- [x] 5.1 新增隔离的 JS 调试 fixture 和后端 E2E fixture 资源准备逻辑
- [x] 5.2 验证首批 16 个扩展执行工具可以从 custom-tool factory 构建并由工具目录暴露原始名称和兼容输入 schema，同时确认 Agent runtime 不暴露目标工具直达入口
- [x] 5.3 使用真实 Node Inspector 验证断点暂停、继续、单步、调用栈、变量和表达式求值
- [x] 5.4 验证条件断点、命中次数断点、logpoint 不暂停语义、非法参数和无暂停上下文错误
- [x] 5.5 验证两个 session 的调试状态隔离、动态端口不冲突和动作审计记录
- [x] 5.6 验证工作区 debug profile 覆盖和旧配置无 debug 字段时的兼容行为

## 6. 验证与交付

- [x] 6.1 运行受影响的 Python 静态检查、类型/编译检查和 focused unit tests
- [x] 6.2 运行新增纯后端 E2E 测试并保留规定目录下的测试产物
- [x] 6.3 运行 `openspec validate add-agent-debug-tool-group --strict`
- [x] 6.4 更新任务状态并确认实现与 proposal、spec、design 一致

## 7. Skill 与提示词驱动验证

- [x] 7.1 在 `resources/skills/debugging/` 增加产品级 Skill、完整工具 schema、调用顺序和安全边界，并加入默认 Gateway Skill 组
- [x] 7.2 让默认 E2E 工作区准备器把 `resources/skills/` 同步到隔离工作区，验证产物包含 `/.boxteam/skills/debugging/SKILL.md`，避免在 `asset/` 维护产品副本
- [x] 7.3 增加经过真实后端 HTTP、真实 Agent runtime、固定 `invoke_custom_tool`、真实 Node Inspector 和本地 OpenAI-compatible 模型边界的提示词驱动 E2E
- [x] 7.4 增加可选的真实外部模型 E2E 开关 `BOXTEAM_RUN_LIVE_DEBUG_E2E=1`，默认不产生外部模型调用费用
- [x] 7.5 将原有直接 `.ainvoke()` 用例明确标为 Node Inspector 适配器集成检查，不再把它们作为完整提示词 E2E 的证据
- [x] 7.6 运行新 E2E、静态检查和 OpenSpec 严格校验

## 8. 共享协作、会话配置与跨文件断点

- [x] 8.1 删除 Agent 执行调试的接管、交接和控制模式协议/UI，保留共享动作与 actor 审计
- [x] 8.2 更新 debugging Skill，说明人类可在模型工具调用之间操作 Web、终端和源码，模型必须接受最新权威状态
- [x] 8.3 新增会话级 Node 调试配置存储，持久化入口、profile、参数、断点、动作和配置修订，不保存运行时连接句柄
- [x] 8.4 为断点增加源码锚点、`current/pending_update/source_deleted` 状态和活动进程 `requires_restart` 标记
- [x] 8.5 让窄体源码预览在暂停时跟随实际 frame，未暂停时跟随最近新增或用户选择的跨文件断点
- [x] 8.6 将 Node Inspector 与提示词调试 fixture 改为两个 JavaScript 文件，并验证 AI/人类共享操作、跨文件暂停和配置恢复
- [x] 8.7 运行 Python 静态检查、focused tests、Web build、真实 Node/Web 全链路检查和 OpenSpec 严格校验

## 9. 只调试目标程序与可移植多方案

- [x] 9.1 删除 Agent 自身执行循环调试的 schema、服务、事件、Job API、容器注入、Web 标签页和测试，不保留兼容协议
- [x] 9.2 将会话 Node 调试存储拆为 manifest、动作审计和独立版本化方案文件，支持会话内多套方案与活动选择
- [x] 9.3 增加方案列举、创建、更新、激活、删除、导入导出和跨会话复制 API，并校验相对路径与运行中切换边界
- [x] 9.4 扩展模型调试工具与 debugging Skill，使 Agent 能创建、选择和使用目标程序调试方案
- [x] 9.5 更新 Web 右侧调试面板和扩展窗口的数据流，提供高频切换与二级方案管理，不再展示 Agent 自身调试入口
- [x] 9.6 刷新 OpenAPI/生成类型并完成静态分析、focused tests、Web build、真实浏览器链路和全局残留检索
- [x] 9.7 执行目录结构、顶层符号、文件规模和 AGENTS.md 覆盖审查，修复本次改动引入的架构问题

## 10. 对话驱动与人工复跑完整 Web 流程

- [x] 10.1 扩展 debugging Skill，要求模型定位入口/依赖、先列举方案、缺失时创建具名方案，并在每次真实暂停后按解释、计数求值加一、继续的顺序工作
- [x] 10.2 增加提示词驱动回归测试，覆盖无匹配方案、跨文件断点、两次暂停求值和真实退出
- [x] 10.3 增加 Web 状态回归测试，覆盖右侧源码预览从空状态到连续跨文件暂停、退出后保留上下文及人工复跑
- [x] 10.4 使用隔离工作区从 Web 创建源码并完成模型自动调试流程，修复入口发现、方案创建、源码跟随、控制台或消息反馈问题
- [x] 10.5 使用模型保存的方案从 Web 手动启动并逐步执行到退出，修复控制按钮、状态和动作历史问题
- [x] 10.6 重跑静态检查、聚焦测试、Web build、真实浏览器复测与 OpenSpec 严格校验

## 11. VS Code 风格特殊断点

- [x] 11.1 更新规格与设计，定义普通、条件、命中次数和日志点的统一模型及源码行号槽交互
- [x] 11.2 扩展后端 DTO、方案存储和 Node Inspector adapter，实现原子编辑、命中次数与不暂停的日志点
- [x] 11.3 扩展 Agent `add_breakpoint`、`add_logpoint` 与 debugging Skill，使模型可创建并识别特殊断点
- [x] 11.4 在右侧侧边栏源码预览和扩展窗口源码区实现右键创建、编辑、删除特殊断点，保留左键普通断点快捷操作
- [x] 11.5 增加后端真实 Node、Agent 工具和 Web 交互回归测试
- [x] 11.6 刷新 OpenAPI/生成类型并执行静态检查、聚焦测试、Web build 与 OpenSpec 严格校验

## 12. 源码变化后的保守失效

- [x] 12.1 禁止源码变化后的断点自动重定位，保留原请求行号并标记 `pending_update`/`source_deleted`
- [x] 12.2 清理活动 Inspector 的失效断点映射，但不阻止继续、暂停、单步和停止
- [x] 12.3 在调试开始和调试结束的工具结果中返回失效断点列表，并补充回归测试与 Skill 说明

## 13. 面向模型的调试提示词

- [x] 13.1 将 Skill 重写为基于 `state.status` 的最少调用决策流程，明确方案复用、暂停解释和结束确认
- [x] 13.2 明确人类并发操作、新消息和 `invalid_breakpoints` 的处理规则，避免控制权交接和失效阻断误判
- [x] 13.3 同步扩展工具短描述并通过提示词回归、静态检查和 OpenSpec 校验
