# 目录用途

存放浏览器前端 Session Turn 历史的 bootstrap、detail 水合和向前分页副作用。

# 可修改内容

- Session Turn bootstrap 的 generation、取消和 partial 轮询。
- 可视 Turn detail 批量水合及旧页分页加载。
- 聚焦 hook 之间的轻量编排。

# 不可修改内容

- 不定义后端 Turn 协议的权威类型。
- 不实现 JSX、会话存储规则或 Gateway 路由。

# 规范

- bootstrap、detail 和 page 保持独立副作用边界。
- 状态变换复用 `state/session/turnTimeline.ts` 的纯函数。
- 请求失败必须写入业务状态或继续抛出，不得静默吞掉。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
