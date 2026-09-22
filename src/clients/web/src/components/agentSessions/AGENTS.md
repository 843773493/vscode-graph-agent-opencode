# 目录用途

存放 Agent Sessions 面板的子组件，负责会话按钮、工作区会话分组、会话资源浏览器及其上下文菜单。

`SessionResourceExplorer.tsx` 只负责会话资源树与搜索结果的渲染；导航层级派生、拖拽/放置状态机和两条归属层级各自的放置提交链由 `useSessionResourceTreeNavigation.ts` 提供（Gateway 工作区导航链 `performWorkspaceDrop` 与会话目录链 `performSessionDrop` 状态机共享、提交与回滚互不渗透），右键菜单与对话框由 `SessionResourceOverlays.tsx` 提供，拖放决策纯函数位于 `sessionResourceDrag.ts`。

**待归位（下一阶段输入）**：本组件实际渲染的是左侧侧边栏的 Gateway 工作区导航树（工作区激活、父子关系、启停、Gateway 重连、连接管理入口），按 AGENTS.md 应属 Gateway 层级，与会话目录树的会话层级归属交叉；两条层级目前共处同一文件与同一渲染入口，尚未拆分。

# 可修改内容

- Agent Sessions 列表、工作区分组和侧栏局部交互组件。
- 仅服务于 AgentSessionsPanel 的小型展示组件和类型。

# 不可修改内容

- 不实现后端 API 协议或会话状态权威逻辑。
- 不在本目录处理全局布局、资源预览或 Composer 行为。

# 规范

- 组件职责要窄，复杂排序、筛选和持久化逻辑优先留在 state/hooks 或上层协调组件。
- 用户可见文案使用中文，专业术语除外。
