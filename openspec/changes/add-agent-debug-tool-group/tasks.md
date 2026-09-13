## 1. 配置与运行时契约

- [ ] 1.1 在 `workspace_schema.jsonc` 增加 `runtime.debug`、Node、Python 预留配置和 launch profile 的严格 schema
- [ ] 1.2 在 `workspace_inline.jsonc` 增加安全的 Node Inspector 默认配置，使用 loopback 和动态端口
- [ ] 1.3 在 `ConfigService` 增加规范化 debug runtime 配置读取与 adapter/profile/端口校验
- [ ] 1.4 为 debug 配置默认值、工作区覆盖、未知字段、非法端口和非法 timeout 增加配置单元测试

## 2. Node 调试服务扩展

- [ ] 2.1 为 Node 调试运行时接入 debug 配置，保持未配置时现有行为和动态 Inspector 端口
- [ ] 2.2 扩展 Node 调试断点模型，支持条件断点元数据并在 Inspector 安装时传递 condition
- [ ] 2.3 为 Node adapter 明确返回 logpoint 不支持结果，禁止静默降级成普通暂停断点
- [ ] 2.4 增加按 session 的 Agent 工具动作审计，记录工具名、tool call identity、结果和时间
- [ ] 2.5 增加安全的路径、工作目录、profile 和 adapter 解析，拒绝 workspace 外路径及未实现 adapter

## 3. Agent 调试工具组

- [ ] 3.1 新增调试工具输入模型和 JSON 结果包装，严格实现 16 个 DebugMCP 兼容工具 schema
- [ ] 3.2 实现启动、停止、重启、继续、暂停和三种单步工具，并返回 authoritative debug state
- [ ] 3.3 实现普通断点、条件断点、断点移除、断点列举和全部清理工具
- [ ] 3.4 实现变量名、指定变量值和表达式求值工具，支持 scope 校验和暂停上下文校验
- [ ] 3.5 将 `ToolInvocationContext` 和 `NodeDebugService` 注入工具 factory，隐藏 session 和运行时内部字段

## 4. Agent 注册与策略

- [ ] 4.1 将 16 个调试工具加入默认 Agent 工具全集和 debugging catalog 分组
- [ ] 4.2 将 NodeDebugService 加入 Agent runtime dependency provider 和默认工具构建链
- [ ] 4.3 让调试工具遵守 denylist、allowlist 和 `confirmation_required`，验证 expression 工具确认行为
- [ ] 4.4 更新工具目录和运行时工具 schema 测试，确认不暴露 session、端口、thread/frame 或 VS Code 字段

## 5. 纯后端 E2E 测试

- [ ] 5.1 新增隔离的 JS 调试 fixture 和后端 E2E fixture 资源准备逻辑
- [ ] 5.2 验证 16 个工具可以从 Agent runtime 构建并暴露原始名称和兼容输入 schema
- [ ] 5.3 使用真实 Node Inspector 验证断点暂停、继续、单步、调用栈、变量和表达式求值
- [ ] 5.4 验证条件断点、logpoint 明确不支持、非法参数和无暂停上下文错误
- [ ] 5.5 验证两个 session 的调试状态隔离、动态端口不冲突和动作审计记录
- [ ] 5.6 验证工作区 debug profile 覆盖和旧配置无 debug 字段时的兼容行为

## 6. 验证与交付

- [ ] 6.1 运行受影响的 Python 静态检查、类型/编译检查和 focused unit tests
- [ ] 6.2 运行新增纯后端 E2E 测试并保留规定目录下的测试产物
- [ ] 6.3 运行 `openspec validate --change add-agent-debug-tool-group --strict`
- [ ] 6.4 更新任务状态并确认实现与 proposal、spec、design 一致
