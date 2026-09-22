# 目录用途

`components/panels/bottomPanel/` 存放主窗口底部面板族的四个标签页容器：TerminalPanel（终端）、GatewayLogPanel（输出）、PortForwardPanel（端口）与 AutomationPanel（自动化）。四者共享同一套「标签栏 + 内容区」外壳布局，切换入口与面板状态由 `state/workspaceBottomPanel` 按工作区保存，数据由 App 层通过 props 传入。

本目录与相邻子包的边界：

- `components/panels/`：其它可停靠面板容器（对话、事件、请求、资源树、Agent 状态、子会话等）；本目录只承载底部面板这一条链路。
- `components/panels/resourceTree/`：会话级可重连资源树；本目录的终端标签页只复用 App 传入的 Gateway 扩展资源条目，不渲染资源树。
- `components/workspace/`：端口面板内容实现 WorkspacePortForwardPanel 位于 `components/workspace/`，本目录只做底部面板外壳接入。
- `components/agentSessions/`：SessionGeneratorManager 位于 `components/agentSessions/`，本目录的自动化标签页只做外壳组合。
- `state/workspaceBottomPanel.ts`：底部面板当前标签与高度的权威状态；本目录不持有该状态。

# 可修改内容

- 底部面板外壳布局、标签栏、工具栏与内容区编排。
- 终端列表宽度拖拽、输出筛选与通道选择等面板级交互。
- 面板切换回调的接线。

# 不可修改内容

- 不定义后端协议类型权威（来自 `types/backend`）。
- 不在本目录实现端口转发、自动化任务或诊断日志的业务规则，这些属于被组合的子组件与后端。
- 不复制资源展示映射（属 `state/display/resourceDisplay`）或 Gateway 日志展示文案（属 `components/panels/gatewayLogPresentation`）。

# 规范

- 面板数据只以 App 层传入的 props 与 API 返回为依据，不在本目录伪造成功状态。
- 导入共享模块使用显式相对路径，禁止任何指向旧平铺路径的转发层。
- 代码注释使用中文，专业术语除外。
- 修改本目录后需要运行 `bun run --cwd src/clients/web build`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
