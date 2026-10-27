# 目录用途

验证测试运行设施自身的跨模块、真实子进程和本地传输行为，包括进程归属、端口冲突及退出清理。

```text
harness/
├── test_browser_manager_addressing.py       Browser 监听、访问 URL 与端口冲突接线
├── test_boundary_display_fixture_lifetime.py 历史边界展示装具的准备及清理失败回收
├── test_catalog_fixture_lifetime.py          Catalog 根路径、owner 复用与隔离释放
├── test_gateway_launcher_control_endpoint.py 原生控制端点的监听、身份与清理
├── test_gateway_readiness.py                 Gateway 就绪探针的认证与 HTTP 拒绝
├── test_inprocess_model_replay.py            同一事件循环的 owner 重建与真实 SDK client 隔离
├── test_manual_browser_fixture_lifetime.py   手动连接装具的 Chromium/context/page 逆序释放
├── test_matrix_process_cleanup.py            矩阵退出、中断、宽限到期与隔离进程组回收
├── test_native_command_guard.py              共用原生命令监督器的标准流、期限、内存与后代回收
├── test_fork_watchdog_process_tree.py         真实矩阵内看门狗到期、控制错误与原生进程树核实
├── test_model_http_assets.py                 模型 SDK 的真实 HTTP 服务、请求与响应验证
├── test_model_metadata_refresh.py            HTTP 模型目录延迟出现后的采用与离线恢复
├── test_outbox_fixture_lifetime.py            静态 HTTP/Chromium 装具准备及清理失败回收
├── test_posix_gateway_target.py              POSIX Gateway 启动、健康探测与进程清理
├── test_process_ownership.py                 进程归属、端口替换、静态服务绑定与回收
├── test_run_task_source_snapshot.py           源码快照、profile 基线与进程组回收
├── test_windows_gateway_target.py             原生 PowerShell Gateway 启动、健康探测与进程树清理
├── test_windows_target_activation.py           Windows 目标激活生命周期
├── test_windows_target_process_guard.py        Windows Job Object 进程树保护
├── test_windows_target_stop.py                 Windows 目标停止与进程回收
├── test_workspace_readiness.py                工作区就绪探针的身份与 HTTP 状态
├── dev_gateway_readiness.test.mjs              外层 Gateway 等待的 HTTP 期限与 manager 身份
├── dev_service_readiness.test.mjs              开发服务 HTTP 与 detached 就绪记录观察预算
└── e2e_control_client.test.mjs                 JS 控制端口的 TCP 分块、回执与并发隔离
```

浏览器场景保留在`clients/web/`；这里通过准备边界故障验证它的真实句柄生命周期，不重写业务场景。先观察被测装具的释放结果，再由探针回收仍持有的资源，不能用探针兜底制造“装具已释放”的结论。

开发 Gateway 等待测试使用真实本地 HTTP、隔离就绪文件和明确的 systemctl 状态替身，覆盖挂起、迟到、重试和 manager 替换；不启动或操作用户的 systemd unit，不作为完整开发栈启动验收。

各服务就绪测试直接调用启动器使用的同一等待模块，使用真实隔离HTTP端点与就绪文件；进程状态和系统校时为显式替身。覆盖挂起请求取消、迟到200拒绝、轮询预算、请求头、请求期间进程退出及未消费正文释放；detached记录核对PID、部分JSON和退出诊断。缩短的是本用例的观察预算，生产默认90/120秒保持；不启动开发栈或操作已有服务，不以Linux上的可移植API测试代替Windows/macOS原生验收。

辅助静态服务绑定测试从完整fixture工作区启动真实Browser/Terminal attach HTTP入口，分别核对IPv4/IPv6的健康、配置和首页，并由fixture释放进程。它区分Node监听主机与URL authority，不启动业务后端或操作用户服务；宿主无法绑定IPv6回环时明确跳过相应场景，不能报告为IPv6已验证。

Browser地址接线测试通过组合装具启动真实Browser后端和attach前端，覆盖IPv4、IPv6、通配监听与localhost；核对后端PID、工作区身份、前端配置及清理，未创建页面或启动Chromium。占用场景保留真实监听者，先确认装具在创建进程前拒绝，再检查原端点仍可连接；不能用端口探针成功宣称端口已预留。

JS 控制客户端测试使用本用例创建的 TCP 服务替身，覆盖跨 UTF-8 字节的分块、合法假值、错误回执、不完整回执及并发请求。它验证传输合同，不调用后端进程控制或启动 fork watchdog，也不把真实 TCP 当成完整业务验收。

无效 JSON、空行和不可读取的 `null` 回执在独立 Node 子进程中验证：异常必须回到原 Promise 的等待方，保留原类型与解析栈；旧版回调崩溃由子进程退出码呈现，不连带终止测试运行器。TCP 服务只属于该子进程，结束时关闭全部连接和监听。

矩阵清理测试从真实Bun入口启动有固定寿命的Python父子进程；后代明确忽略TERM，另有一个独立对照进程组。先记录runner退出后的实际状态，再由fixture兜底清理，不能用兜底制造产品已回收的结论。Linux用`/proc`区分仍执行和僵尸；PID回收属于其父进程，不把僵尸当作仍运行的程序。该组不启动产品服务，不证明Windows/macOS原生进程树行为。

`test_model_http_assets.py` 持有共享模型资产的真实 HTTP 服务、监听线程及请求记录。它独立验证 Chat/Responses 同步和异步 SDK、认证、错误与非流式 JSON；启动和释放失败的资源归属由对应 harness 单测验证。业务场景使用现有精确回放，不反向依赖业务测试的模型服务。

`test_windows_gateway_target.py` 只在原生 Windows 上运行。它在本测试的 `out/tests/integration/harness/test_windows_gateway_target/` 下创建临时仓库、fake Uvicorn 和独立 HTTP 端口，真实执行生成的 PowerShell 启动、健康探测与进程树清理命令；不连接 SSH 目标、不清理共享端口，也不启动用户开发 profile。Linux 上的跳过结果不能作为 Windows 原生验证证据。

`test_posix_gateway_target.py` 在 Linux/macOS 上经本机 `sh` 执行与远端相同的 POSIX Gateway 启动、健康探测和清理命令；用路径含空格及单引号的隔离仓库核对配置复制、运行 manifest、环境变量和真实 HTTP 健康响应。进程回收另用本测试创建的 Python 子进程核实 TERM 清理与僵尸态识别；不连接 SSH 目标，fixture 最后仍负责回收本用例进程。

## 可修改内容

- 真实进程或本地网络边界组成的 harness 集成测试与专用 fixture。

## 不可修改内容

- 不放纯 mock 的单元测试；它们归 `tests/unit/harness/`。
- 不把 harness 子进程测试作为真实产品用户流程的 E2E 证据。

## 规范

- 通过 pytest fixture 注入依赖，所有测试子进程由创建者确定性回收。
- 就绪HTTP替身在socket绑定后立即登记关闭，线程成功启动后再登记停止和等待；任一释放失败或中断仍尝试其余动作，不对未启动线程调用shutdown/join。HTTP认证、身份和状态断言仍沿真实传输入口验证。
- 只操作本用例创建的进程；端口被占用时保留原所有者并明确失败。
- 归属验证中自建的 listener/idle 子进程创建后立即交给 fixture 的 ExitStack，覆盖就绪中断及后续进程启动失败；该栈与被测嵌套 process scope 分开，防止把替代端口所有者也算成被测进程。
- 高输出接线用例可用真实 HTTP 子进程替换被启动的程序，但保留装具实际输出载体和就绪路径；它证明 Browser、Terminal 装具日志不会阻塞，不证明相应产品行为。
- 辅助服务高输出接线同时使用嵌套工作区和显式正式产物根，验证日志归属跟随调用测试，不随工作区深度改变；同一测试的多个服务共用产物根并以唯一日志名区分。
- 工作区、日志和运行数据写入镜像路径 `out/tests/integration/harness/<测试名>/`。
