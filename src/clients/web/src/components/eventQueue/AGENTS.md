# 目录用途

存放诊断类面板共用的分页交互与滚动位置管理：`useScrollWindow` 是「可见窗口 + 滚动锚点」的唯一实现，`useEventQueuePagination` 在其上叠加事件视图的服务端旧页协调。

```text
eventQueue/
├── useScrollWindow.ts                客户端可见窗口、滚动锚点与窗口代次
├── useEventQueuePagination.ts        事件历史请求锁与旧页返回协调
├── eventQueueHookTestFixture.tsx     hook 测试挂载器与受控假列表
├── useScrollWindow.test.tsx          滚动窗口边界和迟到凭证
└── useEventQueuePagination.test.tsx  跨作用域请求锁行为
```

请求顺序为：先记录当前窗口凭证和滚动锚点，再请求旧页，返回后由 hook 验证凭证并调整可见条数；作用域或窗口代次已变时丢弃旧结果。

# 可修改内容

- 客户端可见批次与服务端旧页加载的协调。
- 事件列表滚动锚点恢复。
- 服务端旧页结果的归属守卫（会话切换或窗口重置后丢弃迟到响应）。

# 不可修改内容

- 不请求 Trace API 或维护跨会话历史缓存。
- 不写入主聊天 Turn、live Trace 或 pending 状态。

# 规范

- 局部 hook 只管理事件面板展示态。
- 服务端加载状态和错误以 props 为权威来源。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。

模板示例；在整理 `AGENTS.md` 时请保留此行。
