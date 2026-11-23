# 目录用途

存放 Python 测试运行上下文、Terminal 测试进程装具及共享进程输出载体。Workspace FastAPI 后端与 Gateway 进程、端口分配和工作区模板准备位于 `tests/support/`。

`terminal_backend.py` 管 Terminal 插件测试用服务进程；Terminal 和 Browser attach 页面均通过 Gateway 插件运行时提供。工作区运行上下文归 `run_context.py`。新增装具先确定实际管理的资源范围，避免为产品已经托管的插件重复启动测试进程。

```text
python/
├── run_context.py         测试运行上下文及登记资源的生命周期
├── web_workbench.py       正式页面测试的会话级 Workbench 构建及各模块消费收据
├── integration_gate.py    集成/E2E session跨worktree独占运行名额
├── artifacts.py           pytest 节点收据、失败现场及成功运行的临时目录回收
├── retention.py           文件级整次运行保留及共享证据引用回收
├── terminal_backend.py    Terminal 后端的启动及启动失败清理
└── process_output.py      测试进程共用的日志输出
```

先按被管理的资源划分启动和清理，再把确实共用的输出载体放到 `process_output.py`；它负责向当前 process scope 登记创建成功的进程，不接管就绪或端口，避免不同资源互相导入启动装具。

`TestRunContext.from_test_file`先确定显式`project_root`（缺省为cwd），再以它解析相对测试文件；绝对文件路径保持原目标。完成解析后统一校验文件属于该项目的`tests/`并镜像到`out/tests/`。`output_root/workspace/`与Gateway fixture共享的`output_root/boxteam-home/`保留固定位置；pytest 用例按`PYTEST_CURRENT_TEST`中的节点 ID 将产物放到`output_root/runs/<run-id>/nodes/<node-id>/artifacts/`，模块／session 共享装具和独立脚本使用`output_root/runs/<run-id>/artifacts/`，节点运行时与节点级`BOXTEAM_HOME`位于`output_root/runtime/<run-id>/node-<node-id>/`下。不能把显式项目根与启动进程的cwd混作同一基准。

```text
out/tests/<test-id>/
├── workspace/                         固定工作区位置
├── boxteam-home/                      Gateway fixture 的共享控制面
├── runtime/<run-id>/node-<node-id>/    pytest 节点隔离的运行时与全局配置
└── runs/<run-id>/
    ├── artifacts/                      模块／session 共享及独立脚本产物
    └── nodes/<node-id>/artifacts/       pytest 用例与子进程产物
```

## 可修改内容

- fixture 与资源所有权实现。

## 不可修改内容

- 不要放具体产品断言和浏览器动作脚本。

## 规范

- `artifacts.py` 是 pytest 运行现场的 owner：节点 `runs/<run-id>/nodes/<node-id>/run.json` 保存完整 node ID（含参数）、三阶段结果、性能和业务证据路径；文件级 `latest.json`、`latest-failed.json` 只是可重建的收据指针，不能当第二事实源。场景自行写的 `artifacts/result.json` 保持原业务内容。
- 固定 `workspace/`、`boxteam-home/` 继续复用；登记资源关闭后，失败现场整目录改名到对应节点的 `state/`，不复制或在线快照 SQLite。模块／session fixture 尚未结束时延迟保留，收据明确标注 `shared_fixture_end`，不能冒充失败瞬间。参数切换在旧 fixture 退出、新 fixture 重建同路径前完成归档；每次退出清空旧路径登记，避免后续场景误收旧数据。`preserve_test_state` 标记可保留成功现场。
- 节点通过同时要求测试阶段和完整 protocol 成功返回；其它插件在 protocol 收尾抛错时记录原错误、标记未完成并保护现场，不能仅凭三个阶段通过就释放 owner。
- `runtime/workspace-owner.json` 保护已登记的固定现场；异常退出或 teardown 失败后不自动接管、不删除。确认进程已退出后显式归档现场和 owner，才能重新运行。锁文件和 owner 不属于临时目录回收范围。
- pytest session 所有装具退出后，`retention.py` 按测试文件保留最近 2 次已结束运行，额外保留最近 1 次失败运行，去重；同一 run 的所有参数变体、业务证据、性能记录和归档现场整组处理。`preserve_test_state` 文件、运行中、中断、内部错误及清理未完成的现场不自动删除。无本 owner 登记的旧目录不自动认领。
- 成功 runtime 关闭后回收，旧失败 runtime 仅保留最近失败运行的，显式保留和未完成现场除外。共享 runtime 跟随需要它的文件，共享日志等证据等最后一个文件消费者过期后回收；同步更新节点 runtime 状态、session 节点引用和快捷索引。
- session 装具的共享证据必须使用装具所属文件的 context（如 `tests/conftest.py`、`tests/harness/python/web_workbench.py`），不能借用首个测试文件的 run 目录，避免首个文件过期时误删其它消费者需要的证据。
- 直接 pytest 也在 session 开始冻结并导出运行 ID 与 UTC 开始时间，供 Python/JS 子进程继承；退出恢复原环境。纯收集且没有节点或资源时不创建运行收据；全部跳过的文件汇总为 `skipped`。
- 文件 `runs/<run-id>/run.json` 和 `tests/conftest.py` 对应的 `pytest-session.json` 复用 `schema_version: 1`，owner 为 `tests.harness.python.artifacts`；前者登记整次运行、节点和性能目录，后者登记文件引用及共享路径。短临界区使用稳定的 `out/tests/harness/python/artifacts/runtime/retention.lock`，不扫描正文、不删除锁。独立 JS/Python 脚本及矩阵外层采样日志仍归各自 owner，不由 pytest 自动清理。
- 依赖注入统一使用 pytest fixture；失败不得回退到替身。
- Workbench 构建由根 pytest plugin 注册的 session fixture 统一持有，并先取得集成闸门；各模块沿用 `web_workbench_assets`，只共享静态产物，保留各自构建收据与日志。浏览器 profile、端口、工作区与其它可写运行数据必须留在调用方的节点目录，不得写入共享产物或其相邻目录；新 pytest 会话重新构建，不读取上轮产物。
- 共享构建复用 `app/runtime` 的平台命令监督器，设置180秒期限；POSIX限制每进程数据段4GiB，Windows限制Job总内存4GiB。正常退出、构建失败、超时和调用方中断都回收构建树。收据同时保留原构建命令、监督命令及退出码；不得用只终止根进程的超时替代监督器。
- 启动前检查端口占用，冲突时明确失败；退出只终止持有的进程句柄，不按端口杀未知监听进程。
- 多进程装具在每个进程创建成功后立即登记清理，后续启动失败或被中断时也要释放已持有的资源。
- `spawn_logged_process`在父进程日志副本关闭后、返回和等待就绪前向同一 process scope 登记；正常停止复用 `terminate_process` 解除登记，不由组合装具重复登记。
- Terminal 独立服务装具显式传入 `BOXTEAM_PROJECT_ROOT`，工作区路径与进程 cwd 不作为发行资源根。Browser 插件只通过 `GatewayPluginRuntime` 启动，并沿插件实例 HTTP/WS 路由访问；不直接启动 Browser 后端进程。
- Terminal 独立服务进程通过 `process_output.py::spawn_logged_process` 将 stdout/stderr 写入专用日志，不接无人读取的 PIPE。子进程继承日志描述符后，父进程关闭自己的副本；启动或关闭失败复用已有句柄清理，不另建进程 owner。
- 所有辅助服务启动装具与输出载体都必须接收显式 `artifacts_dir`，由调用测试传入 `TestRunContext.artifacts_dir` 等正式产物根；不根据业务工作区父目录猜路径。嵌套或多个工作区共用所属测试的产物根，日志由进程前缀和唯一后缀区分，不在工作区树里另建日志目录。
