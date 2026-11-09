# 目录用途

`server/` 实现 Debug 插件的受管运行入口、Node 目标生命周期与 Inspector 消息转发。

# 可修改内容

- 可以维护 `backend.js`、运行时协议验证和本目录对应的测试。
- 可以调用共享的插件管理、日志、请求解析与关闭模块。

# 不可修改内容

- 不在此处维护 Backend 权威 Debug 状态或 Agent 工具业务。
- 不依赖 Gateway 内部状态、Backend Python 模块或 Workbench 实现。
- 不通过未登记的 HTTP 路由或任意方法调用扩展能力。

# 规范

- 使用 ESM；只监听运行时分配的 loopback 端口。
- 只接受 Gateway 转发的 `/debug-runtime` WebSocket，并验证本机运行时凭据及显式 workspace 身份。
- WebSocket 断开仅释放传输句柄；插件关闭时逐个停止并核实自己持有的进程。
