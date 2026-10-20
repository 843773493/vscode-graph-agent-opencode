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

先按被管理的资源划分启动和清理，再把确实共用的输出载体放到 `process_output.py`；它不接管就绪、端口或进程清理，避免不同资源互相导入启动装具。

## 可修改内容

- fixture 与资源所有权实现。

## 不可修改内容

- 不要放具体产品断言和浏览器动作脚本。

## 规范

- 依赖注入统一使用 pytest fixture；失败不得回退到替身。
- 启动前检查端口占用，冲突时明确失败；退出只终止持有的进程句柄，不按端口杀未知监听进程。
- 多进程装具在每个进程创建成功后立即登记清理，后续启动失败或被中断时也要释放已持有的资源。
- Browser、Terminal 进程统一通过 `process_output.py::spawn_logged_process` 将 stdout/stderr 写入独立且不可覆盖的日志，不接无人读取的 PIPE。子进程继承日志描述符后，父进程关闭自己的副本；打开日志或创建进程失败也必须关闭已打开的文件。就绪失败保留原异常并附日志路径，仍按已有句柄回收全部进程。
- 所有辅助服务启动装具与输出载体都必须接收显式 `artifacts_dir`，由调用测试传入 `TestRunContext.artifacts_dir` 等正式产物根；不根据业务工作区父目录猜路径。嵌套或多个工作区共用所属测试的产物根，日志由进程前缀和唯一后缀区分，不在工作区树里另建日志目录。
