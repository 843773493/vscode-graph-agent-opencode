# 目录用途

`components/chat/` 存放主对话视图的 turn 和 response part 组件，对齐 Copilot Chat 的连续消息展示方式。

# 可修改内容

- 用户气泡、Assistant 回复、Thinking、Tool 和 Markdown 展示组件。
- 仅与消息展示直接相关的局部交互状态。

# 不可修改内容

- 不在本目录实现 SSE、HTTP 请求或后端业务规则。
- 不在组件中直接解析原始 trace 协议。

# 规范

- 组件保持单一职责，数据聚合使用 `state/` 已有纯函数。
- 用户可见文案使用中文，专业术语除外。
- 交互控件必须提供可访问名称和明确状态。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
