# 目录用途

`components/panels/inspectionPanels/` 存放只读诊断面板族：EventQueuePanel（事件视图，权威 Trace 事件队列审计）、RequestLogPanel（请求视图，逐次审计模型输入输出与请求组成来源）、AgentStatePanel（Agent State 原始 JSONL 调试快照）及其渲染测试。三者都是按会话读取后端权威数据、按层级懒展开的只读审计面板，数据由 App 层通过 props 传入。

本目录与相邻子包的边界：

- `components/panels/`：其它可停靠面板容器（对话、资源树、底部面板族、会话与子线程）；本目录只承载只读诊断这一条链路。
- `components/eventQueue/`：事件视图的滚动窗口与服务端旧页协调 hook（`useEventQueuePagination`、`useScrollWindow`）；本目录的面板消费它，不另写一份滚动逻辑。
- `components/contextInspection/`：Agent State 面板「冻结请求」模式内嵌的上下文检查组件；本目录只做接入。
- `components/chat/`：对话回合渲染单元；与本目录无耦合。
- `components/workspace/`：工作区文件、端口与 Gateway 面板；本目录不渲染工作区资源。
- `components/shell/`：应用外壳；本目录不实现。
- `state/display/`：事件队列与 Agent State 的展示投影权威（`eventQueueDisplay`、`agentStateDisplay`）；`state/requestLogDisplay/`：请求日志展示投影权威。本目录只渲染，不复制其投影与文案映射。

# 可修改内容

- 三个面板的列表渲染、分组标题、折叠展开与懒加载编排。
- 面板级分页入口、滚动窗口与滚动锚点恢复的接线。
- 与上述逻辑直接相关的组件测试。

# 不可修改内容

- 不定义后端协议类型权威（来自 `types/backend` 与 `types/frontend`）。
- 不在本目录复制事件/请求/Agent State 的展示投影、常量或格式化逻辑，必须复用对应 `state/` 模块。
- 不请求 Trace API、不维护跨会话历史缓存、不写入主聊天 Turn 或 pending 状态。

# 规范

- 诊断数据只以 App 层传入的 props 与 API 返回为依据，不在本目录伪造成功状态。
- 大体积 Prompt、工具 schema 与 JSON 必须按层级懒展开，不要恢复成默认渲染全部内容。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
