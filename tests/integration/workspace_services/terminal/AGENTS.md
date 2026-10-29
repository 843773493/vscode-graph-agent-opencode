# 目录用途

验证 Terminal 辅助服务与真实操作系统进程之间的集成行为，包括信号、进程身份、退出和回收边界。

## 进程释放验证树

```text
terminal/
├── test_process_release.py         Linux session、僵尸、过期身份及SIGTERM中新建后代
├── test_process_release_windows.py Windows taskkill 对真实父子进程树的回收
└── test_operation_receipts.py      真实PTY回执核验、工作区完成与目录占用结算
```

Linux 僵尸断言只描述 Linux 的回收语义；Windows 用原生 `taskkill /T` 终止并分别核对进程根与子进程退出。两类验证使用各自平台的操作系统证据，不以模拟 `taskkill` 或 `/proc` 状态替代原生验证。

Linux的SIGTERM后代场景由真实受控根进程在信号处理期间fork，同session后代忽略SIGTERM；停止返回后独立枚举进程表核对没有仍在执行的成员。测试父进程以subreaper回收自己产生的根与后代，失败收尾只操作本例隔离的进程组，不把该边界测试称为完整PTY或Backend重启验收。

`test_operation_receipts.py`通过正式Session、消息附件上传、Terminal创建及原工具operation生成真实Node/PTY/HTTP状态，明确注入回执版本／核实状态或账本通知／日志故障。读、真实命令写入及workspace完成的错误回执保留精确原lease和目录占用；显式管理停止核实后才结清并允许到期GC。独立终态场景暂停steering轮询，由完成查询独占提交真实外部停止事实，仍保留workspace记录与关联；通知或日志故障不改变已提交的结算，核对冷账本和真实目录回收。suite `integration-terminal-operation-receipts`显式选择原7场景，固定90秒/4GiB；`integration-terminal-lease-notifications`选择通知失败及通知／日志双重故障2场景，固定60秒/4GiB，两组复用同一场景实现并串行运行。支持Linux和Windows，实机结论以各平台原始结果为准；原生PTY以回车提交命令，不替代Gateway/Web、独立Backend重启或完整旧代接管验收。

`test_terminal_shutdown_signals.py`经正式HTTP创建、真实Node/PTY及POSIX信号验证在途创建的关闭观察超时、原任务保留、管理查询、精确拒绝回执、迟到就绪的同链关闭与冷Manager恢复；另核致命错误及关闭期间到达的致命错误仍退出1。`terminal_start_barrier.mjs`是显式测试故障边界，仅延迟真实Worker就绪结果的交付并记录原进程身份，不代填Session或恢复状态。正常Manager退出继续保留PTY，fixture沿生产终止入口仅回收自己记录的精确实例。通过`integration-terminal-shutdown-signals`运行，固定90秒/4GiB；不代表Windows SIGBREAK、Gateway/主Web、全部后代独立核实或整个进程退出的总期限。

`test_terminal_startup_cleanup.py`通过正式HTTP创建真实PTY父子进程，仅在原worker就绪消息交付前注入失败；父子均忽略SIGTERM，要求原owner核实强杀及worker退出后才签创建失败回执。首次停止故障保持created/unknown、原进程与句柄，原HTTP管理kill重试后才核实终态；独立/proc观察、原持久记录和peer资源共同验收。正常日志与原writer真实ENOTDIR均覆盖上述两种停止结果；另核四类cwd拒绝保留not_created回执、零PTY及原状态字节，故障解除后沿同一intent创建和停止真实PTY。`terminal_startup_failure.mjs`只提供显式IPC/信号及日志writer故障边界，不代填资源或进程终态。suite `integration-terminal-startup-cleanup`固定60秒/4GiB，仅证明Linux该窗口，不代表未交付PID的native阻塞、Gateway/Web或Windows。

`test_websocket_input_errors.py`通过真实Node WebSocket/HTTP及PTY验证三类原生路径的JSON/schema/type拒绝、附加前零资源、原授权顺序，以及同连接真实shell写文件、PID保持与detach。`integration-terminal-websocket-input-errors`固定60秒/4GiB；不代表Gateway/Web、Python业务owner、进程重启、Windows实机或其它Terminal运行时错误验收。

`test_terminal_storage_diagnostics.py`经正式HTTP创建真实PTY，移动原状态文件并以目录制造发布冲突，只在原诊断writer制造真实ENOTDIR。核对公开错误、原文件字节、进程身份、已应用尺寸和故障解除后的同尺寸落盘，原删除入口后独立核实PTY/worker退出；故障恢复只归还原文件，不代填状态。suite `integration-terminal-storage-diagnostics`固定60秒/4GiB，不代创建失败窗口、Gateway/Web、Python业务owner、Backend重启或Windows实机。

`test_terminal_exit_diagnostics.py`经正式HTTP创建真实PTY父子，忽略SIGTERM和SIGHUP以排除Worker退出自动挂断的替代证明；仅在真实Worker错误事件和原诊断writer注入故障。核对原进程身份、独立停止观察、原致命处理器开始前已发布的退出记录、退出码1和冷Manager读取，正常日志仍保持服务存活。suite `integration-terminal-exit-diagnostics`固定60秒/4GiB；关键状态只由产品生成，致命事件观察器不处理拒绝或改写产品记录，不代未知停止接管、Gateway/Web、Windows或其它生命周期错误验收。

同文件另在原SIGTERM出口注入一次EPERM，经Worker错误产生exited/unknown及存活PTY父子；同Manager和冷Manager各经原HTTP kill/delete核实停止、持久终态、重复请求及无新执行。不替换身份核验或停止算法，不手填恢复记录；此证据只覆盖已有完整原生身份的无Worker重试，不代未知native无PID、Python operation lease或Windows验收。


## 可修改内容

- Terminal 模块与受控子进程组成的集成测试及专用 fixture。

## 不可修改内容

- 不在此实现产品逻辑或放置纯解析单元测试。
- 不把受控进程测试称为完整终端用户流程的 E2E。

## 规范

- 通过 pytest fixture 创建和回收进程，只操作本用例持有的进程身份。
- Linux `/proc`、session 或僵尸进程的断言明确限定平台。
- 正式产物镜像测试文件路径写入 `out/tests/integration/workspace_services/terminal/`。
