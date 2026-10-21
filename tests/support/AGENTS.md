# 目录用途

存放跨测试层复用的测试基础设施，包括路径、端口、隔离工作区、进程生命周期、协议桩、API 等待器和断言辅助函数；本目录不放测试用例。

以下职责按边界组织，跨业务共享的替身进入对应子目录：

```text
support/
├── e2e_control/          pytest 外置控制面与受控真实 E2E 服务组合
│   ├── harness.py        时钟、phase、identity、进程租约与回放计划
│   ├── backend_app.py    使用受控 RuntimeClockPorts/identity 的真实 Backend
│   ├── gateway_app.py    启动上述 Backend 的真实 Gateway
│   ├── mcp_stdio_server.py 真实 stdio MCP 服务
│   └── browser_client.mjs 浏览器控制客户端与 fork deadline watcher
├── resource_owners/      资源 owner 测试装具与显式传输/准入替身
│   ├── sessions.py      Terminal/Browser 共用会话前提
│   ├── terminal.py      Terminal owner 装具
│   └── browser.py       Browser owner 装具
├── process_scope.py     进程句柄登记与作用域清理
├── processes.py         Workspace 后端及普通进程就绪辅助
├── gateway_processes.py Gateway 测试进程与配置准备
├── paths.py             正式输出路径
├── ports.py             按测试文件隔离的端口块
└── workspaces.py        只读模板复制与测试工作区准备
```

先区分“真实进程装具”和“业务依赖替身”，再按共享前提与业务消费者分层；不要让 Browser 等无关业务为了取共用身份而导入 Terminal 装具。上树只展示这些边界，其他协议辅助文件仍按各自名称定位。

## 可修改内容

- 无业务语义的确定性测试资源管理与断言辅助代码。
- 被多个 E2E 文件或多个测试层复用的 fake、stub 和 fixture 实现。

## 不可修改内容

- 不放 `test_*.py` 测试用例或产品业务实现。
- 不把客户端、Gateway 或 Agent 特有业务规则伪装成通用 helper。
- 不在本目录写入运行产物或修改 `asset/` 模板。

## 规范

- helper 必须按单一职责命名，禁止重新形成 `utils.py` 杂物箱。
- 正式测试产物路径由调用测试决定并镜像 `tests/` 路径，本目录不得改变其规则。
- 进程和外部资源必须由创建者明确清理，失败时暴露完整错误。
- Workspace和Gateway启动装具在创建进程前持有日志文件，任何文件打开或创建进程失败都要立即关闭已打开的文件；创建成功后由返回的进程句柄接管，关闭进程时一并关闭日志。
- 关闭进程和各日志是独立释放动作；一个释放失败或中断时仍尝试其余动作，再用BaseExceptionGroup保留全部原始错误。全部释放成功后再注销进程句柄。
- `process_scope.py` 按句柄身份登记进程归属，嵌套 scope 退出后恢复外层归属；即使某个回收动作抛出中断，也先尝试回收其余已登记进程，再汇总原始错误。带标签的通用测试资源由 `tests/harness/` 的运行上下文管理。
- 只检查 HTTP 200 的就绪等待统一由 `processes.py::wait_for_http_ok` 提供，Gateway探针通过它透传本地认证头；期限是总等待预算，连接与重试间隔使用剩余预算。HTTP 非200及进程提前退出直接暴露，不重试错误路由或鉴权拒绝。需要校验工作区身份、响应正文的后端探针保留专用合同，不降为仅检查200。
- 就绪等待发现进程退出后，错误以已观察的退出码和目标为主；输出采集只使用剩余预算，采集超时保留已捕获内容及原始 cause，不能掩盖退出事实。就绪探针不越权终止仍持有管道的其他进程，清理由创建者负责。
- `session_navigation_http.py` 的30秒等待覆盖实际HTTP请求、轮询间隔和可选的删除settlement；期限只属于本次观察，不取消后端导航操作。传输层异常和调用方取消原样传播，不能翻译为operation超时；提交入口返回的是operation ID，等待入口返回终态回执。
- `wait_for_backend_ready` 专门核对响应正文的工作区 UUID，连接和重试也受剩余预算约束；HTTP拒绝保留有界诊断正文并关闭响应，不把身份不符或异常状态轮询至超时。
- `session_catalog_owner.py` 只管理测试 catalog 的连接生命周期；登记和定向释放使用同一绝对路径，避免相对路径与绝对路径形成两个 owner。关闭发生中断时仍尝试释放全部选中组件，再汇总原始异常；生产 catalog 装配不依赖此替身。
- `workspace_tool_runtime.py` 的装具退出清理在原 admission 内依次重置调用/作用域绑定、关闭执行作用域、释放浏览器与终端；任一步骤失败或取消仍尝试剩余动作。退出应用 lifespan 后分别关闭活动库和 catalog；准备失败也走该外层清理，多个原始错误由标准退出栈的异常链保留，不另建业务关闭入口。
