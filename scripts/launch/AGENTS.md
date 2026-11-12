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

Windows/macOS 的 detached manager 在 `out/development-runtime/detached-ready.json` 发布 PID、ready 时间和项目路径；start/stop 用同目录的操作锁串行化。锁 owner 已退出或锁内容损坏时明确报出锁路径，不猜测删除。macOS 对独立进程组先发 SIGTERM、等待170秒后才发 SIGKILL；Windows 先用 `taskkill /T` 请求进程树退出，等待170秒后仍存活才调用 `/T /F`，并确认 manager PID 退出。状态命令只确认 manager 存活与 Gateway 健康，不替代完整 Web 页面初始化检查。

外层等待已经观察到健康请求失败或manager替换后，后续“manager尚未就绪”的占位诊断不能覆盖该具体原因；新的HTTP观察仍可更新错误，终态进程退出仍立即抛出。

`dev-systemd.mjs status` 的 Gateway 单次HTTP观测使用独立2秒期限，并取消未消费响应正文；这是状态查询的观察预算，不代表启动就绪或systemd停止期限。

`dev.mjs`的各服务探针统一调用`dev-readiness.mjs`，默认HTTP观察预算90秒、detached manager记录预算120秒；请求头、目标进程和路径由调用方显式传入。请求、轮询与迟到响应判断共用`performance.now()`的单调期限；HTTP请求按剩余预算取消，只看状态码并取消未消费的正文，健康响应后再次核对进程退出事实。detached记录仍按子进程PID匹配，未写完的JSON允许下一次观察。观察超时不终止服务；同步文件读取和被阻塞的事件循环不因此获得硬取消能力，也不涉及fork的业务期限。

detached manager启动失败后，终止请求成功时原样抛出启动异常；终止请求也失败时，`AggregateError.errors`按顺序保留启动和清理两份原异常，`cause`指向启动异常。`dev-detached-lifetime.test.mjs`通过AST提取实际入口函数并注入文件、进程和信号替身，不执行开发编排或操作用户服务；该证据只覆盖异常交付与清理调用，不能替代真实进程退出确认。

Unix及Windows启动前只查询本次目标端口的TCP监听者；有任何监听者时列出端口与PID并拒绝启动，不尝试判断其属于哪个实例，也不发送TERM、KILL或taskkill。`lsof`关闭名称解析，以`-iTCP:<逗号分隔端口>`和`-Fpn`一次取得全部监听映射；仅正常结束、无任何输出的exit 1表示无匹配，其它查询失败必须明确报错，不能当作端口空闲。Windows通过`netstat -ano -p tcp`只读取LISTENING行。`dev-port-listeners.test.mjs`以真实随机本地监听者验证拒绝启动且不发信号；Windows测试以命令替身验证不会执行taskkill。产物保留在对应`out/tests/scripts/launch/<测试文件名>/<run_id>/`路径；不能把端口空闲证明视为完整开发栈启动验收。

监听进程查询不可见的端口仍可能因权限或平台差异不可 bind；因此 `dev.mjs` 启动前还会通过 Node `net.createServer` 对选中监听地址及所有目标 TCP 端口逐个 bind/close。任一失败保留 host、port、错误码和原始错误并停止启动；该检查只短暂占用本次候选端口，不终止现有进程。`dev-port-bindable.test.mjs`使用随机端口上的真实 TCP listener 检查 EADDRINUSE、空闲 bind/close 与其它 bind 错误，不依赖 lsof 可见性。

实例重启由该实例的 manager/systemd 生命周期负责回收进程组；若端口仍被占用，启动器保留进程并明确失败。禁止以端口占用本身作为终止进程的授权依据。

`dev.mjs` 启动前若目标 Home 的 Launcher 锁仍指向存活 PID，就拒绝启动并要求调用对应实例的 `dev-manager stop`；不能在新启动链里停止现有 Launcher。锁 PID 仅用于 `kill(pid, 0)` 存活探测，ESRCH 视为陈旧锁，其他进程错误显式报出。`dev-launcher-lock.test.mjs`提取此入口并只验证零信号探测。

Windows每次启动只读取一次完整TCP监听表，按目标端口找出本地LISTENING行；只选LISTENING，不选择客户端或其它端口。查询失败保留命令、退出码、signal与stderr；任一端口被占用就拒绝启动，不执行taskkill。`dev-windows-port-listeners.test.mjs`用命令替身验证占用与失败路径，不能替代Windows原生服务验收。

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

- worktree 角色与脚本目标契约见 [worktree 与发布迁移约定](../../docs/architecture/worktree-and-release-policy.md)：根共享开发，根验证优先，dev 集成次之，实验隔离最后；固定目录采用主 worktree 的 `out/worktrees/dev/`、`out/worktrees/release/`，实验目录按首次创建 UTC 日期归入 `out/worktrees/YYYY/MM/DD/<task_name>/`。
- `workspace.mjs <root|dev|experiment> <start|stop|status> [experiment-path]` 是开发服务的统一实例路由。dev 使用根启动器和根 Vite，后端源码/Home/工作区/端口绑定到受管 dev；root 从根启动整组服务；experiment 从自己的 checkout 启动。dev/experiment 核对匹配 `primary_root` 的 metadata，归档 checkout 拒绝启动。root/dev 可用 `--backend-root <路径>` 显式选择后端源码，其 Home 默认属于选定后端并使用显式环境端口偏移；只有实验允许 `--home=<路径>`。入口拒绝冲突的继承 Home/workspace，不启动 release。
- `worktree.mjs` 提供 create/setup/sync/list/artifact/archive：按 Git 注册定位主 worktree，保留源暂存/未暂存/新增改动；同步只允许明确选择文件并在目标冲突时拒绝覆盖；archive 保留 checkout 与完整脏状态。归档调用者必须完成进程与其他 Agent 引用检查并显式传 `--checked-references`；入口还会检查 ready、manager lock、Launcher lock 与同一布局的 systemd unit，活动/未知状态或 status 查询错误都会拒绝归档。此参数是检查确认，不是删除或停服授权。
- 本机 dev/实验创建默认复制明确清单中的 `.env`、未跟踪 `.env.*` 与 `configs/*_local.jsonc`，不复制 Home/数据库/身份/运行账本；不得打印配置值，不把本机复制授权扩展为远端同步授权。初始化依赖使用各 checkout 自己的锁文件和依赖目录。
- 固定 dev 的 Workbench Vite、Terminal/Browser 前端取根源码，Launcher、Gateway、Workspace 与辅助后端、Python 环境、配置资源取 dev。`BOXTEAM_PROJECT_ROOT` 是后端路径，`BOXTEAM_FRONTEND_ROOT` 是前端路径，启动和状态均展示两者；底层启动器也接受 `--backend-root`。固定 dev 的 Home/工作区与端口独立，配置复制不能继承源实例运行身份；同步时核对协议。不得重启或清理另一任务正在使用的实例。
- 完整浏览器前端只从 `src/clients/workbench` 启动 Vite；端口与 Gateway 路由由统一环境布局传入，不启动旧 React App 外壳。

- `cross-platform-development-target.test.mjs` 的 Linux bootstrap 检查实际执行管理脚本，uv、bun、node、python3 和 Git 为明确的命令替身；验证缺失上游时在安装前失败、三份依赖安装顺序、Workbench 锁变化的 metadata 指纹，以及安装失败的退出码与零成功回执。命令、原始 stdout/stderr 和替身调用保留在同名正式镜像目录的 artifacts/<run_id>/bootstrap/；只在隔离 fixture 写文件，不连接目标或修改宿主环境，不替代 Windows 及真实依赖安装验收。

- JavaScript 脚本使用 ESM，并从仓库根目录或显式路径解析资源。
- 开发启动必须尊重 `BOXTEAM_HOME`、worktree 隔离和显式端口配置。
- 启动失败时直接报告具体命令、路径或服务错误。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。

- Gateway结清加入正式停止链后，detached manager的优雅退出期限统一为170秒；systemd最外层`TimeoutStopSec`为180秒，必须晚于manager与Launcher内层期限，不新增环境变量或配置参数。
