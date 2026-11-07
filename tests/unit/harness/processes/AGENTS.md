# 目录用途

验证测试进程设施的启动、HTTP 就绪、日志描述符和确定性清理。用 mock 注入创建失败、中断和清理失败，观察被测装具持有的句柄，不启动真实产品服务。

```text
processes/
├── test_workspace_python_executable.py   宿主平台对应的虚拟环境解释器路径
├── test_port_availability.py             三平台端口绑定语义与探针socket释放
├── test_process_log_lifetime.py          Workspace/Gateway进程与日志移交
├── test_process_termination.py           Windows启动器与服务器退出顺序、POSIX原信号语义
├── test_auxiliary_process_lifetime.py    Browser/Terminal辅助进程的准备与清理
├── test_http_readiness.py                HTTP探针预算与失败归属
└── test_*_lifetime.py                    按被管理资源划分的生命周期故障注入
```

解释器路径测试在真实隔离目录中模拟Linux、Darwin和Windows的选择条件，不修改宿主平台、不执行空的候选文件。它证明目录布局选择和错误归属；各平台真实进程启动仍须在对应系统验证，不能把平台参数化等同于Windows或macOS运行验收。

端口探针单测显式模拟三平台socket绑定边界，覆盖Windows允许重复绑定的REUSEADDR语义、显式IPv4/IPv6地址及主机名多地址冲突、异常的原始cause与探针关闭。它不监听真实端口；原监听者继续可用的证据来自integration/harness中的真实进程归属和Browser地址接线测试。

`test_auxiliary_process_lifetime.py`区分日志打开、创建进程、关闭父进程日志副本、移交句柄和服务就绪；日志关闭失败时尚未移交的进程也必须回收。文件关闭、进程创建和终止故障为显式替身，不能用调用记录代替真实子进程退出证据。

`test_http_readiness.py`核对探针的请求预算、状态和失败分类；`test_http_server_fixture_lifetime.py`核对测试端点自身的socket与线程归属，真实HTTP接线留在`tests/integration/harness/`。先区分探针行为与测试服务生命周期，避免用同一套mock冒充传输证据。

公共 HTTP、Gateway 与 Backend 的定向反例须证明监听前零连接，监听和 HTTP 共用原期限；不得通过增加重试或放宽 HTTP 拒绝掩盖端口自连接。

`test_model_replay_fixture_lifetime.py`只检查 HTTP/SDK 专项测试中模型服务的socket、线程和请求诊断文件；用两种模型协议验证启动与清理失败，不启动Gateway、模型执行或辅助服务，也不替代真实关闭验收。

`test_http_stub_server_lifetime.py`检查通用HTTP替身的共用生命周期：三个公共入口都覆盖线程构造、启动、join超时及清理异常。仅注入server/thread替身；协议响应和真实socket释放应通过真实HTTP另验，不由清理调用记录推断。

`test_e2e_control_lifetime.py`核对pytest控制端口从构造到退出的资源归属、两类barrier释放和事件落盘。server/thread均为mock，不运行真实产品，也不改业务时钟、fork期限或控制协议；真实TCP客户端的释放和协议结果另验。

`test_e2e_process_lease_lifetime.py`用真实隔离文件验证租约新建失败回收、关闭失败、竞争文件保留及显式重试，并核对失败和正常退出都释放本次端口绑定。进程启动标记、端口检查和保留 socket 是显式替身；不监听端口、不操作其他进程，真实自动客户端竞争及服务监听由 Integration 单独验证。

## 可修改内容

- 进程 scope、Workspace/Gateway 后端和 Browser/Terminal 辅助服务装具的单元测试。
- 组合装具的资源移交、就绪预算和原始异常保留断言。

## 不可修改内容

- 不放真实子进程、监听端口或浏览器操作；这些验证归 `tests/integration/harness/`。
- 不放工作区配置、catalog 生命周期、业务协议等待器或产品实现。

## 规范

- 通过 pytest fixture 注入依赖；沿用上层 conftest 的输出路径与资源作用域，不另建一套运行上下文。
- 验证进程、stdout、stderr 等独立资源均得到释放；测试兜底前先检查被测装具的清理结果。
- 直接驱动 pytest 生成器装具时使用当前 pytest 的 `_get_wrapped_function()`，显式传入装具依赖；持有 `close`／`throw` 的对象声明为 `Generator`。HTTP线程生命周期驱动 `health_server`，逐次响应配置归 `health_endpoint`，不得混用两者。
- 只验证启动失败的 Web 装具调用方显式传入未消费的资源目录，并阻止意外外部命令；不要伪造构建成功，也不要为生命周期单测构建工作台。产物上下文使用当前单测路径。
- 中断与普通异常都必须保留原始对象；清理失败不能掩盖启动失败，也不能跳过其余资源。
- 产物镜像当前测试路径写入 `out/tests/unit/harness/processes/<测试名>/`；历史结果保留在原路径，不混作新运行证据。

- 日志生命周期接线分别核Gateway显式150秒和Workspace默认10秒；Windows归属等待测试同时核覆盖期限传到原launcher与已捕获server子进程，不以替身冒充原生验收。

- `test_process_stdin_eof.py`验证显式stdin EOF合同、单次总期限、非零退出、超时保留在运行进程和显式force，不把替身当作真实服务已退出的证据。
