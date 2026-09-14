## Why

当前工作区已经具备 Node Inspector 源码调试服务、Web 调试面板和一组 DebugMCP 风格扩展目标，但其 OpenSpec仍把16个调试能力写成Provider直接工具、把调试资源绑定到Session；这与“少量直接工具＋固定`invoke_extension_tool`信封”及一个Session可包含多个SessionThread的目标架构冲突。需要保留稳定的调试目标名/参数契约，同时把模型调用入口和调试资源owner收敛到新版边界。

## What Changes

- 保留16个DebugMCP风格源码调试目标名称和各自输入契约；它们只进入ExtensionToolCatalog，通过Provider可见且始终存在的固定`invoke_extension_tool(tool_name, arguments)`信封调用，不作为16个Provider直接工具，也不把目标清单塞进信封description。
- 保留现有调试会话生命周期、继续/暂停/单步、条件/命中次数断点与不暂停的 Node Logpoint、变量查看和表达式求值；不能把已实现的 Logpoint 降级为“不支持”。
- 首期使用现有 Node Inspector 实现 JavaScript 调试；为未来 debugpy、DAP 和 VS Code 调试适配预留后端边界，但本次不实现 VS Code 会话路由。
- 核对并补齐既有 `runtime.debug` 工作区配置及 schema，保留默认 adapter、Node Inspector、debugpy 预留配置和可命名的 launch profile；不新增 thread 级 JSONC 覆盖层。
- 默认使用 loopback 和动态调试端口，禁止将 Web 端口 8211 作为 Inspector 或 debugpy 端口。
- 以精确`(session_id, thread_id)`作为调试运行时、方案归属、断点、动作审计与持久定位的owner；可移植方案正文不写入owner，目录索引/manifest建立归属。普通Session入口只解析权威main thread，child入口必须明确绑定child thread。模型不传产品thread或DAP/Inspector内部ID，也不取得可跨thread操作的端口/WebSocket句柄。
- 保留`debugConfigurationId`与两类断点的`hitCondition`作为信封内目标参数，不增加Provider直接工具；调试模块负责旧Session调试文件向main thread的定点迁移，并接入itemized共享maintenance gate。
- `start_debugging`继续要求路径参数；选中已有方案时，这些参数必须与方案在当前Workspace模板下解析出的有效入口/工作目录一致，显式profile也必须一致，冲突在任何方案激活、旧进程停止或新进程启动前报错。公开fork只在同Workspace按固定模式复制可移植方案；跨Workspace方案导入不是本次可调用能力。
- 当前thread的调试进程处于`starting|running|paused|stopping`时阻止resident runtime的30分钟idle卸载；由调试owner核实进程终态并维护跨Turn资源lease，不能由`LifetimeScope`关闭推断进程已停止。
- 调试进程使用独立于单次tool/Web操作的持久process-instance claim；先登记启动意图再spawn，重启恢复必须核对这一代进程的可验证身份，不能仅凭PID或端口认领、停止其它进程。
- 调试目标的启停、权限和确认策略只改变内层扩展目录/调用准入，不改变Provider ToolSetRef或同epoch已提交前缀；直接工具或固定信封本身变化仍服从上下文生命周期change的ToolSet hard rebase。调试目标可通过受控Skill/工具指引被模型发现。
- 对表达式求值和其他调试动作记录可审计的调试动作；表达式求值支持沿用 Agent 工具确认策略进行配置。
- 返回统一的调试状态和错误结构，同时保留足够信息供 Web 调试面板和 Agent 继续工作。

## Capabilities

### New Capabilities

- `agent-debug-tool-group`: 定义16个信封内源码调试目标、各自输入schema、thread级生命周期、状态返回和错误行为。
- `debug-runtime-configuration`: 定义 `runtime.debug` 工作区配置、Node Inspector 默认值、未来 adapter/launch profile 扩展点、端口和安全边界。

### Modified Capabilities

- 无。现有配置初始化和运行时加载能力继续负责加载、合并和校验 `runtime.debug`；本变更不改变配置层级或版本契约。

## Impact

- Agent工具注册和工具策略：`app/agents/agent_tools.py`、`app/agents/tools/`、ExtensionToolCatalog、`invoke_extension_tool`的调度与提示词、Agent配置选择；不增加16个Provider tools定义。
- 调试业务与基础设施：现有`NodeDebugService`、`NodeDebugSessionStore`、配置registry、工具调用上下文、调试动作审计及Node调试API须从Session隐式key迁为精确SessionThread owner；旧Session入口经catalog解析main，child使用thread级入口。
- 配置文件与 schema：`configs/workspace_inline.jsonc`、`configs/workspace_schema.jsonc`，以及工作区 `.boxteam/workspace.jsonc` 覆盖。
- Web 调试面板和 SSE 状态消费可能需要适配统一的工具动作/状态模型。
- 不新增外部调试服务依赖；debugpy、VS Code Debug API、DAP 连接器仅作为后续扩展点。
