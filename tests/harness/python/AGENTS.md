# 目录用途

存放 Python 测试运行上下文和 Browser、Terminal 辅助服务的进程装具。Workspace FastAPI 后端与 Gateway 进程、端口分配和工作区模板准备位于 `tests/support/`。

文件按启动的资源命名：`terminal_backend.py` 管 Terminal 后端，`terminal_frontend.py` 管 Terminal attach 网页进程，`browser_manager.py` 管 Browser 后端及其 attach 前端；工作区运行上下文归 `run_context.py`。新增装具先确定实际管理的资源范围，避免用 `manager` 泛指单个前端或协议辅助函数。

```text
python/
├── run_context.py         测试运行上下文及登记资源的生命周期
├── browser_manager.py     Browser 后端、attach 前端的启动与清理
├── terminal_backend.py    Terminal 后端的启动及启动失败清理
├── terminal_frontend.py   Terminal attach 前端的启动与清理
└── process_output.py      两种辅助服务共用的进程日志输出
```

先按被管理的资源划分启动和清理，再把确实共用的输出载体放到 `process_output.py`；它不接管就绪、端口或移交成功后的进程清理，避免不同资源互相导入启动装具。

`TestRunContext.from_test_file`先确定显式`project_root`（缺省为cwd），再以它解析相对测试文件；绝对文件路径保持原目标。完成解析后统一校验文件属于该项目的`tests/`并镜像到`out/tests/`。`output_root/workspace/`与Gateway fixture共享的`output_root/boxteam-home/`保留固定位置；pytest 用例按`PYTEST_CURRENT_TEST`中的节点 ID 将产物放到`output_root/runs/<run-id>/nodes/<node-id>/artifacts/`，独立脚本使用`output_root/runs/<run-id>/artifacts/`，节点运行时与节点级`BOXTEAM_HOME`位于`output_root/runtime/<run-id>/node-<node-id>/`下。不能把显式项目根与启动进程的cwd混作同一基准。

```text
out/tests/<test-id>/
├── workspace/                         固定工作区位置
├── boxteam-home/                      Gateway fixture 的共享控制面
├── runtime/<run-id>/node-<node-id>/    pytest 节点隔离的运行时与全局配置
└── runs/<run-id>/
    ├── artifacts/                      独立脚本产物
    └── nodes/<node-id>/artifacts/       pytest 用例与子进程产物
```

## 可修改内容

- fixture 与资源所有权实现。

## 不可修改内容

- 不要放具体产品断言和浏览器动作脚本。

## 规范

- 依赖注入统一使用 pytest fixture；失败不得回退到替身。
- 启动前检查端口占用，冲突时明确失败；退出只终止持有的进程句柄，不按端口杀未知监听进程。
- Browser组合装具的`backend_host`是监听目标，端口预检使用同一主机及地址族；健康检查和前端配置共用一个后端URL。通配监听地址`0.0.0.0/::`分别通过`127.0.0.1/::1`访问，IPv6方括号只用于URL，不传给Node的`--host`。
- 多进程装具在每个进程创建成功后立即登记清理，后续启动失败或被中断时也要释放已持有的资源。
- Browser、Terminal 后端的启动环境显式传入 `BOXTEAM_PROJECT_ROOT`，工作区路径与进程 cwd 不作为发行资源根。
- Browser、Terminal 进程统一通过 `process_output.py::spawn_logged_process` 将 stdout/stderr 写入独立且不可覆盖的日志，不接无人读取的 PIPE。子进程继承日志描述符后，父进程关闭自己的副本；打开日志或创建进程失败也必须关闭已打开的文件。就绪失败保留原异常并附日志路径，仍按已有句柄回收全部进程。
- 日志副本全部关闭后，`spawn_logged_process`才把进程句柄移交调用方。创建成功但日志关闭失败或中断时，入口通过既有`terminate_process`回收尚未移交的进程；回收失败保留日志关闭异常链与日志路径。正常返回后仍由调用方管理进程，不能把失败回收扩大为第二套服务清理。
- 所有辅助服务启动装具与输出载体都必须接收显式 `artifacts_dir`，由调用测试传入 `TestRunContext.artifacts_dir` 等正式产物根；不根据业务工作区父目录猜路径。嵌套或多个工作区共用所属测试的产物根，日志由进程前缀和唯一后缀区分，不在工作区树里另建日志目录。
