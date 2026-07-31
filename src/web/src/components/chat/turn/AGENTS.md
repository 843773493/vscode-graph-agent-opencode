# 目录用途

存放单个 Chat Turn 的聚焦展示与局部交互模块，由上层 `ChatTurn` 负责组合。

# 可修改内容

- 用户消息编辑、pending 操作和 replay 确认的局部状态与组件。
- Assistant response body 的纯展示组件和展示派生。

# 不可修改内容

- 不发起会话历史、SSE 或其他后端请求。
- 不解析原始 Trace 协议，不维护跨 Turn 的业务状态。

# 规范

- 局部 hook 只管理单个 Turn 的交互状态。
- Response body 保持纯组件，复杂聚合复用 `state/` 纯函数。
- 用户可见文案使用中文，专业术语除外。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
