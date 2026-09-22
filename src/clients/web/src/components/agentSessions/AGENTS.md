# 目录用途

存放 Agent Sessions 面板的子组件，负责会话按钮、工作区会话分组、会话资源浏览器及其上下文菜单。

`SessionResourceExplorer.tsx` 只负责会话资源树与搜索结果的渲染；导航层级派生、拖拽/放置状态机和拖放提交由 `useSessionResourceTreeNavigation.ts` 提供，右键菜单与对话框由 `SessionResourceOverlays.tsx` 提供，拖放决策纯函数位于 `sessionResourceDrag.ts`。

# 可修改内容

- Agent Sessions 列表、工作区分组和侧栏局部交互组件。
- 仅服务于 AgentSessionsPanel 的小型展示组件和类型。

# 不可修改内容

- 不实现后端 API 协议或会话状态权威逻辑。
- 不在本目录处理全局布局、资源预览或 Composer 行为。

# 规范

- 组件职责要窄，复杂排序、筛选和持久化逻辑优先留在 state/hooks 或上层协调组件。
- 用户可见文案使用中文，专业术语除外。
