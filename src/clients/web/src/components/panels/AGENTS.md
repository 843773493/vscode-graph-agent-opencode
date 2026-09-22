# 目录用途

存放主窗口与扩展窗口的各类面板容器组件：请求日志、事件队列、会话资源、Agent 会话、Gateway 扩展资源、聊天、Agent 状态、自动化、端口转发、终端、Gateway 日志和子会话面板，负责把对应数据渲染成可停靠的面板 UI。

本目录与相邻子包的分工边界：

- `components/chat/`：聊天回合内部的渲染单元（ChatTurn、虚拟滚动等），本目录的 ChatPanel 只做列表容器与分页编排。
- `components/agentSessions/`：Agent 会话树与筛选菜单内部实现，本目录的 AgentSessionsPanel 只做面板级组合。
- `components/workspace/`：工作区文件与端口等面板实现，本目录的 PortForwardPanel 只做工作区端口面板的接入层。
- `components/eventQueue/`、`components/contextInspection/`、`components/nodeDebug/`、`components/gatewayExtensions/`：各自专属面板的进一步拆分子包。
- `components/overlays/`（待建）：AnchoredOverlay / WarmActionDialog / SessionNameDialog / GatewayUserAccessMenu 等覆盖层与对话框，术语上不属于面板。

# 可修改内容

- 各面板的布局、可见性渲染与数据到视图的映射。
- 面板级分页、滚动与交互入口。
- 与上述逻辑直接相关的组件测试。

# 不可修改内容

- 不定义后端协议类型的权威来源，不复制业务状态权威。
- 不吞掉数据错误；错误必须显式呈现。
- 不在本目录实现面板内部的深层子组件（应放入对应子包）。

# 规范

- 业务数据只以 API 返回和事件流数据为依据，不在本目录伪造成功状态。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
