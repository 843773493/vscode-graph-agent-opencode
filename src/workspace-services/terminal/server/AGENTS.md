# 目录用途

`src/workspace-services/terminal/server/` 存放持久终端管理后端。它负责启动和管理 PTY、维护终端元数据、通过 HTTP/JSON API 与 WebSocket 提供 attach/detach 和交互能力。

```text
server/
├── backend.js                    HTTP/WebSocket 入口与服务退出
├── terminalManager.js            工作区终端集合、容量与持久化协调
├── workspaceIdentity.js          UUID格式与权威工作区身份文件核对
├── terminalSession.js            单个终端的交互及状态转换
├── processes/                    操作系统进程与隔离PTY宿主
│   ├── isolatedPtyProcess.js      后端持有的worker句柄与IPC
│   ├── ptyWorker.js               worker内的PTY与逐包背压
│   ├── terminalProcessLifecycle.js  进程身份、进程树终止与退出确认
│   └── *.test.js                 宿主关闭及进程元数据解析
├── output/                       输出字节边界、重放与逐连接流控
│   ├── terminalOutputMultiplexer.js 模型与客户端输出游标、重放和广播
│   ├── terminalUtf8Output.js       UTF-8字节上限、尾部保留与事件切块
│   └── *.test.js                  字节边界、窗口、控制顺序与编码复用
├── terminalStateStore.js          工作区内终端记录的文件读写
└── *.test.js                      对应模块的单元测试
```

## 职责边界

单终端行为归 Session，跨终端策略归 Manager；系统进程是否仍在执行由进程层判断。Linux 僵尸进程已停止执行，父进程尚未回收不构成终端停止失败。

## 身份与传输授权

`TerminalManager.terminalsById`按`terminal_id`保存`TerminalSession`资源实例，含仍保留的非运行记录；它不是聊天Session或Thread集合。管理器内部资源集合与跨层业务身份用具体资源名区分，不保留旧字段别名。

工作区身份校验归`workspaceIdentity.js`；Manager、后端入口与attach静态服务直接引用，不为取得校验函数而加载Session或PTY管理链。该模块只验证显式UUID并读取现有权威身份文件，不创建身份、管理终端或补默认工作区。

`TerminalSession`表示按`terminal_id`定位的Node执行记录，不是聊天会话。`os_pid`是PTY执行根，`pty_worker_pid`是隔离Node宿主，`process_session_id`是操作系统SID/Darwin sess；进程身份、信号和退出确认统一归[processes/](processes/AGENTS.md)，不与资源状态、聊天Session或输出消费状态混用。

传输授权从Manager的一次性grant经WebSocket接线交给Session，内部统一用`TerminalTransportGrant.transportOwnerRevision`表示发放时冻结的owner版本；它不表示模型命令代际、输出sequence或完成事件编号。`expiresAtUnixMs`是授权过期的绝对时刻。公开HTTP字段`owner_revision/expires_at`和状态字段`transport_owner_revision`保持原合同，Node只保存传输所需的版本影子，不成为领域owner。

## 交互与命令事实

文本处理先区分“字节边界”和“命令协议语义”：前者归 `output/terminalUtf8Output.js`，后者归 `../shared/commandText.js`。两者只接收值并返回值，不持有资源或状态；Session决定何时解析并更新命令事实，Multiplexer决定输出顺序和消费窗口。展示文本只过滤含标记的整行，不替换原始buffer；输入标签是最后一个去掉首尾空白的非空行，不是命令执行结果。

输入标签按CR、LF或CRLF分行，先跳过尾部空白再定位最后有效行，不为全部输入做归一化或逐行分配；无换行走原生查询快速路径。Unicode空白只按trim语义处理，不把行内Unicode分隔符或ANSI当成新行。此结果只更新last_input，交给PTY及广播的输入仍是完整原文；全空白输入不清空既有标签及其来源时刻。

展示过滤按标记位置切除完整LF行，并保留普通正文的原始CRLF、单独CR及ANSI；不把整个缓冲拆成逐行数组。没有标记直接返回正文，有标记只拼接保留区间；该派生值不另设缓存或状态owner。

命令结束标记属于正在运行的模型命令，不属于普通交互输出。Session收到PTY输出时，仅在 `lastCommandStatus === "running"` 扫描保留缓冲；完成后停止扫描，正文记录、序号和广播仍照常进行。恢复记录中的命令状态推导继续由构造入口负责，不把输出回调变成通用历史扫描器。

退出码解析只保留最后一个匹配的数字文本，扫描结束后转换一次，不累计全部匹配数组；换行归一、ANSI处理及显式命令标记的字面量转义仍由同一纯函数负责。末尾残缺标记不覆盖之前的完整结果，缺省标记用于恢复读取，显式标记只匹配当前命令。

## 输出序号与消费游标

本地记录的`created_at/started_at/ended_at`是UTC ISO时间。输出事件的`timestamp`是Session接收消息的观察时刻，同次切块共享，重放沿用；顺序始终由`sequence`决定，不能用时间戳或进程启动标记代替。

Session内部用`outputSequence`表示已产生的最后输出，`modelReadThroughOutputSequence`表示模型已读到的位置（包含该条），`commandOutputBaselineSequence`表示命令写入前最后已有的输出。最后者不是该命令的首条输出，也不单独提供唯一命令代际；完成处理按`TerminalCompletionComparison`联合核对事件与基线。公开/落盘字段仍为`sequence/model_output_sequence/command_start_sequence`，完成请求中的`commandStartSequence`沿用同一基线含义，不与worker包号、UI ACK或传输授权版本混用。

输出身份分三层理解：processes内的worker按IPC包编号计费与回执，Session另行产生输出`sequence`供UI累计确认，聊天消息ID归业务层。processes不处理UI重放和socket窗口，output不拥有PTY句柄或进程回收。

输出的字节边界、增量重放与逐连接流控集中在`output/`，见[输出目录约束](output/AGENTS.md)。Session持有PTY事实与输出序号，output管理保留字节及各消费者的游标，协议编码仍只引用`../protocol/messages.js`。

## 状态观察与恢复落盘

Manager拥有唯一在途状态保存：先登记共享Promise，同轮请求合并，再取当前终端记录串行写入；写入期间新增请求由下一轮快照覆盖，所有等待方在排空后才成功。快照或writer的同步、异步异常原样交给等待方，并释放本次Promise，让后续显式保存可恢复；不能留下旧失败或自动重试。

StateStore的`TerminalStateWriteSnapshot`只承载Manager本轮恢复记录；`stateFilePayload`在此基础上加入权威工作区身份和文件schema。顶层`updated_at`在Manager开始组装本轮快照、逐条读取`toRecord()`之前生成，不是各资源的更新时间，也不表示写盘完成；类型直接引用Session的记录返回值，不复制一套字段定义。

StateStore在临时写入或发布失败后清理本次临时文件，正式状态继续保留旧字节；单次写入失败原样抛出。清理也失败时，用`AggregateError`按发生顺序保留两份原异常，并将写入异常作为`cause`，不能覆盖主失败或伪报发布成功。这里只结束本次文件操作，Manager释放在途保存后仍允许下一次显式请求，不自动重试或恢复临时正文。

Session的`toRecord()`是恢复事实的唯一组装入口，供Manager落盘；原始输出只存`buffer`一份。`snapshot()`在该记录上补齐`display_buffer`、当前`client_count`和`pty_worker_pid`供HTTP/WebSocket/模型读取；这些派生或连接观察不写入状态文件。恢复时从原始输出计算展示、从当前句柄取得连接信息，不维护第二份展示正文或持久连接状态。

`/health`的`terminal_count`直接读取Manager当前资源Map的大小，含仍保留的非运行记录，与列表条目数量同义；它不是活跃PTY进程数。健康计数不生成、排序终端展示快照，也不读取或过滤输出buffer。

## 测试工作区

`terminalManager.test.js`的工作目录场景只使用本次正式输出根下的目录：工作区内子目录、工作区外的兄弟目录和移动后的新根均由fixture创建，不依赖宿主`/etc`等目录。目录链接在Windows用junction、Linux/macOS用dir符号链接；断言仍核对链接路径保留、越出工作区后的相对表达及缺失目录明确失败。链接类型选择不等于对应平台已经原生验收。

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
