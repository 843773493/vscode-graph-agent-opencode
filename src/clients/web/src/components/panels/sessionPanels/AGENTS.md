# 目录用途

`components/panels/sessionPanels/` 存放会话与子线程面板族：AgentSessionsPanel（左侧侧边栏会话导航面板，含筛选、搜索、分组与工作区会话目录编排）、ChildThreadPanel（右侧侧边栏「运行与连接」中的子会话线程面板）及其渲染测试。两者都是会话层级资源的面板容器，业务数据由 App 层通过 props 传入。

本目录与相邻子包的边界：

- `components/agentSessions/`：会话树、筛选菜单、上下文菜单、文件夹资源浏览器与树状态 hook 的内部实现；本目录只做面板级组合与编排。
- `components/panels/`：其它可停靠面板容器（对话、事件、请求、资源树、底部面板族、Agent 状态）；本目录只承载会话与子线程这一条链路。
- `components/workspace/`：工作区文件、端口与 Gateway 工作区面板；本目录的会话面板只通过回调触发工作区动作，不实现工作区面板。
- `components/chat/`：对话回合渲染单元；与本目录无耦合。
- `components/shell/`：应用外壳与工具栏；本目录只消费 AnchoredOverlay 等通用浮层，不实现外壳。
- `state/display/`：权威数据的展示投影（资源、事件队列、Agent State、工具）；本目录不复制其展示映射。子线程状态文案来自 `state/childThreadDisplay`。
- `state/uiSettings/`：会话侧栏持久化偏好的唯一权威；本目录只读写其中的纯函数，不另存一份集合切换实现。

# 可修改内容

- 会话面板的筛选、搜索、分组、折叠与工作区会话目录编排。
- 子线程面板的列表渲染、调试 owner 选中与复制交互。
- 面板级对话框（重命名工作区、新建工作区、新建会话文件夹）的接线。
- 与上述逻辑直接相关的组件测试。

# 不可修改内容

- 不定义后端协议类型权威（来自 `types/backend` 与 `types/frontend`）。
- 不在本目录实现会话树、上下文菜单或资源浏览器的深层子组件（属 `components/agentSessions/`）。
- 不复制 `state/uiSettings/preferences` 的集合归一与切换实现，必须复用 `stableUiSettingIds` / `toggleUiSettingId`。

# 规范

- 会话与子线程数据只以 App 层传入的 props 与 API 返回为依据，不在本目录伪造成功状态。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
