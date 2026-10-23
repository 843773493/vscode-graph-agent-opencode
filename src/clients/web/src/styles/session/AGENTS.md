# 目录用途

存放会话、对话及 Agent 活动区域的样式。

```text
session/
├── agentSessionsPanel.css  # Agent 会话列表
├── agentState.css          # Agent 状态查看
├── chatMessages.css        # 对话消息
├── childThreadPanel.css    # 子线程会话展示
├── eventQueue.css          # 会话事件队列
├── requestLog.css          # 会话模型请求记录
└── toolControl.css         # Composer 工具选择
```

# 可修改内容

- 会话列表、消息、子线程、事件和工具选择相关样式。

# 不可修改内容

- 不实现会话业务状态或请求协议。
- 不放置工作区管理、Gateway 控制面或全局主题样式。

# 规范

- 样式只负责视图呈现；业务状态与数据归属由对应组件和状态层维护。
- 新增文件使用能对应组件或会话区域的名称。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
