# 目录用途

`components/chat/` 存放主对话视图的逐轮消息、transcript 展示和待处理消息界面。按状态作用范围分层：单个 turn 放在 `turn/`，待处理队列放在 `pending/`，跨 turn 的列表、定位和共享展示留在当前目录。

```text
chat/
├── pending/                 # Composer 队列与单轮待处理策略控件
├── turn/                    # 单轮消息组合、用户动作和 Assistant response
├── ChatHistory*             # 历史加载、空状态和投影提示
├── transcriptItems.ts       # 把会话投影整理成可虚拟化的 transcript 项
├── useTurnVirtualScroller.ts # 跨 turn 的窗口化渲染与滚动锚点
├── useTurnAnchorNavigation.ts
├── InternalExecutionTranscriptItem.tsx
└── MarkdownContent.tsx、ThinkingSection.tsx、ToolRow.tsx 等共享展示组件
```

测试与被测组件同目录；跨多个展示组件的组合测试留在 `chat/` 根目录。

# 可修改内容

- `turn/` 中单轮用户气泡、Assistant 回复及局部交互。
- `pending/` 中待处理消息队列与投递策略展示。
- 当前目录中的 transcript、滚动定位、Thinking、Tool 和 Markdown 展示。

# 不可修改内容

- 不在本目录实现 SSE、HTTP 请求或后端业务规则。
- 不在组件中直接解析原始 trace 协议。

# 规范

- 组件保持单一职责，数据聚合使用 `state/` 已有纯函数。
- 单轮专属组件放入 `turn/`；跨轮列表和定位逻辑留在 `chat/` 根目录；待处理队列控件放入 `pending/`。
- 展示组件接收上层提供的数据与回调，不直接发起请求或维护业务状态。
- 用户可见文案使用中文，专业术语除外。
- 交互控件必须提供可访问名称和明确状态。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
