# 目录用途

存放Terminal的操作系统进程身份、隔离PTY宿主及进程退出处理；不管理工作区终端集合或聊天会话。

```text
processes/
├── terminalProcessLifecycle.js       进程元数据、身份核验、信号与进程树退出
├── terminalProcessLifecycle.test.js  Linux/POSIX/Darwin元数据解析
├── terminalWindowsTermination.test.js  Windows工具失败及目标退出确认的系统边界替身
├── isolatedPtyProcess.js             后端持有的隔离Node句柄与IPC
├── isolatedPtyProcess.test.js        worker关闭、强杀后确认与监听器释放
└── ptyWorker.js                      隔离进程内的node-pty及逐包读取背压
```

按资源边界理解调用链：Manager和Session决定终端业务状态，IsolatedPtyProcess管理Node宿主句柄，ptyWorker持有PTY，terminalProcessLifecycle核验并停止操作系统进程。进程退出观察不等于整个终端资源已经释放，信号发送也不等于进程已退出。

`TerminalProcessStat`统一说明PID、进程组、操作系统SID/Darwin sess与`processStartTime`。启动标记只用于同平台身份联合核验：Linux为启动后的时钟滴答数，其他POSIX为ps原文；不是业务Session ID或UTC时间。Linux僵尸进程已停止执行，等待父进程回收不属于终端停止职责。真实信号与身份检查位于`tests/integration/workspace_services/terminal/test_process_release.py`。

Windows通过`taskkill /T /F /PID`请求进程树终止，工具调用有独立上限并保留启动错误、退出码、信号和stderr。目标仍存活时的工具失败必须抛出，不能返回`missing`；工具成功后仍在有界期限内用Node的PID存在性查询确认执行根消失，持续存活返回`still_running`。该查询不依赖POSIX元数据，不能把Windows的`readProcessStat() === null`当作退出证据；也不声称已逐个独立核验树内后代。Windows单测在隔离Node进程模拟平台、工具与存在性边界，不调用宿主taskkill或发送真实信号，不能代替Windows原生验收。

Windows和POSIX共用的退出宽限等待按`performance.now()`的单调毫秒计时，轮询间隔不超过剩余预算；系统校时前跳或回拨不改变宽限长度。期限只限制轮询等待，不取消正在执行的进程状态查询，也不代表整个终端关闭入口的硬期限。

`IsolatedPtyProcess.workerProcess`是隔离Node句柄，`workerState`只表示该宿主的生命周期；`pid`保留node-pty的PTY执行根含义，`workerPid`才取Node句柄的PID。`ptyOutputHandlers`消费PTY正文，`exitObservationHandlers`接收首次退出观察；观察可能来自PTY消息或worker异常，退出码/信号属于对应来源。`workerShutdownExpected`只表示安排过宿主退出，`exitObservationEmitted`只做交付去重，二者都不能证明进程树已回收。

`shutdown()`只有观察到worker的`exit`事件才成功：先给关闭IPC三秒宽限，再发SIGKILL并等待最多三秒确认。未确认或发送失败须明确拒绝，并释放本次监听和定时器；该确认只涉及隔离worker，不替代PTY进程树的独立核验。

启动的首条IPC发送、worker错误、就绪前退出和十秒超时共用同一个就绪Promise。失败先清除启动计时器，再请求停止尚未退出的worker；停止请求也失败时，以`AggregateError.errors`按顺序保留启动和信号两份原始异常，不能让信号错误越过Promise或留下永不结束的启动。`stopping`只表示已经进入停止流程，不能当作退出确认；相关单测用显式IPC/信号替身检查这条控制流，不冒充真实进程树回收证据。

worker的`outputPacketId`（IPC字段仍为`messageId`）逐包计费，宿主结束本次分发后逐包回执；它不是Session的输出sequence或聊天消息ID。`ptyOutputPaused`只暂停node-pty输出读取，未确认UTF-8字节超过高水位时暂停、低于低水位时恢复，不表示OS进程或Job暂停。重复或未知包回执不重复释放额度。

## 可修改内容

- 系统进程元数据解析、身份核验、信号发送和有界退出等待。
- 隔离Node宿主的启动、IPC、退出确认与worker内的PTY读取背压。
- 对应Node单测；真实受控进程验收仍放在integration目录。

## 不可修改内容

- 不管理TerminalManager集合、资源保留策略、HTTP/WebSocket或持久化文件。
- 不处理UI的累计ACK、输出重放或模型读取游标；这些归`../output/`。
- 不保留父目录旧文件的转发模块，不复制另一套进程身份或终止实现。

## 规范

- 使用ESM；node-pty的现有原生加载仅在worker内执行，不加载到后端宿主。
- worker入口与隔离句柄同目录，用`import.meta.url`确定worker文件，不依赖启动cwd。
- 进程身份不匹配明确拒绝终止，已发送信号不能冒充退出成功。
- 单测沿既有辅助服务runner递归发现；目录移动不新增测试副本或改变断言。
