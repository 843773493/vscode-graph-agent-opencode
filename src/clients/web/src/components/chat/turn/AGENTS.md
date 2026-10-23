# 目录用途

存放单个 Chat Turn 的组合、展示和局部交互；`ChatTurn` 是本目录入口，由 `ChatPanel` 按 transcript 项调用。

```text
turn/
├── ChatTurn.tsx                 # 单轮组件入口与 memo 比较
├── ChatTurnErrorBoundary.tsx    # 单轮渲染错误隔离
├── ChatTurnUserActions.tsx      # 用户消息、附件和单轮动作
├── ProgressiveUserMessage.tsx   # 大段用户输入的渐进展示
├── MessageAttachments.tsx       # 单轮附件展示
├── ChatTurnResponseBody.tsx     # Assistant response 组合
├── TurnActivitySummary.tsx      # 工具与执行活动摘要
├── responseStatusParts.tsx      # response 状态片段
├── activityRenderers.tsx        # 活动类型到展示组件的映射
├── useChatTurnActions.ts        # 单轮动作编排
├── types.ts                     # 单轮 handlers 与载入回调
├── chatTurnTestFixtures.ts      # 每个用例独立创建的会话投影
└── ChatTurn.*.test.tsx          # memo、response、历史详情和用户动作
```

组件测试与源码并列；ChatTurn 测试按职责拆分并共享 fixture；跨多个展示区域的渐进渲染测试放在父目录 `chat/`。

# 可修改内容

- 用户消息编辑、pending 操作和 replay 确认的局部状态与组件。
- Assistant response body 的纯展示组件和展示派生。
- 单轮附件及逐步展开的大段用户消息。

# 不可修改内容

- 不发起会话历史、SSE 或其他后端请求。
- 不解析原始 Trace 协议，不维护跨 Turn 的业务状态。

# 规范

- 局部 hook 只管理单个 Turn 的交互状态。
- 不把跨 Turn 的 transcript、虚拟滚动或历史分页移入本目录。
- Response body 保持纯组件，复杂聚合复用 `state/` 纯函数。
- 用户可见文案使用中文，专业术语除外。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
