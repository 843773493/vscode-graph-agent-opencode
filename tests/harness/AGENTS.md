# 目录用途

存放 JavaScript 与 Python 测试运行上下文，以及客户端辅助服务的进程装具。

```text
harness/
├── js/
│   └── run-context.mjs     JS 输出路径、结构化结果与资源清理
└── python/
    ├── run_context.py        Python 输出路径、节点隔离、结果与资源清理
    ├── browser_manager.py    Browser 辅助服务及其前端的进程装具
    ├── terminal_backend.py   Terminal 后端的进程装具
    ├── terminal_frontend.py  Terminal attach 前端的进程装具
    └── process_output.py     辅助服务共用的日志输出载体
```

这里组织运行上下文和进程生命周期；端口分配、工作区模板准备及跨测试复用的协议辅助函数位于 `tests/support/`，客户端页面动作位于 `tests/clients/drivers/`。

测试上下文镜像测试文件生成正式 `output_root`，并在其下按测试运行 ID 和 xdist worker 分隔 `runs/<run-id>/`。pytest 用例与其启动的 JS 子进程继承当前用例 ID，将产物隔离到 `runs/<run-id>/nodes/<node-id>/artifacts/`；独立 JS 测试脚本使用 `runs/<run-id>/artifacts/`。节点 runtime 沿用 `runtime/<run-id>/node-<id>/`。共享测试工作区与 Gateway fixture 使用的 `boxteam-home/` 仍位于 `output_root/`。按节点隔离的 Python `BOXTEAM_HOME` 位于对应 runtime 节点目录。进程装具管理所启动的服务，输出载体只负责日志描述符。业务工作区可以嵌套或有多个，但辅助服务日志仍由调用方显式传入所属用例的 `artifacts_dir`，不能从工作区深度推导归属。

```text
out/tests/<test-id>/
├── workspace/                         测试文件固定的隔离工作区
├── boxteam-home/                      Gateway 集成 fixture 的共享控制面
├── runtime/
│   └── <run-id>/node-<node-id>/        按运行和 pytest 节点隔离的运行时
└── runs/<run-id>/                      每次测试命令独立保留
    ├── artifacts/                     独立 JS 测试脚本产物
    └── nodes/<node-id>/artifacts/      pytest 用例与子进程产物
```

## 可修改内容

- 通用运行上下文、资源所有权、清理和结构化结果实现。

## 不可修改内容

- 不要放业务场景和产品断言。
- 不要在真实依赖失败时启动替身作为降级。

## 规范

- 每个资源必须有明确所有者并按逆序确定性清理。
- 关闭时移交并清空已登记的回调，尝试清理全部资源；清理被中断或失败时，汇总原始错误并保留资源标签或 cause。重复关闭不重试已执行的回调，关闭后拒绝登记新资源。
- `UNMET_PREREQUISITE`、失败和通过必须是不同状态。
- 输出路径必须由测试文件路径显式推导。
- Python 临时目录和 `BOXTEAM_HOME` 必须同时按测试运行 ID 与节点 ID 隔离，不得使用会被并发 pytest 进程清理的共享系统目录。
