# 目录用途

存放浏览器前端 SessionThread 历史的 bootstrap、detail 水合和向前分页副作用，以及右侧 child 对话的精确 history/message 请求编排。

# 可修改内容

- Session Turn bootstrap 的 generation、取消和 partial 轮询。
- 可视 Turn detail 批量水合及旧页分页加载。
- 聚焦 hook 之间的轻量编排。
- `useChildThreadConversation.ts` 编排选定 child 的冷历史、execution 报告续读、直接消息、独立草稿及迟到响应隔离；消息准入事实只来自后端，历史仍调用唯一 API reader。

# 不可修改内容

- 不定义后端 Turn 协议的权威类型。
- 不实现 JSX、会话存储规则或 Gateway 路由。
- 不把 child 内容写入 main 时间线，不伪造 Turn、执行完成或驻留状态。

# 规范

- bootstrap、detail 和 page 保持独立副作用边界。
- 状态变换复用 `state/session/turnTimeline.ts` 的纯函数。
- 请求失败必须写入业务状态或继续抛出，不得静默吞掉。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
