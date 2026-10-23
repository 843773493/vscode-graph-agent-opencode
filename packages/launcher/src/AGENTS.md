# 目录用途

`packages/launcher/src/` 存放 Launcher 的可测试实现模块。

运行资源按下列链路定位，平台包选择与宿主路径规则分别验证：

```text
运行资源
├── runtime-discovery.mjs  显式manifest优先，其次按目标平台选择包
├── runtime-manifest.mjs   校验清单，并以清单目录解析资源位置
└── cli.mjs                消费已定位的Node、Python、配置和Skill资源
    ├── config-bootstrap.mjs  用manifest Python执行配置初始化
    └── federation-token.mjs  用manifest Python签发配对令牌
```

Gateway相关实现与测试集中在[Gateway生命周期](gateway/AGENTS.md)：

```text
gateway/
├── supervisor.mjs             进程、配置generation与handoff协调
├── control-channel.mjs        控制端点、请求/响应传输及连接释放
├── public-listener.mjs        HTTP/WebSocket代理与当前目标
├── server-lifetime.mjs        监听器建立、关闭和事件释放
├── supervisor.test.mjs        启动契约、信号与active恢复
├── handoff.test.mjs           Node真实控制连接与generation切换
├── control-channel.test.mjs   Unix/Windows端点与文件操作边界
├── control-client.test.mjs    Node真实响应、断连及连接释放
├── readiness.test.mjs         请求预算、取消与连接释放
├── public-listener.test.mjs   真实HTTP流转发
└── public-listener-websocket.test.mjs 真实WebSocket握手、消息与释放
```

`config-bootstrap.test.mjs`和`federation-token.test.mjs`的路径输入分别位于各自`out/tests/packages/launcher/src/<测试名>/workspace/`下，包含中文和空格。输入和预期均按宿主路径规则构造；spawn为替身，只验证命令接线与返回值，不创建这些目录、不初始化用户配置或签发真实令牌。

两个Python命令入口将子进程创建失败与命令非零退出分开：`spawnSync`返回的`error`作为包装异常的`cause`原样保留，供调用方定位错误码、路径和底层堆栈；原有文案及stderr处理保持。对应测试注入ENOENT、EACCES和ETIMEDOUT，断言异常身份和完整文案，不执行Python、不证明原生平台的权限或超时行为，也不为这些命令新增超时预算。

两份`runtime-*.test.mjs`只做清单和路径计算；输入根位于各自`out/tests/packages/launcher/src/<测试名>/workspace/`下，包含中文和空格，路径使用宿主规则。目标平台仍由参数单独指定；这些测试不创建目录、不加载真实平台包或执行安装。

`detached-process.mjs`区分分离启动、终止请求和带升级的退出等待。Windows的`terminateDetachedProcess`通过`taskkill /T /F /PID`发出终止请求，工具调用最多等待5秒；启动或超时异常通过`cause`保留，非零退出或信号结束且目标仍存在时，明确报告PID、退出码、信号和stderr。目标已消失允许幂等返回，查询本身失败则原样抛出。成功返回只表示工具返回；该同步入口不等待执行根或后代全部退出，POSIX侧也仍只发送SIGTERM。

`detached-windows-termination.test.mjs`在独立Node进程模拟Windows、taskkill和PID存在性边界，不执行宿主taskkill或向真实PID发送信号；它验证错误分类和工具调用配置，不能代替Windows原生进程树验收。

`spawnDetachedProcess`只有在父端日志描述符关闭成功后才交付原child；单一创建或关闭异常原样抛出，同时失败按发生顺序聚合，cause保留首因。创建成功但交付前关闭失败时，复用`terminateDetachedProcess`向本次尚未交付的PID发终止请求；请求也失败时继续保留该原因。不重复关闭同一描述符、不清理其它实例，也不把终止请求成功写成已确认整棵进程树退出。无PID表示没有可定位的已创建进程，不因此补造PID或发信号。

日志IO集中在[logging/](logging/AGENTS.md)：`cli.mjs`和`dev.mjs`消费日志输出与路径，`detached-process.mjs`复用同一描述符初始化。正常与故障fixture分别位于`out/tests/packages/launcher/src/logging/<测试名>/workspace/<run_id>/`；owner只释放本次持有的描述符。故障fixture按平台和场景保留输入及对应artifacts；既有初始化、短写和失败归属覆盖不变，新增创建/交付边界以Linux、macOS和Windows替身核调用顺序及错误身份，不启动后台进程。轮转fixture通过文件长度触发大小阈值，不分配同等大小的正文缓冲区；文件系统是否稀疏存储取决于宿主，不能据此宣称跨平台性能收益。

`gateway/supervisor.mjs`的就绪探针按单调时钟共享请求及重试预算；只观察HTTP状态并取消正文，超时保留最后原因。每个待启动Gateway持有自己的取消信号，退出或就绪竞争结束即取消该探针，不能让旧generation继续轮询。`gateway/readiness.test.mjs`以随机端口的隔离HTTP服务验证请求释放，以进程替身验证实际supervisor入口的取消交付；不启动用户服务。

`instance-lock.mjs`先排他创建锁，再写入并关闭描述符；初始化失败只清理本次尚未交付的锁，单一异常原样抛出，多重失败按发生顺序聚合并保留首个原因。两份`instance-lock*.test.mjs`分别验证实例冲突/失效替换和初始化失败，输入放各自`out/tests/packages/launcher/src/<测试名>/workspace/<run_id>/<scenario>/`。运行和场景目录独占创建，结束后保留正式现场；正常业务释放仍删除当前锁，不能为留证改变被测行为。

```text
out/tests/packages/launcher/src/instance-lock/
├── workspace/<run_id>/<active-conflict|stale-replacement|query-…>/控制 home/
│   └── state/launcher.lock           被测锁；成功release后按产品行为消失
└── artifacts/<run_id>/<scenario>/
    ├── input.json                    当前场景的home与runtime输入，路径遵循宿主规则
    ├── original.lock.json            原始锁字节，在冲突/查询/替换之前保存
    └── after-….lock.json或replacement.lock.json  业务操作后的锁快照
```

工作区输入与观察结果按相同运行和场景连接；锁快照只保留已读字节，不重新序列化业务内容。查询异常场景复用同一份操作后快照做字节断言，不重复读取；快照独占写入，不覆盖旧运行。进程存在性为替身，不向真实PID发信号，不证明原生平台锁竞争或跨进程竞态。

```text
out/tests/packages/launcher/src/instance-lock-errors/
├── workspace/<run_id>/<write|close|write-close|write-remove|write-close-remove>/
│   └── 控制 home/state/launcher.lock  仅清理失败场景保留未完成锁
└── artifacts/<run_id>/
    ├── observer.mjs                   本次运行共用的独立Node观察脚本
    └── <scenario>/                    input.json、stdout.raw、stderr.raw、result.json
```

先分开输入与观察，再以相同运行ID和场景连接两者；独立Node注入文件边界异常，不向宿主PID发信号。原始输出及准确命令、cwd、5秒子进程预算和真实退出结果先落盘，再解析或判断；外层runner仍提供进程外保护，不以包装脚本成功代替锁行为断言。测试只用隔离控制目录，不操作用户服务，也不证明跨进程竞态或原生Windows/macOS锁验收。

锁与停止入口共用的`processIsAlive`只把`ESRCH`视为不存在、`EPERM`视为目标存在但无权发信号；其它查询异常原样传播，不能伪报实例冲突或继续进入终止升级。锁测试核原锁字节保持；停止测试从默认探针进入，仅替换`process.kill`，验证停止前及SIGTERM后的查询失败不再发后续信号或删锁。detached终止的探针仍在信号权限错误时直接失败，不与锁获取的权限判断混为同一合同。

`stop.mjs`分别观察原进程是否退出和锁token是否仍属原实例；锁消失不等于进程退出，替换后的锁不能删除。优雅/强制观察共用各自的单调预算，只由明确的超时结果触发升级，不能根据异常文案决定强杀。`stop.test.mjs`使用锁、进程与时钟替身验证状态转换，不向宿主PID发送信号或创建工作区。

`terminateProcessWithEscalation`的`nowImpl`参数表示单调毫秒时钟，默认使用`performance.now`；两段期限各自固定，轮询只消费剩余预算。系统墙钟校时不提前强制终止或拖长等待，剩余时间耗尽不向sleep传入负数，亚毫秒余量向上取整避免Bun.sleep空转；PID存在性、信号与权限异常仍由原入口处理。

`detached-process.test.mjs`的真实POSIX进程fixture位于`out/tests/packages/launcher/src/detached-process/workspace/<run_id>/<case_id>/`。目录创建后即进入清理范围，ready等待或断言失败仍从固定PID记录发出进程组终止请求；生成的子进程另设15秒自退出，防止测试进程被外部终止后无限存活。文件等待使用单调时钟；终止请求不等于已确认整个进程树退出，Windows原生生命周期仍待对应目标验证。

# 可修改内容

- 可以实现 manifest 校验、进程监督、锁和诊断 helper。

# 不可修改内容

- 不依赖仅在源码 checkout 存在的相对目录。
- 不静默修复损坏 manifest 或切换系统运行时。

# 规范

- 模块抛出包含资源路径和发行标识的明确错误。
- 顶层入口保持轻量，复杂行为拆到聚焦模块。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
