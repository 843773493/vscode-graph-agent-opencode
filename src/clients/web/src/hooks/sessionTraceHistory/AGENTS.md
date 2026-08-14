# 目录用途

存放事件诊断视图专用的 Session Trace 历史按需加载与向前分页逻辑。

# 可修改内容

- Trace 尾页和 opaque cursor 旧页请求。
- 按 workspace/session scope 隔离的 generation、取消和错误状态。

# 不可修改内容

- 不向主聊天 Turn timeline、live Trace 镜像或 pending conversation 写入历史事件。
- 不连接实时 SSE；实时事件仍由 `useSessionEventStream` 负责。

# 规范

- 只有事件视图处于 active 时才能发起历史请求。
- 会话切换和乱序响应必须由 generation 丢弃。
- 游标失效与网络错误必须透明展示。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
