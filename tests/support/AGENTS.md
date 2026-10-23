# 目录用途

存放跨测试层复用的测试基础设施，包括路径、端口、隔离工作区、进程生命周期、协议桩、API 等待器和断言辅助函数；本目录不放测试用例。

## 按职责查找

下面是职责知识树，叶节点使用相对本目录的实际文件名；按“准备环境 → 持有资源 → 构造前提 → 驱动与观察”查找。`resource_owners/`中的模块有自己的[目录指南](resource_owners/AGENTS.md)。

```text
测试辅助职责
├── 环境、路径与输入
│   ├── paths.py / ports.py                正式输出根、测试文件端口块
│   ├── workspaces.py                      从只读模板准备独立工作区
│   └── custom_tool_test_workspace.py      工具历史装具准备与阶段计时
├── 进程、连接与存储生命周期
│   ├── processes/scope.py                    进程句柄登记及作用域释放
│   ├── processes/workspace.py                Workspace后端、日志、端口预检与HTTP就绪
│   ├── processes/gateway.py                  Gateway进程、配置和就绪
│   ├── gateway_managed_backends.py        Gateway托管后端的显式进程替身
│   └── session_catalog_owner.py           测试catalog连接的登记与释放
├── 会话与历史数据前提
│   ├── canonical_id_at.py / graph_binding.py  确定性身份与图绑定
│   ├── catalog_session_bundle.py          catalog、物理节点和会话控制数据的统一播种
│   ├── job_thread_fixture.py              Job测试的Thread与执行绑定
│   ├── rollout_output.py                  工具输出装具的封存
│   └── web_boundary_seeding.py            Web历史边界场景数据
├── 业务owner装配与显式替身
│   ├── resource_owners/sessions.py        Terminal/Browser共享Session与Thread前提
│   ├── resource_owners/terminal.py        Terminal owner及传输替身
│   ├── resource_owners/browser.py         Browser owner及传输替身
│   ├── workspace_tool_runtime.py          工具运行时准入和多资源释放
│   ├── message_service.py                 绑定权威main thread解析器的MessageService
│   ├── node_debug_dependencies.py         Node调试的会话准入替身
│   └── background_work.py                 可控后台任务handler与注册装配
├── 跨进程控制与场景入口
│   ├── e2e_control/harness.py                pytest控制端口、可控时钟、phase barrier和租约
│   ├── e2e_control/browser_client.mjs        JS控制客户端与独立fork期限watchdog
│   ├── fork_deadline.py                   Python侧fork期限与独立watchdog
│   ├── e2e_control/runtime_ports.py          无应用副作用的显式受控端口
│   ├── e2e_control/backend_app.py            注入测试控制入口的Workspace ASGI
│   ├── e2e_control/tool_observations.py      真实 canonical 工具提交观测
│   ├── e2e_control/communication_backend_app.py  同一受控Backend的通信故障观测组合
│   ├── e2e_control/gateway_app.py            将托管后端绑定到测试ASGI的Gateway入口
│   └── e2e_control/mcp_stdio_server.py       场景所用的MCP服务入口
├── 模型与MCP前提
│   ├── model_stream_config.py            流式模型测试的显式配置准备
│   ├── model_stream_crash_worker.py       模型流故障实验的独立执行入口
│   ├── frozen_provider.py                冻结Provider请求与上下文前提
│   ├── mcp_configuration.py              MCP配置文件准备
│   └── mcp_session_doubles.py             MCP会话及工厂替身
├── 通信目标与故障
│   ├── communication.py                  send/read/wait显式返回值probe
│   ├── communication_federation.py       联邦通信的多节点与SSH装配
│   └── communication_fault_backend.py    通信故障注入后端
└── API等待、观察与断言
    ├── sealed_request_audit.py           Provider日志、真实transport与sealed registry/checkpoint只读核对
    ├── api_helpers.py / api_waiters.py    HTTP建会话、checkpoint样板与Job等待
    ├── session_navigation_http.py        导航operation提交及终态等待
    ├── session_navigation_queue.py       导航catalog intent准备
    ├── gateway_skill_sources.py          Skill发布与接受的显式装配
    ├── skill_gateway_observations.py     Skill Gateway请求、响应观察
    ├── sse.py                            SSE事件读取与匹配
    └── messages.py / text.py / trace.py   消息、文本和Trace断言辅助
```

先区分真实进程、真实业务owner与外部依赖替身，再沿对应前提找到消费者。真实辅助服务的启动和日志移交见[Python harness](../harness/python/AGENTS.md)；装具自身的故障注入见[单元测试](../unit/harness/AGENTS.md)，真实进程与传输组合见[集成测试](../integration/harness/AGENTS.md)。不要让Browser为了共享身份导入Terminal装具，也不要把启动成功、替身返回值和真实用户流程作为同一种证据。

新增helper先找实际持有资源或业务前提的分支，只有多个测试确实共用时才提升到此目录；新增职责不应继续叠到`processes/workspace.py`、`e2e_control/harness.py`或名称宽泛的文件中。物理拆分需要连同全部直接与间接导入一起迁移，按真实依赖实施。

## 控制端口与协议边界

控制状态归 pytest 的 Python harness；JS driver 通过单行 JSON 请求观察或操作已登记的装具。客户端只在当前 UTF-8 分块内找换行，收齐第一条回执后拼接并解析一次，不重复扫描累计前缀；仍须保存完整回执，不能据此声称内存与响应大小无关。控制端口传输与独立 fork watchdog 是不同生命周期，本地传输优化不得改动 fork 的60秒期限。

控制装具的 JSON 租约、事件和模型回放文件统一显式使用 UTF-8，不能依赖宿主默认编码。回放文件读取、临时写入与发布共用此合同；损坏编码直接报错，不尝试本地代码页或替换字符。网络仍传输 UTF-8 字节，文件编码与平台进程身份是不同边界，不能据此声称 Linux 租约已跨平台。

socket 回调不在创建 Promise 时的同步异常边界内：回执解析或读取结果字段失败时，客户端必须关闭自己持有的连接，并用原始异常拒绝该请求，保留错误类型与栈。不能把回调抛错留给进程的未捕获异常处理，也不能用空结果伪装成功；正常远端错误回执仍按原消息拒绝。

## 可修改内容

- 无业务语义的确定性测试资源管理与断言辅助代码。
- 被多个 E2E 文件或多个测试层复用的 fake、stub 和 fixture 实现。

## 不可修改内容

- 不放 `test_*.py` 测试用例或产品业务实现。
- 不把客户端、Gateway 或 Agent 特有业务规则伪装成通用 helper。
- 不在本目录写入运行产物或修改 `asset/` 模板。

## 规范

### 职责与产物归属

- helper 必须按单一职责命名，禁止重新形成 `utils.py` 杂物箱。
- 正式测试产物路径由调用测试决定并镜像 `tests/` 路径，本目录不得改变其规则。
- 进程和外部资源必须由创建者明确清理，失败时暴露完整错误。

### 工作区路径与模板准备

- `processes/workspace.py::resolve_workspace_python_executable`按当前宿主平台选择`.venv`文件：Windows为`Scripts/python.exe`，Linux/macOS为`bin/python`。两个布局并存时不以存在顺序猜平台；当前布局缺失或只是目录时明确报告平台与预期路径，不回退到其他平台残留，也不使用全局Python。
- `paths.py::output_root_for_test`以显式`project_root`解析相对测试文件和相对`tests_root`，未传项目根时才使用当前工作目录；两者不能分别归属不同的根。绝对路径保持原目标，自定义绝对测试根仍可位于项目外；函数只计算镜像路径，不创建目录或要求测试文件已存在。
- `custom_tool_test_workspace.py`中的`FixtureStageObserver`只持有显式profile的JSONL文件，不生成业务事实。打开后首条记录写入或flush失败也要关闭；关闭时摘要失败仍尝试关闭文件，并保留原始异常链。摘要只尝试一次，普通记录仍逐条flush；生成器和累计计数不由清理逻辑重建。

### 进程、端口与HTTP就绪

- `ensure_local_port_available`按显式`host`瞬时检查监听地址，缺省仍为IPv4回环；主机名逐一检查系统解析的地址，IPv6按Node默认双栈语义绑定。不预留端口或声明长期租约。Windows使用独占地址选项，避免REUSEADDR把重复绑定误报为空闲；Linux/macOS保留REUSEADDR处理已关闭连接。探针成功或冲突都关闭自己的socket，不能终止原监听者。
- Workspace和Gateway启动装具在创建进程前持有日志文件，任何文件打开或创建进程失败都要立即关闭已打开的文件；创建成功后由返回的进程句柄接管，关闭进程时一并关闭日志。
- 关闭进程和各日志是独立释放动作；一个释放失败或中断时仍尝试其余动作，再用BaseExceptionGroup保留全部原始错误。全部释放成功后再注销进程句柄。
- `processes/scope.py` 按句柄身份登记进程归属，嵌套 scope 退出后恢复外层归属；即使某个回收动作抛出中断，也先尝试回收其余已登记进程，再汇总原始错误。带标签的通用测试资源由 `tests/harness/` 的运行上下文管理。
- 只检查 HTTP 200 的就绪等待统一由 `processes/workspace.py::wait_for_http_ok` 提供，Gateway探针通过它透传本地认证头；期限是总等待预算，连接与重试间隔使用剩余预算。HTTP 非200及进程提前退出直接暴露，不重试错误路由或鉴权拒绝。需要校验工作区身份、响应正文的后端探针保留专用合同，不降为仅检查200。
- 就绪等待发现进程退出后，错误以已观察的退出码和目标为主；输出采集只使用剩余预算，采集超时保留已捕获内容及原始 cause，不能掩盖退出事实。就绪探针不越权终止仍持有管道的其他进程，清理由创建者负责。
- `wait_for_backend_ready` 专门核对响应正文的工作区 UUID，连接和重试也受剩余预算约束；HTTP拒绝保留有界诊断正文并关闭响应，不把身份不符或异常状态轮询至超时。

### 业务装具的资源释放

- `session_catalog_owner.py` 只管理测试 catalog 的连接生命周期；登记和定向释放使用同一绝对路径，避免相对路径与绝对路径形成两个 owner。关闭发生中断时仍尝试释放全部选中组件，再汇总原始异常；生产 catalog 装配不依赖此替身。
- `workspace_tool_runtime.py` 通过真实消息准入一次性冻结canonical输入和执行队列，领取并启动精确Thread的holder后才执行工具。装具只验证资源工具，不伪造模型完成；准入成功后即登记自身执行的结算责任，准备失败也取消已准入执行，结算未成功须明确报错；退出清理在原 admission 内依次重置调用/作用域绑定、关闭执行作用域、释放浏览器与终端、取消自身执行，最后释放holder。任一步骤失败或取消仍尝试剩余动作。退出应用 lifespan 后分别关闭活动库和 catalog；准备失败也走该外层清理，多个原始错误由标准退出栈的异常链保留，不另建业务关闭入口。

### 跨进程控制与端口租约

- `e2e_control/harness.py` 的控制端口从构造时归 harness 持有，绑定后立即登记关闭；启动准备失败也要释放，不能对未启动线程调用 shutdown。只有目录创建成功后才保存事件，线程启动成功后才登记停机与 join；close 先释放两类 barrier，再执行完整清理，一个动作失败仍尝试其余动作。控制时钟和事件用于观测，不写业务状态或延长业务期限。
- 控制端口的 `read_directory` 只查询已登记 trace 根下的显式目标；检查叶名称时用有关闭作用域的 scandir，命中即结束，不构造父目录名称全集。缺失叶仍返回 null，父目录缺失、断链、文件和越界仍明确失败；真正返回目录内容时继续完整读取并排序。存在性查找与目录内容返回是两种成本，不维护路径缓存或改变注册根归属。
- `E2EProcessLease`的JSON文件声明pytest当前进程使用的服务端口，不持续持有socket或系统文件锁。记录中的`start_time`来自Linux `/proc/<pid>/stat`第22字段，是启动滴答数原文，不能当Unix秒、毫秒或UTC时间；内部读取函数明确命名为`_linux_process_start_ticks`。release用PID、启动标记、随机token共同校验持有者，端口块如何分配归`ports.py`；进程身份、租约归属、端口分配和端口当前空闲是不同事实。
- 新租约文件独占创建成功后立即登记失败回收，写入和关闭均成功才保留；写入中断后可显式重试，不留下半截JSON或尚未取得的租约。独占创建碰到竞争文件时不回收他人的记录；已有损坏记录仍明确报错，不能把新建失败回收变成通用清除。回收自身也失败时保留原始写入异常链。

### 导航操作的等待边界

- `session_navigation_http.py` 的30秒等待覆盖实际HTTP请求、轮询间隔和可选的删除settlement；期限只属于本次观察，不取消后端导航操作。传输层异常和调用方取消原样传播，不能翻译为operation超时；提交入口返回的是operation ID，等待入口返回终态回执。
