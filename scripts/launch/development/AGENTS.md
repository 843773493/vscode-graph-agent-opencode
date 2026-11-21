# 目录用途

实现公开 `../dev.mjs` 入口背后的开发栈编排、平台进程管理、就绪检查和内置插件准备；测试位于 `tests/`，只读调试模板位于 `fixtures/`。

环境布局决定启动目标。`manager.mjs` 在 Linux 委托 systemd，在 Windows/macOS 使用 `services.mjs --only-launch` 管理 detached 服务组；两个入口共用相同的服务探针和就绪记录。systemd 入口等待该 manager 发布就绪后再探测 Gateway；健康响应成功后仍核对 manager PID。外层 Gateway 等待的 HTTP 请求、未消费正文的取消和轮询共用单调时钟的总预算，不因重试重新计时；每次请求在`finally`中清理计时器并主动中止控制器。健康响应只有在预算内释放正文并复核manager PID及ready marker后才算就绪。该预算不将同步 `systemctl` 调用变成可取消操作，也不替代 `services.mjs` 内各服务的就绪检查。

Windows/macOS 的 detached manager 在 `out/development-runtime/detached-ready.json` 发布 PID、ready 时间和项目路径；start/stop 用同目录的操作锁串行化。锁 owner 已退出或锁内容损坏时明确报出锁路径，不猜测删除。macOS 对独立进程组先发 SIGTERM、等待170秒后才发 SIGKILL；Windows 先用 `taskkill /T` 请求进程树退出，等待170秒后仍存活才调用 `/T /F`，并确认 manager PID 退出。状态命令只确认 manager 存活与 Gateway 健康，不替代完整 Web 页面初始化检查。

外层等待已经观察到健康请求失败或manager替换后，后续“manager尚未就绪”的占位诊断不能覆盖该具体原因；新的HTTP观察仍可更新错误，终态进程退出仍立即抛出。

`systemd.mjs status` 的 Gateway 单次HTTP观测使用独立2秒期限，并取消未消费响应正文；这是状态查询的观察预算，不代表启动就绪或systemd停止期限。

`services.mjs`的各服务探针统一调用`readiness.mjs`，默认HTTP观察预算90秒；前端须等待 Workbench 资源构建和 Vite 入口预热，预算300秒。systemd 与 detached manager 的外层启动预算统一为420秒，覆盖顺序启动的 Gateway 与前端；请求头、目标进程和路径由调用方显式传入。请求、轮询与迟到响应判断共用`performance.now()`的单调期限；HTTP请求按剩余预算取消，只看状态码并取消未消费的正文，健康响应后再次核对进程退出事实。detached记录仍按子进程PID匹配，未写完的JSON允许下一次观察。观察超时不终止服务；同步文件读取和被阻塞的事件循环不因此获得硬取消能力，也不涉及fork的业务期限。

detached manager启动失败后，终止请求成功时原样抛出启动异常；终止请求也失败时，`AggregateError.errors`按顺序保留启动和清理两份原异常，`cause`指向启动异常。`tests/detached-lifetime.test.mjs`通过AST提取实际入口函数并注入文件、进程和信号替身，不执行开发编排或操作用户服务；该证据只覆盖异常交付与清理调用，不能替代真实进程退出确认。

Unix及Windows启动前只查询本次目标端口的TCP监听者；有任何监听者时列出端口与PID并拒绝启动，不尝试判断其属于哪个实例，也不发送TERM、KILL或taskkill。`lsof`关闭名称解析，以`-iTCP:<逗号分隔端口>`和`-Fpn`一次取得全部监听映射；仅正常结束、无任何输出的exit 1表示无匹配，其它查询失败必须明确报错，不能当作端口空闲。Windows通过`netstat -ano -p tcp`只读取LISTENING行。`tests/port-listeners.test.mjs`以真实随机本地监听者验证拒绝启动且不发信号；Windows测试以命令替身验证不会执行taskkill。产物保留在对应`out/tests/scripts/launch/development/tests/<测试文件名>/<run_id>/`路径；不能把端口空闲证明视为完整开发栈启动验收。

监听进程查询不可见的端口仍可能因权限或平台差异不可 bind；因此 `services.mjs` 启动前还会通过 Node `net.createServer` 对选中监听地址及所有目标 TCP 端口逐个 bind/close。任一失败保留 host、port、错误码和原始错误并停止启动；该检查只短暂占用本次候选端口，不终止现有进程。`tests/port-bindable.test.mjs`使用随机端口上的真实 TCP listener 检查 EADDRINUSE、空闲 bind/close 与其它 bind 错误，不依赖 lsof 可见性。

实例重启由该实例的 manager/systemd 生命周期负责回收进程组；若端口仍被占用，启动器保留进程并明确失败。禁止以端口占用本身作为终止进程的授权依据。

`services.mjs` 启动前若目标 Home 的 Launcher 锁仍指向存活 PID，就拒绝启动并要求调用对应实例的 `workspace stop`；不能在新启动链里停止现有 Launcher。锁 PID 仅用于 `kill(pid, 0)` 存活探测，ESRCH 视为陈旧锁，其他进程错误显式报出。`tests/launcher-lock.test.mjs`提取此入口并只验证零信号探测。

Windows每次启动只读取一次完整TCP监听表，按目标端口找出本地LISTENING行；只选LISTENING，不选择客户端或其它端口。查询失败保留命令、退出码、signal与stderr；任一端口被占用就拒绝启动，不执行taskkill。`tests/windows-port-listeners.test.mjs`用命令替身验证占用与失败路径，不能替代Windows原生服务验收。

`tests/systemd.test.mjs`的布局用例只计算路径和unit参数：输入根放正式输出镜像路径，含中文、空格，路径及PATH分隔符遵循宿主规则。状态观察用例只在该输出根下创建ready标记和`systemctl`替身，并使用动态本地HTTP端口验证真实status CLI的限时、响应正文取消和迟到响应；不启动transient unit、开发栈或用户端口。纯计算测试可跨平台运行，依赖POSIX替身和systemd CLI形态的状态用例仅Linux运行，不能把路径模拟通过当作跨平台服务启动验收。

`tests/systemd-readiness.test.mjs`直接调用真实`buildTransientDevelopmentUnit`和`waitForGateway`，用随机本地HTTP端口验证503请求的socket在重试前关闭、成功响应后连接及时释放、请求总预算、迟到响应及manager PID复核。测试仅以`Bun.spawnSync`替身应答并断言完整`systemctl show`命令，不会执行真实`systemctl`或触碰用户服务；ready marker、fixture、raw和结果独占写入`out/tests/scripts/launch/development/tests/systemd-readiness/{workspace,artifacts}/<run_id>/<scenario>/`。

`tests/systemd-state.test.mjs`启动独立 CLI 并用 preload 替换`Bun.spawnSync`，通过空`PATH`避免触及真实服务；验证状态查询只把无 signal 且输出明确`LoadState=not-found`的非零结果映射为不存在，bus错误与signal会保留为详细错误。workspace、raw和结果独占写入`out/tests/scripts/launch/development/tests/systemd-state/{workspace,artifacts}/<run_id>/<scenario>/`，Linux以外跳过CLI用例。

# 可修改内容

- 开发栈的平台生命周期、服务编排、SDK 构建接线与对应测试。

# 不可修改内容

- 不实现 Agent 业务或发行安装规则，不建立第二套进程账本。
- 不恢复独立 dev:watch 启停器，不为 SDK 编译增加常驻服务。

# 规范

- 公开入口在加载 SDK 消费者前编译选定前后端源码根，内部进程沿原管理器启动，不重复构建。
- 已有实例沿原幂等启动合同复用；只有本次新建后就绪失败才由创建者清理，保留启动与清理原错。
- 完整停止沿原 owner 回收进程；不按端口杀进程。
- SDK 编译不替代严格类型检查，不自动修改已有插件绑定。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
