# 目录用途

存放 Launcher 管理 Gateway generation 的启动契约、公开代理监听、handoff 控制和生命周期监督。

```text
gateway/
├── startup-contract.mjs   active/pending generation 的环境契约
├── public-listener.mjs    稳定端口代理 HTTP、SSE 与 WebSocket 流
├── server-lifecycle.mjs   本地 HTTP/TCP server 的 listen/close 事件管理
├── handoff-control.mjs    本地 socket 上的 generation 交接协议
└── supervisor.mjs         启动子进程、就绪检查、信号和 generation 生命周期
```

测试放在同目录，通过每个职责模块的公开入口核验状态转换。

# 可修改内容

- Gateway 子进程启动、公开监听和 pending generation handoff。
- 与 Gateway generation 生命周期直接对应的单元测试。

# 不可修改内容

- 不解析 Launcher CLI 参数，不决定配置或 runtime manifest 的来源。
- 不实现 Gateway 工作区路由、会话或 Agent 业务规则。
- 不新增旧模块路径转发层；调用方直接依赖职责模块。

# 规范

- active/pending 启动契约在 `startup-contract.mjs` 唯一维护。
- public listener 只负责流量代理，handoff control 只负责消息传输；generation 切换由 supervisor 决定。
- server 的 listen/close 事件归 `server-lifecycle.mjs` 维护。
- 退出码、超时和连接错误必须传回调用方，不伪报启动或停止成功。
