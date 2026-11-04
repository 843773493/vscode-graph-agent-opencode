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

`test_terminal_creation_intent.py`复用原真实PTY就绪屏障，在就绪尚未交付时核对先前发布的created/unknown意图，再SIGKILL本fixture的Node并从原磁盘启动新Manager。公开查询必须保留同一身份，缺PID的kill/delete不能伪报核实停止，迟到创建不能替换意图；测试不补写记录或原生身份，不代表无身份接管、Python lease、Windows或Web。正式产物镜像本测试路径；原信号fixture按request.node.path确定输出归属。

原就绪屏障的`shutdowns.jsonl`在Worker真实关闭IPC发送处记录次数与策略；等待同一关闭Promise的重入不算另一份派发。原信号场景仍要求两个Worker各派发一次，不放宽身份、次数或保留PTY的断言。

Linux的SIGTERM后代场景由真实受控根进程在信号处理期间fork，同session后代忽略SIGTERM；停止返回后独立枚举进程表核对没有仍在执行的成员。测试父进程以subreaper回收自己产生的根与后代，失败收尾只操作本例隔离的进程组，不把该边界测试称为完整PTY或Backend重启验收。

`test_operation_receipts.py`通过正式Session、消息附件上传、Terminal创建及原工具operation生成真实Node/PTY/HTTP状态，明确注入回执版本／核实状态或账本通知／日志故障。错误回执保留精确原lease和目录占用；显式管理停止核实后才结清，未修改的工作副本由原附件owner立即释放，真实命令生成的脚本与结果保留。独立终态场景暂停steering轮询，由完成查询独占提交真实外部停止事实，仍保留workspace记录与关联；通知或日志故障不改变已提交的结算，核对冷账本和物理工作副本。suite `integration-terminal-operation-receipts`显式选择原7场景，固定90秒/4GiB；`integration-terminal-lease-notifications`选择通知失败及通知／日志双重故障2场景，固定60秒/4GiB，两组复用同一场景实现并串行运行。支持Linux和Windows，实机结论以各平台原始结果为准；原生PTY以回车提交命令，不替代Gateway/Web、独立Backend重启或完整旧代接管验收。

`test_terminal_shutdown_signals.py`经正式HTTP创建、真实Node/PTY及POSIX信号验证在途创建的关闭观察超时、原任务保留、管理查询、精确拒绝回执、迟到就绪的同链关闭与冷Manager恢复；另核致命错误及关闭期间到达的致命错误仍退出1。`terminal_start_barrier.mjs`是显式测试故障边界，仅延迟真实Worker就绪结果的交付并记录原进程身份，不代填Session或恢复状态。正常Manager退出继续保留PTY，fixture沿生产终止入口仅回收自己记录的精确实例。通过`integration-terminal-shutdown-signals`运行，固定90秒/4GiB；不代表Windows SIGBREAK、Gateway/主Web、全部后代独立核实或整个进程退出的总期限。

`test_terminal_startup_cleanup.py`通过正式HTTP创建真实PTY父子进程，仅在原worker就绪消息交付前注入失败；父子均忽略SIGTERM，要求原owner核实强杀及worker退出后才签创建失败回执。首次停止故障保持created/unknown、原进程与句柄，原HTTP管理kill重试后才核实终态；独立/proc观察、原持久记录和peer资源共同验收。正常日志与原writer真实ENOTDIR均覆盖上述两种停止结果；另核四类cwd拒绝保留not_created回执、零PTY及原状态字节，故障解除后沿同一intent创建和停止真实PTY。`terminal_startup_failure.mjs`只提供显式IPC/信号及日志writer故障边界，不代填资源或进程终态。suite `integration-terminal-startup-cleanup`固定60秒/4GiB，仅证明Linux该窗口，不代表未交付PID的native阻塞、Gateway/Web或Windows。

`test_websocket_input_errors.py`通过真实Node WebSocket/HTTP及PTY验证三类原生路径的JSON/schema/type拒绝、附加前零资源、原授权顺序，以及同连接真实shell写文件、PID保持与detach。`integration-terminal-websocket-input-errors`固定60秒/4GiB；不代表Gateway/Web、Python业务owner、进程重启、Windows实机或其它Terminal运行时错误验收。

`test_terminal_storage_diagnostics.py`经正式HTTP创建真实PTY，移动原状态文件并以目录制造发布冲突，只在原诊断writer制造真实ENOTDIR。核对公开错误、原文件字节、进程身份、已应用尺寸和故障解除后的同尺寸落盘，原删除入口后独立核实PTY/worker退出；故障恢复只归还原文件，不代填状态。suite `integration-terminal-storage-diagnostics`固定60秒/4GiB，不代创建失败窗口、Gateway/Web、Python业务owner、Backend重启或Windows实机。

`test_terminal_exit_diagnostics.py`经正式HTTP创建真实PTY父子，忽略SIGTERM和SIGHUP以排除Worker退出自动挂断的替代证明；仅在真实Worker错误事件和原诊断writer注入故障。核对原进程身份、独立停止观察、原致命处理器开始前已发布的退出记录、退出码1和冷Manager读取，正常日志仍保持服务存活。suite `integration-terminal-exit-diagnostics`固定60秒/4GiB；关键状态只由产品生成，致命事件观察器不处理拒绝或改写产品记录，不代未知停止接管、Gateway/Web、Windows或其它生命周期错误验收。

同文件另在原SIGTERM出口注入一次EPERM，经Worker错误产生exited/unknown及存活PTY父子；同Manager和冷Manager各经原HTTP kill/delete核实停止、持久终态、重复请求及无新执行。不替换身份核验或停止算法，不手填恢复记录；此证据只覆盖已有完整原生身份的无Worker重试，不代未知native无PID、Python operation lease或Windows验收。

自发退出场景仅在原Worker的process.exit和原ChildProcess.kill边界阻止退出，真实PTY经原命令自行退出。核原Worker仍存活且句柄保留，关闭确认到期后持久unknown，移除故障后经原HTTP kill/delete停止同一Worker；另有无故障正常退出回归。正式suite仍为60秒/4GiB，不替代无PID接管、Backend重启或Windows验收。

`test_terminal_root_exit_recovery.py`复用上述真实PTY装具，装具输出位置按当前测试文件确定。原停止失败生成unknown后，真实命令自行退出根，保留原SID内忽略SIGTERM/SIGHUP的后代；同Manager及冷Manager各经原HTTP kill/delete核实后代停止、持久终态和重复请求，不预填状态或替换身份／停止算法。该证据限定Linux，不代Python lease、Gateway/Web或无PID创建接管。

`test_terminal_process_diagnostics.py`经正式HTTP创建真实PTY，在原生stat读取/解析、session扫描或SIGTERM出口注入失败，并分别制造原日志writer的真实ENOTDIR；核普通响应、unknown持久记录、原进程身份仍存活及解除故障后同入口停止。fixture先执行HTTP资源清理再关闭client，并以冻结创建时间的psutil实例兜底回收。suite `integration-terminal-process-diagnostics`固定60秒/4GiB；原真实Linux/Windows进程树测试另由`integration-terminal-process-release`同样固定60秒/4GiB选择，平台skip不能计作原生通过。不代Python lease、Gateway/Web、Backend重启或其它平台实机。

同文件的ps/taskkill与探测双错场景始终由真实Linux PTY创建资源，只在故障窗口显式替换平台选择、原生查询和存在性探测，故障解除后恢复Linux原停止链。核主错误码、两份原诊断、unknown/原身份及同入口停止；不能把这种HTTP/PTY组合证据称为Windows或Darwin实机验收。


`test_terminal_startup_recovery.py`复用启动失败装具，输出按当前测试文件归属。真实创建在PID/身份交付前失败、收到迟到身份并丢弃停止证明后，持久保持created/unknown；同Manager和精确Node硬重启后的原HTTP kill/delete均沿原生启动标记核实终态、持久发布并重复请求，不补写恢复状态或重新执行命令。Linux定向验证保留60秒/4GiB及共享闸门，不代Python lease、未知native无身份、Windows或Gateway/Web。

`test_startup_directory_recovery.py`沿正式消息上传、Python Terminal 创建与管理入口复用同一启动故障装具。停止证明丢失时，核原operation lease和附件目录lease继续保护工作副本；原Node与硬重启后的Node均须由原管理停止核实并持久结清两份精确占用，再由原附件owner释放未修改的工作副本。独立核对原生身份、停止观察、恢复文件和冷读账本，不手填终态或重放创建；通过通用`integration-python`发现并沿共用闸门运行，不扩大原suite预算，不代Python Backend硬重启、无原生身份接管、Windows或Gateway/Web。

## 可修改内容

- Terminal 模块与受控子进程组成的集成测试及专用 fixture。

## 不可修改内容

- 不在此实现产品逻辑或放置纯解析单元测试。
- 不把受控进程测试称为完整终端用户流程的 E2E。

## 规范

- 通过 pytest fixture 创建和回收进程，只操作本用例持有的进程身份。
- Linux `/proc`、session 或僵尸进程的断言明确限定平台。
- 正式产物镜像测试文件路径写入 `out/tests/integration/workspace_services/terminal/`。
