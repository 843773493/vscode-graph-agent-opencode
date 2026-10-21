# 目录用途

集中存放真实 E2E 进程使用的外置控制面与受控组合：pytest 进程拥有测试控制服务，真实 Backend、Gateway、MCP stdio server 和浏览器客户端通过显式地址、token 与环境变量接入。

```text
e2e_control/
├── harness.py             控制服务、进程租约、phase、时钟和精确回放计划
├── backend_app.py         绑定受控 ID、phase 与时钟的真实 Backend ASGI app
├── gateway_app.py         连接受控 Backend ASGI app 的真实 Gateway app
├── mcp_stdio_server.py    接受测试 catalog 的真实 MCP stdio server
└── browser_client.mjs     浏览器端控制客户端与 fork deadline watchdog
```

## 可修改内容

- E2E 控制协议及 pytest 所拥有的时钟、进程租约、phase barrier 和资源观测。
- 绑定控制协议的真实服务组合与浏览器控制客户端。
- 真实外部 MCP stdio 服务的测试专用实现。

## 不可修改内容

- 不放生产应用、普通测试替身或产品业务规则。
- 不让真实服务静默回退到替代服务；控制地址、认证 token 或必要配置缺失时必须明确失败。
- 不因文件迁移保留旧模块路径重导出或兼容 shim。

## 规范

- `harness.py` 是控制状态的唯一权威来源；Backend 和 Gateway 只能通过显式控制协议读取或报告状态。
- 进程租约必须验证 PID 与进程创建时间，端口和输出根由调用测试明确传入。
- E2E 服务组合继续使用真实应用 lifespan、真实传输和独立进程；控制协议只提供确定性时钟、identity 与同步屏障。
- 新文件按运行角色命名，并同步更新 `tests/support/AGENTS.md` 中的树形索引。
