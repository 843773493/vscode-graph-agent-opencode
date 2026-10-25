# 目录用途

`scripts/launch/` 存放源码开发栈、开发目标和本地服务生命周期的启动入口。

```text
launch/
├── dev-environment.mjs   开发路径、端口布局及工作区身份 CLI 接线
├── dev-readiness.mjs     各服务HTTP和detached manager记录的就绪等待
├── dev-manager.mjs       统一开发栈 start/status/stop 入口，按平台选择生命周期实现
├── dev-systemd.mjs       Linux transient unit 生命周期与外层 Gateway 就绪等待
├── dev.mjs               开发栈进程编排、各服务探针和 manager 就绪发布
├── debug-fixtures/       启动器安装的只读调试模板
└── cross-platform-development-target.mjs  跨平台开发目标入口
```

环境布局决定启动目标。`dev-manager.mjs` 在 Linux 委托 systemd，在 Windows/macOS 使用 `dev.mjs --only-launch` 管理 detached 服务组；两个入口共用相同的服务探针和就绪记录。systemd 入口等待该 manager 发布就绪后再探测 Gateway；健康响应成功后仍核对 manager PID。外层 Gateway 等待的 HTTP 请求、未消费正文的取消和轮询共用单调时钟的总预算，不因重试重新计时；每次请求在`finally`中清理计时器并主动中止控制器。健康响应只有在预算内释放正文并复核manager PID及ready marker后才算就绪。该预算不将同步 `systemctl` 调用变成可取消操作，也不替代 `dev.mjs` 内各服务的就绪检查。

Windows/macOS 的 detached manager 在 `out/development-runtime/detached-ready.json` 发布 PID、ready 时间和项目路径；start/stop 用同目录的操作锁串行化。锁 owner 已退出或锁内容损坏时明确报出锁路径，不猜测删除。macOS 对独立进程组先发 SIGTERM、等待15秒后才发 SIGKILL；Windows 先用 `taskkill /T` 请求进程树退出，等待15秒后仍存活才调用 `/T /F`，并确认 manager PID 退出。状态命令只确认 manager 存活与 Gateway 健康，不替代完整 Web 页面初始化检查。

外层等待已经观察到健康请求失败或manager替换后，后续“manager尚未就绪”的占位诊断不能覆盖该具体原因；新的HTTP观察仍可更新错误，终态进程退出仍立即抛出。

`dev-systemd.mjs status` 的 Gateway 单次HTTP观测使用独立2秒期限，并取消未消费响应正文；这是状态查询的观察预算，不代表启动就绪或systemd停止期限。

`dev.mjs`的各服务探针统一调用`dev-readiness.mjs`，默认HTTP观察预算90秒、detached manager记录预算120秒；请求头、目标进程和路径由调用方显式传入。请求、轮询与迟到响应判断共用`performance.now()`的单调期限；HTTP请求按剩余预算取消，只看状态码并取消未消费的正文，健康响应后再次核对进程退出事实。detached记录仍按子进程PID匹配，未写完的JSON允许下一次观察。观察超时不终止服务；同步文件读取和被阻塞的事件循环不因此获得硬取消能力，也不涉及fork的业务期限。

detached manager启动失败后，终止请求成功时原样抛出启动异常；终止请求也失败时，`AggregateError.errors`按顺序保留启动和清理两份原异常，`cause`指向启动异常。`dev-detached-lifetime.test.mjs`通过AST提取实际入口函数并注入文件、进程和信号替身，不执行开发编排或操作用户服务；该证据只覆盖异常交付与清理调用，不能替代真实进程退出确认。

Unix开发端口清理只查询TCP监听进程，不把已连接客户端纳入终止名单；`lsof`关闭名称解析，以`-iTCP:<逗号分隔端口>`和`-Fpn`一次取得全部目标端口的PID及监听地址，解析为端口→PID列表。仅正常结束、无任何输出的exit 1表示无匹配，其它命令失败或信号中断必须明确报错，不能当作端口空闲；无效字段、未登记端口或缺少PID的记录直接失败。`dev-port-listeners.test.mjs`用两个随机本地监听端口、一个空闲端口和独立客户端验证一次查询返回完整监听映射；错误路径以替身核查清理前失败，不发送真实终止命令。fixture与观察原件保留在`out/tests/scripts/launch/dev-port-listeners/{workspace,artifacts}/<run_id>/`；原生选择用例需要Linux/macOS和lsof，不代表完整开发栈启动验收。

Unix监听进程使用Linux/macOS共用的`process.kill`发信号，不再启动外部`kill`命令；先批量TERM，保留1秒等待，再重查并KILL残留，最后确认端口释放。三个阶段各取一次新快照，每阶段跨端口去重PID，不能对同一进程重复强杀；不因合并查询省略后续重查。仅ESRCH表示查询后的进程已退出，EPERM等其它异常原样抛出；Windows继续使用taskkill结束进程树。真实清理测试只允许向本次创建的随机端口监听子进程发信号，并保留PID、信号、lsof调用和退出结果；其它进程、外部kill命令及开发服务均不在授权范围。ESRCH/EPERM由独立替身验证，不将替身结果当作原生权限验收。

Windows每个清理阶段只读取一次完整TCP监听表，按目标端口分组；清理前的快照用于去重PID和taskkill，清理后的新快照用于确认释放，不跨阶段复用。只选本地端口上的TCP LISTENING行，不选择连接客户端或其它端口；查询失败保留命令、退出码、signal与stderr，目标监听行的无效PID直接报错。`dev-windows-port-listeners.test.mjs`以真实入口源码和命令替身验证IPv4/IPv6、去重、两阶段查询及失败，workspace与观察原件分别保留在`out/tests/scripts/launch/dev-windows-port-listeners/{workspace,artifacts}/<run_id>/`；不执行netstat/taskkill，不代表Windows原生服务验收。

`dev-systemd.test.mjs`的布局用例只计算路径和unit参数：输入根放正式输出镜像路径，含中文、空格，路径及PATH分隔符遵循宿主规则。状态观察用例只在该输出根下创建ready标记和`systemctl`替身，并使用动态本地HTTP端口验证真实status CLI的限时、响应正文取消和迟到响应；不启动transient unit、开发栈或用户端口。纯计算测试可跨平台运行，依赖POSIX替身和systemd CLI形态的状态用例仅Linux运行，不能把路径模拟通过当作跨平台服务启动验收。

`dev-systemd-readiness.test.mjs`直接调用真实`buildTransientDevelopmentUnit`和`waitForGateway`，用随机本地HTTP端口验证503请求的socket在重试前关闭、成功响应后连接及时释放、请求总预算、迟到响应及manager PID复核。测试仅以`Bun.spawnSync`替身应答并断言完整`systemctl show`命令，不会执行真实`systemctl`或触碰用户服务；ready marker、fixture、raw和结果独占写入`out/tests/scripts/launch/dev-systemd-readiness/{workspace,artifacts}/<run_id>/<scenario>/`。

`dev-systemd-state.test.mjs`启动独立 CLI 并用 preload 替换`Bun.spawnSync`，通过空`PATH`避免触及真实服务；验证状态查询只把无 signal 且输出明确`LoadState=not-found`的非零结果映射为不存在，bus错误与signal会保留为详细错误。workspace、raw和结果独占写入`out/tests/scripts/launch/dev-systemd-state/{workspace,artifacts}/<run_id>/<scenario>/`，Linux以外跳过CLI用例。

`cross-platform-development-target.test.mjs`的配置、SSH边界和Git快照fixture统一归文件级运行根`out/tests/scripts/launch/cross-platform-development-target/workspace/<run_id>/<case_id>/`，先登记结束清理，再准备各自目录；不维护临时目录数组、不写系统tmp。SSH命令测试在本次隔离目录生成假identity及known-hosts路径，不能引用或收紧`asset/`模板密钥。POSIX权限场景在Windows显式跳过；命令组装和配置校验不连接真实目标，Git快照场景另需允许初始化隔离仓库及写入其index，不能把未运行的快照用例算成通过。

# 可修改内容

- 可以维护开发服务启动、停止、状态检查和跨平台开发目标编排脚本。
- 可以维护启动脚本使用的环境布局、端口计算和最小调试 fixture。

# 不可修改内容

- 不在这里实现 Workspace、Gateway、Agent 或客户端业务逻辑。
- 不在这里放发布打包、安装后处理或长期运行时产物。

# 规范

- JavaScript 脚本使用 ESM，并从仓库根目录或显式路径解析资源。
- 开发启动必须尊重 `BOXTEAM_HOME`、worktree 隔离和显式端口配置。
- 启动失败时直接报告具体命令、路径或服务错误。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
