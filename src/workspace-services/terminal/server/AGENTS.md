# 目录用途

`src/workspace-services/terminal/server/` 存放持久终端管理后端。它负责启动和管理 PTY、维护终端元数据、通过 HTTP/JSON API 与 WebSocket 提供 attach/detach 和交互能力。

```text
server/
├── backend.js                    HTTP/WebSocket 入口与服务退出
├── terminalManager.js            工作区终端集合、容量与持久化协调
├── workspaceIdentity.js          UUID格式与权威工作区身份文件核对
├── terminalSession.js            单个终端的交互及状态转换
├── isolatedPtyProcess.js          后端持有的隔离 worker 句柄与 IPC
├── ptyWorker.js                   worker 内的 PTY 与输出背压
├── terminalProcessUtils.js        系统进程身份、信号及退出判定
├── terminalOutputMultiplexer.js   模型与客户端的输出游标、重放和广播
├── terminalOutputMultiplexerEncoding.test.js 多连接编码复用、独立重放与socket拒收
├── terminalUtf8Output.js          PTY文本的UTF-8字节上限、尾部保留与事件切块
├── terminalStateStore.js          工作区内终端记录的文件读写
└── *.test.js                      对应模块的单元测试
```

单终端行为归 Session，跨终端策略归 Manager；系统进程是否仍在执行由进程层判断。Linux 僵尸进程已停止执行，父进程尚未回收不构成终端停止失败。

工作区身份校验归`workspaceIdentity.js`；Manager、后端入口与attach静态服务直接引用，不为取得校验函数而加载Session或PTY管理链。该模块只验证显式UUID并读取现有权威身份文件，不创建身份、管理终端或补默认工作区。

Manager拥有唯一在途状态保存：先登记共享Promise，同轮请求合并，再取当前终端记录串行写入；写入期间新增请求由下一轮快照覆盖，所有等待方在排空后才成功。快照或writer的同步、异步异常原样交给等待方，并释放本次Promise，让后续显式保存可恢复；不能留下旧失败或自动重试。

Session的`toRecord()`是恢复事实的唯一组装入口，供Manager落盘；原始输出只存`buffer`一份。`snapshot()`在该记录上补齐`display_buffer`、当前`client_count`和`pty_worker_pid`供HTTP/WebSocket/模型读取；这些派生或连接观察不写入状态文件。恢复时从原始输出计算展示、从当前句柄取得连接信息，不维护第二份展示正文或持久连接状态。

文本处理先区分“字节边界”和“命令协议语义”：前者归 `terminalUtf8Output.js`，后者归 `../shared/commandText.js`。两者只接收值并返回值，不持有资源或状态；Session决定何时解析并更新命令事实，Multiplexer决定输出顺序和消费窗口。展示文本只过滤含标记的整行，不替换原始buffer；输入标签是最后一个去掉首尾空白的非空行，不是命令执行结果。

输入标签按CR、LF或CRLF分行，先跳过尾部空白再定位最后有效行，不为全部输入做归一化或逐行分配；无换行走原生查询快速路径。Unicode空白只按trim语义处理，不把行内Unicode分隔符或ANSI当成新行。此结果只更新last_input，交给PTY及广播的输入仍是完整原文；全空白输入不清空既有标签及其来源时刻。

展示过滤按标记位置切除完整LF行，并保留普通正文的原始CRLF、单独CR及ANSI；不把整个缓冲拆成逐行数组。没有标记直接返回正文，有标记只拼接保留区间；该派生值不另设缓存或状态owner。

命令结束标记属于正在运行的模型命令，不属于普通交互输出。Session收到PTY输出时，仅在 `lastCommandStatus === "running"` 扫描保留缓冲；完成后停止扫描，正文记录、序号和广播仍照常进行。恢复记录中的命令状态推导继续由构造入口负责，不把输出回调变成通用历史扫描器。

退出码解析只保留最后一个匹配的数字文本，扫描结束后转换一次，不累计全部匹配数组；换行归一、ANSI处理及显式命令标记的字面量转义仍由同一纯函数负责。末尾残缺标记不覆盖之前的完整结果，缺省标记用于恢复读取，显式标记只匹配当前命令。

`TerminalSession` 表示按 `terminal_id` 定位的 Node 执行记录，不是聊天会话。`os_pid` 指 PTY 执行根进程，`pty_worker_pid` 指持有 PTY 的隔离 worker；`process_session_id` 则是操作系统 SID / Darwin sess。`terminalProcessUtils.js` 的 `TerminalProcessStat` 统一说明这些身份及平台单位，禁止把它们与业务 Session ID 混用。

`IsolatedPtyProcess.workerProcess`是隔离Node句柄，`workerState`只表示该宿主的生命周期；`pid`保留node-pty的PTY执行根含义，`workerPid`才取Node句柄的PID。`ptyOutputHandlers`消费PTY正文，`exitObservationHandlers`接收首次退出观察；这个观察可能来自PTY消息或worker异常，退出码/信号属于对应来源。`workerShutdownExpected`仅表示安排过宿主退出，`exitObservationEmitted`只做交付去重，二者都不能证明终端进程树已回收。

`shutdown()`只有观察到worker的`exit`事件才成功：先给关闭IPC三秒宽限，再发SIGKILL并等待最多三秒确认；信号发送不算退出完成。未确认或发送失败须明确拒绝，并释放本次监听和定时器；该确认只涉及隔离worker，不替代PTY进程树的独立核验。

传输授权从Manager的一次性grant经WebSocket接线交给Session，内部统一用`TerminalTransportGrant.transportOwnerRevision`表示发放时冻结的owner版本；它不表示模型命令代际、输出sequence或完成事件编号。`expiresAtUnixMs`是授权过期的绝对时刻。公开HTTP字段`owner_revision/expires_at`和状态字段`transport_owner_revision`保持原合同，Node只保存传输所需的版本影子，不成为领域owner。

`process_start_time` 只与 PID、进程组和操作系统 session 联合核验身份，不是 UTC 创建时间；本地记录的 `created_at/started_at/ended_at` 才是 UTC ISO 时间。输出事件的 `timestamp` 是 Session 接收消息的观察时刻，同次切块共享，重放沿用；顺序始终由 `sequence` 决定。

输出身份分三层理解：worker的`outputPacketId`（IPC字段仍为`messageId`）逐包计费，宿主结束本次分发后逐包回执；Session另行产生输出`sequence`，UI按该序号累计确认；聊天消息ID属于业务层，与前两者无关。worker的`ptyOutputPaused`仅暂停node-pty输出读取，未确认UTF-8字节超过高水位时暂停、低于低水位时恢复，不表示OS进程或Job暂停。重复或未知IPC包回执不重复释放字节额度。

文本字节边界归 `terminalUtf8Output.js`，输出顺序、游标和背压归 Multiplexer；缓冲截断只能从完整字符开始，保留正文不得因解码替换字符而再次超过上限。无需切块的输出直接形成一个事件。

尾部只保留256KiB，不应编码已经确定会丢弃的整段输入。UTF-8每个UTF-16单元至少占1字节，因此只需编码最后256Ki个单元，再按字节边界对齐；扫描与编码范围由保留上限约束。未超限字符串保持原值，超限时沿用UTF-8编码对孤立代理项的替换语义。

超出64KiB的单次输出按码点宽度累计UTF-8字节，确定边界后从原字符串切片；不逐字符调用编码函数或拼接块正文。码点决定字符宽度，UTF-16偏移决定切片位置，孤立代理项按3字节计费且保留原值，不能把这两种单位混作同一偏移。

输出sequence只有Session逐一递增产生，重放环只从头部淘汰，因此Multiplexer可用序号差加逻辑头偏移定位增量起点，无需从头扫描或维护第二份位置索引。淘汰立即释放正文引用，空前缀达到数组一半后集中压缩，避免大量小输出逐项shift搬移。字节窗口、ACK、控制事件顺序及缺口resync仍由同一Multiplexer处理。

模型增量读取从已定位的后缀直接拼接正文，不为读取再复制事件数组或提取一份正文数组；不缓存拼接结果，不修改重放记录。字节保留上限、游标校验和缺口快照仍归同一读取入口。

客户端未确认队列只记录sequence和字节数，ACK累计确认到指定序号；逻辑头游标推进后同样集中压缩。窗口计费只包含未确认后缀；resync替换整份待确认快照时必须同时归零头游标，防止旧游标跳过新快照的确认。该队列与模型读取的重放缓冲各管自己的消费状态。

多个连接接收同一输出事件时，Multiplexer只在本次同步广播内复用现有协议owner生成的封包；广播返回即释放这个局部映射，重放记录不额外保留编码正文。后续ACK续传、socket重试和重连仍按各自游标重新编码；没有连接时不编码输出，不另建持久传输缓存或第二套序列化合同。

## 可修改内容

- 可以新增或修改终端 manager、HTTP 路由、WebSocket 协议处理和状态持久化逻辑。
- 可以维护测试友好的假 PTY 注入边界。
- 可以调整终端状态 DTO 字段，但要同步 Python 资源视图和前端 attach 页面。

## 不可修改内容

- 不要把主聊天前端组件放到这里。
- 不要直接依赖 FastAPI 容器或 Python 服务。
- 不要把终端状态持久化到 `.boxteam/terminal-manager/` 以外的位置。

## 规范

- 使用 Node.js ESM。
- 失败时返回明确 HTTP 错误或 WebSocket error 消息。
- detach 只断开客户端，不杀死 PTY；kill/delete 才终止终端。
- 保留“模板示例；在整理 `AGENTS.md` 时请保留此行。”这一行。
