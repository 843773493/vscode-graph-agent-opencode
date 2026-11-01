# 目录用途

`integration-debug-cold-recovery` 复用真实调试 API、Node/Inspector 与冷 service，核对 claim／OS 身份／恢复审计的原生等待、同 owner 竞争及取消结算，固定90秒／4GiB与原集成闸门；准入沿现有替身，不代 Session 删除 gate、Gateway、进程硬重启或 Windows。

`integration-debug-catalog-drain` 经真实导航入队、Session 删除 owner 和 SQLite 验证 Node 调试 catalog 读取／关闭等待，原连接与 gate 结算后才允许取消或继续删除；固定60秒／4GiB及原独占闸门，不代真实 Gateway、外部调试进程、重启或整个 shutdown 期限。

`integration-debug-claim-publication` 经真实调试 ASGI、Node/Inspector 与原 claim writer 验证三种启动阶段的写盘、重复取消、迟到故障及进程退出顺序；固定120秒/4GiB，沿原集成独占闸门，不代替 Gateway/Web、独立进程重启、Windows 或整个 shutdown 期限。

`integration-generation-source-publication` 经真实 Session/任务 ASGI 入口生成定义，停止隔离运行时的原 worker 后验证来源 URI 的冷读、普通错误、复制发布拒绝及原事务回滚；固定60秒/4GiB及集成独占闸门，不代替执行恢复、独立进程重启、Gateway/Web或Windows验收。

`integration-mcp-connection-publication` 经真实 SDK/stdio 与原持久账本验证三种连接入口的登记、重复取消、写盘失败及关闭顺序，固定90秒/4GiB并复用集成独占闸门；不替代 Gateway/Web、独立Backend重启、Windows 或整个shutdown期限。

`integration-attachment-publication` 经消息ASGI入口和model_stream生成普通文件三种输入与上传附件的同一历史，核消息详情、列表及Turn的32组损坏引用拒绝、原canonical与request_hash不变；固定120秒/4GiB及集成独占闸门，不代Gateway/Web、进程重启或Windows实机验收。

`integration-task-uri-publication` 经真实 Session、消息与任务 ASGI 入口及 model_stream 生成历史和定义，验证原 SQLite owner 拒绝复制后损坏引用、事务事实不变及同 revision 重试；固定120秒／4GiB，沿原集成独占闸门，不代 Gateway 业务代理、独立进程重启、Web或Windows验收。

`integration-history-failure-boundary` 经产品消息与 model_stream 生成历史，验证三个历史入口的真实布局／I/O 拒绝和修复后冷读，核原 canonical 字节及封存请求 hash；固定120秒／4GiB并沿共享集成闸门，不代替独立Backend重启、Gateway、Web或Windows验收。

`integration-web-diagnostics-lifetime` 用真实 Chromium 调用唯一聊天驱动的诊断入口，验证大正文读取后释放及旧缓存路径被拒绝；固定180秒／4GiB，使用显式 HTTP 正文，不代替真实 Gateway 完整 default。

`unit-workbench-build` 以真实文件复制验证扩展构建只过滤复制源内部目录，固定60秒／4GiB；不启动服务，不代替完整扩展编译或发布验收。

`integration-web-metadata` 运行静态产物与真实 Vite 两个参数用例，经 Gateway 核初始化、原生目录编辑、设置刷新与日志持久化；固定600秒/4GiB，保留各业务操作原期限。不能只验静态首页或健康接口。

`integration-web-react-surface` 直接加载新 ReactSurface 及完整产品样式，用真实 Chromium 核对双根错误、Provider 初始化、浮层、焦点、释放和样式隔离；固定90秒/4GiB。属于组件 Integration，不装配 Gateway 或模型，不代替完整聊天与多 Thread 业务隔离。

`integration-session-creation-io` 经消息、Agent/model_stream和ASGI fork入口核对Session冻结、准备、rename、发布CAS与隔离中止中的真实SQLite写锁及取消；原key/gate持续到I/O结算，同请求恢复原目标或原aborted记录。固定180秒/4GiB及原60秒fork watchdog和集成闸门；不代表Gateway/Web、独立Backend重启、Windows或全shutdown期限。

`integration-fork-confirmation-io` 经产品消息、Agent 和 model_stream 生成带附件来源，核新 Session main/child 的异步发布确认、线程池排队及确认写入前后取消；保留原 gate、published 事实、附件结算和同 key 恢复。固定180秒/4GiB及原60秒fork期限，复用集成独占闸门；不代表独立进程重启、Gateway/Web、Windows或整个shutdown期限。

`e2e-web-session-navigation` 加载正式 Workbench，通过真实 Gateway 验证会话/工作区目录、拖拽、真实模型历史、fork 与删除后的编辑器清理；固定300秒/4GiB，保留原60秒 fork 期限，不替代完整聊天或 Electron 验收。

`integration-thread-attachment-isolation` 经真实 Gateway 消息与 fork 创建 main/child，使用 model_stream 验证 sibling 并行、各自 FIFO、未来输入隔离、独立上传关联及硬重启历史；固定180秒/4GiB及集成独占闸门，不代表 Web、Windows、GC 或全部 Thread 隔离验收。

`integration-thread-rewind-isolation` 经真实 Gateway/Backend 消息、fork 与 main regenerate 验证 child 完整存储和请求隔离、硬重启后的历史及 child 续聊；只读核对原封存请求与 owner/hash。固定180秒/4GiB及原集成独占闸门，不代替 compaction、Web、Windows 或完整8.3。

`integration-thread-input-admission` 经 ASGI 消息、回退和手动摘要入口及真实 Agent/model_stream 验证同 Thread 的 acceptance→Job 登记互斥，以及同 Session 的 child 并行；关键历史与 child 经产品入口生成。固定180秒/4GiB及原集成独占闸门，不代替真实 Gateway 业务代理、Web、重启或 Windows 验收。

`integration-thread-compaction-isolation` 经真实 Gateway/Backend 消息生成六轮长历史和 child，再由手动摘要入口及后继执行生成摘要，核 child 完整存储不变、硬重启及未摘要历史续聊请求。两项 Thread 视图 suite 分别选择单个用例，串行运行，各保留180秒/4GiB及原集成独占闸门，不代替 Web、Windows 或完整8.3。

`integration-child-compaction-isolation` 经正式工具选择、真实消息/fork/child 摘要工具及归档读取，核 main 完整存储隔离、硬重启和双方续聊请求；固定300秒/4GiB及原集成独占闸门。模型 usage 由原 model_stream 编码器提供，不修改产品摘要阈值，不代替 Web、Windows 或完整8.3。

`integration-resource-cold-sync-retry`经正式资源API、真实Node/PTY或Chromium/HTTP及冷owner验证同步失败后显式重试原版本、未知占用保护与终态结算，固定90秒/4GiB和原集成独占闸门；不代替Backend硬重启、Gateway/Web、Windows或全shutdown验收。

`integration-shared-backend-history-restart` 通过真实 Gateway／共享 Backend、双挂载 Session 和消息入口，分别验证 Backend 整组重启及 Gateway 冷重启后的完整组、原历史与续轮请求。模型复用 model_stream，固定360秒／4GiB和集成独占闸门；不代替Web、Windows、任意故障恢复或全部多工作区验收。

`integration-sqlite-diagnostics` 经实际 Session 创建、工作区存储冲突／结构拒绝与冷 container 重试，核原件、原因链和真实日志 I/O 次错；固定60秒/4GiB，不代表独立进程重启、Gateway/Web或Windows实机。

`integration-catalog-maintenance-errors` 经实际Session创建、导航worker、启动拒绝与只读备份验证维护错误及原件保全，复跑原完整性场景；原导航schema回归单列 `integration-catalog-maintenance-schema`。两组串行、各固定60秒/4GiB，显式恢复产品备份不代表自动修复或独立进程重启。

`integration-catalog-access-errors` 经实际 Session HTTP 创建、SQLite 回滚失败隔离及冷 container 重开核对 409、request_id、原件与身份；固定60秒/4GiB，ASGI装具不代真实网络、独立进程重启或Gateway/Web。

`integration-fork-source-scope` 经真实消息、捕获及 fork 入口验证私有来源解析失败的错误归属、原意图中止、来源不变、作用域释放与新意图发布。固定60秒/4GiB及原fork期限，复用model_stream，不代Gateway/Web、OS重启或Windows实机。

`integration-fork-prepared-control` 经真实消息和 fork 入口建立未发布目标，核只读校验不补建空/缺表控制库、不切换 journal、不重建缺失文件；故障恢复后由同一操作完成发布与幂等重试。固定120秒/4GiB及单次60秒fork期限，使用 model_stream，不代表独立进程重启、Gateway/Web或Windows验收。

`integration-fork-child-publication` 经消息／Agent 生成带附件历史，核 child 发布后确认写入前后及阶段观察失败的原错误、唯一目标、附件结算和同 key 恢复。固定180秒/4GiB及原60秒fork期限，复用 model_stream 和集成独占闸门；不代表真实网络、OS重启、Gateway/Web或团队验收。

`integration-chatgpt-auth-errors` 经真实认证、模型工厂、Agent服务及ASGI路由验证凭据读取、结构拒绝及迁移/刷新发布失败的错误归属、文件不变与同入口恢复，固定60秒/4GiB；仅替换原生故障和路由依赖装配，不代表真实OAuth网络、Gateway、独立Backend或Web验收。

`integration-compaction-history-errors` 经工具选择、消息 API、真实 Agent 和 model_stream 验证归档缺失与真实日志 ENOTDIR 次错、canonical 工具结果和模型协议配对、后续执行及封存请求 hash。固定120秒/4GiB及集成独占闸门；ASGI 装具不代真实 HTTP、Gateway、进程重启或全部压缩验收。

`integration-compaction-activity-errors` 经消息入口生成六轮历史、调用正式压缩工具，再用真实归档目录冲突及日志 ENOTDIR 验证原错误、专属活动失败事件、冷事件读取与原会话恢复压缩。固定180秒/4GiB及原集成闸门，复用 model_stream；不代 Gateway、真实 HTTP、进程重启或全部压缩验收。两个归档 suite 各选自身用例，避免重复执行或合并后超出原预算。

存放测试套件注册、能力矩阵和统一命令入口，不包含具体测试实现。

`integration-web-chat-tool-loop` 在完整 Workbench 静态产物、真实 Gateway/Backend 及 Terminal/Browser 辅助服务上验证工具循环和同 Session main/child 的双 Grid 草稿、并行调用与消息隔离；模型使用显式回放，固定360秒/4GiB，不代表全部旧页面场景已迁移。`e2e-web-session-information` 通过新 Workbench 的真实导航菜单与剪贴板验证有界会话诊断协议，固定300秒/4GiB。

`integration-web-resource-recovery` 验证真实 Gateway 认证恢复、资源可见性轮询、窄布局及 Thread 内浏览器连接，固定480秒/4GiB；显式的网络传输拦截保留原 Gateway 响应。`integration-web-progressive-history` 保留128轮装具、投影及分页性能预算，固定600秒/4GiB；`integration-web-context-inspection` 验证真实封存上下文的检查和刷新，固定300秒/4GiB。三者加载正式 Workbench，不代表完整聊天 E2E、旧外壳删除或 Electron 宿主已经通过。

`integration-web-child-thread` 使用固定模型回放及真实 Agent/task 委派，验证工作台首次与复用 child 标签的调试导航、SSE 和父子请求日志；固定420秒/4GiB，保留集成独占闸门，不标为真实 Provider E2E。

`integration-web-browser-views` 使用正式 Workbench、真实 Gateway/Backend 和 model_stream 验证浏览器本地视图的刷新与新页面恢复、双浏览器隔离和未打开会话的真实通知；保留 HTTP 账户租约、接管及真实45秒过期合同，固定300秒/4GiB，不代替跨设备同步或 Electron 重启验收。

## 可修改内容

- `matrix.jsonc`、ESM runner、套件过滤和结构化汇总。

## 不可修改内容

- 不要隐藏失败、吞掉退出码或把未满足前置条件记为通过。
- 不要让 E2E 套件回退执行 Integration stub 套件。

## 规范

- `integration-web-turn-source-freezing` 经真实 Workbench Composer／Gateway／受管 Backend 和 model_stream 验证工具循环中的 AGENTS Turn 冻结、下轮更新及安全重启后 API／UI／冷 Saver 原文与请求 hash；固定660秒／4GiB并沿集成独占闸门。公共构建装具、默认服务和 Section 8 总验收 owner 不变，不代替 model_call policy、Skill、团队或 Windows 验收。

- `integration-skill-selection-recovery`经真实Skill文件发布/工具调用、Saver准入与封存，验证模式切换、A→B→A选择、checkpoint/rewind/compaction、冷Saver及24→25→26维护迁移的原字节和hash，固定60秒/4GiB；冻结来源装具不代表完整Agent、Backend进程重启、Gateway/Web或Windows实机。
- `integration-context-source-seal`经真实Saver准入、Skill激活与SQLite/JSONL封存验证来源更新保留已接受及已封存选择次序、幂等冷读，以及原assembly提交/失败回滚/CAS/epoch合同，固定60秒/4GiB；关键输入由Saver装具生成，不代完整Agent、Backend进程重启、Gateway/Web或Windows验收。
- `integration-execution-input-commit`经Saver准入、真实SQLite/JSONL及冷Saver核对内部输入在提交前/后失败或取消时的正文、准入和队列原子性，以及原身份重试不重复写入；复跑既有队列投影/显式迁移及checkpoint提交窗口，固定60秒/4GiB，不代表完整消息执行、Backend进程重启、Gateway/Web或Windows实机。
- `integration-rollout-snapshot-release`经真实SQLite/JSONL与Saver准入验证快照建立、取消及关闭失败后的连接／文件锁释放，并复跑原硬退出恢复与独立reader合同，固定60秒/4GiB；不代表完整消息执行、Gateway/Web或Windows实机验收。
- `integration-fork-live-diagnostics`通过产品执行历史、ASGI公开fork和真实文件循环／日志故障核对unknown租约、已提交关联及同key恢复，固定180秒/4GiB并保留原fork watchdog；外设传输为替身，不代表真实PTY/Chromium、Gateway/Web或Backend重启。
- `integration-fork-live-deadline`经产品消息执行、ASGI公开fork、真实原owner锁竞争及同key重试核对已发布目标、未派发回执与唯一关联，固定90秒/4GiB和集成独占闸门；仅推进原关联预算，不覆盖原fork watchdog。外设传输为替身，不代真实PTY/Chromium、Gateway/Web或Backend重启。
- `integration-fork-export-diagnostics`通过产品消息／Agent生成来源及公开fork验证已发布目标、释放pending、原取消、冷catalog与周期恢复，固定120秒/4GiB并保留原60秒fork watchdog；传输为进程内替身，不代真实网络、Backend重启或Web验收。
- `integration-fork-source-intent-errors`通过产品消息与真实Agent生成来源，验证远端intent字段拒绝、真实日志写入次错、零目标副作用及原key重试，固定180秒/4GiB并保留原fork watchdog；传输为进程内替身，不代真实HTTP、Gateway/Web或Backend重启验收。
- `integration-skill-catalog-publication`经真实文件watcher、目录owner、catalog派生及SQLite封存装具验证来源失败/日志次错后的继续发布与同名覆盖历史绑定，固定60秒/4GiB；Gateway来源是显式替身，不代产品执行、HTTP、独立Backend重启或Web验收。
- `integration-terminal-operation-receipts`显式选择原7场景，保留90秒/4GiB；`integration-terminal-lease-notifications`单独选择真实PTY完成后的通知／日志故障2场景，固定60秒/4GiB。两批复用同一正式工作区和场景实现，必须串行运行；不以扩大截止或减少断言处理总量增长，超时仍如实记为TIMEOUT。
- `integration-workspace-file-navigation-errors`经真实文件owner、SQLite与ASGI路由验证原生访问及控制面回复错误、上传碰撞、文本预检和operation／日志双重故障；核原结算、未知效果、FIFO与worker重启，固定100秒/4GiB，不代表独立Backend、Gateway/Web或Windows实机。
- `integration-gateway-profile-errors`经真实Gateway用户入口生成账户/profile并制造原生目录与日志写入双重故障，核原HTTP分类、request_id、账户副作用、原件保全与同入口恢复；固定90秒/4GiB，不启动Backend、Web或真实Provider。
- `integration-test-gate` 经两个隔离Git worktree中的真实pytest进程验证集成/E2E在服务装具前排队、清理后交接、强杀释放和xdist串行；固定90秒/4GiB，不启动用户开发服务。普通pytest集成/E2E也自动加载相同闸门；排队占用原suite外部期限，不能把超时算通过。
- `integration-service-json-errors` 经真实辅助服务 HTTP 验证 JSON 解析失败的诊断边界及同入口正常请求，固定90秒；Linux按每进程4GiB数据段保护，不代表整树总内存预算，也不代表 Python owner、Gateway／Web 或 Backend 重启验收。

- `integration-browser-governor-diagnostics`通过真实Node/HTTP及受控Linux内核读取验证治理健康状态与原始日志边界，固定60秒/4GiB；不代表Chromium、Gateway/Web或其他平台实机。
- `integration-browser-websocket-input-errors`经真实Node WebSocket/HTTP及Chromium验证输入拒绝、零资源副作用和同连接授权恢复，固定90秒/4GiB；不代表Gateway/Web、Python业务owner或Backend重启。
- `integration-terminal-websocket-input-errors`经真实Node WebSocket/HTTP和PTY验证JSON/schema/type拒绝、附加前零资源及同连接原生shell写文件，固定60秒/4GiB；不代表Gateway/Web、Python业务owner、Backend重启或Windows实机。
- `integration-browser-runtime-diagnostics`经真实Node/HTTP与Chromium验证原生启动失败、日志目录失败下的创建归属和后续页面恢复；仅替换原launch参数和writer边界，固定60秒/4GiB，不代Gateway/Web、Python业务owner、Backend重启或Windows实机。
- `integration-browser-storage-diagnostics`经真实Node/HTTP/Chromium截图、原生目录冲突及日志writer次错核对原操作、冷记录和同入口PNG重试，固定60秒/4GiB；不代其它存储出口网络验收、Gateway/Web、Python业务owner或Backend重启。
- `integration-terminal-storage-diagnostics`经真实Node/HTTP/PTY验证resize发布冲突、日志次错、同尺寸显式保存及原进程回收，固定60秒/4GiB；不代创建失败窗口、Gateway/Web、Python业务owner或Backend重启。
- `integration-terminal-exit-diagnostics`经真实Node/HTTP/PTY父子验证Worker错误和日志次错时的停止、致命处理前原记录发布及冷Manager读取，固定60秒/4GiB；不代未知进程接管、Gateway/Web、Windows或其它生命周期错误验收。
- `integration-terminal-process-diagnostics`经真实HTTP/PTY验证原生元数据、扫描、信号及日志次错后的unknown、原身份与同入口管理重试，固定60秒/4GiB；不代Python lease、Gateway/Web、Backend重启或Windows。
  ps/taskkill与探测双错在真实Linux资源上显式替换平台查询边界，日志writer仍制造真实ENOTDIR，不作Windows/Darwin原生结论。
- `integration-terminal-process-release`选择既有Linux/Windows原生进程树释放测试，固定60秒/4GiB；只按实际平台结果报告，skip不能代替该平台验收。

- `integration-fork-capture-receipts` 使用真实 B→A→C Gateway/SSH 和 model_stream，从用户消息入口生成来源，核冻结坐标、双端 journal、公开回执及来源离线后目标进程重启、幂等重试和新输入。固定240秒/4GiB及原60秒fork预算；小负载合同验收不替代完整附件/Goal负载的期限与恢复验收。

- `integration-fork-prefix-integrity` 核对缺失／损坏必需正文时不发布目标，以及非前缀模式的严格 revision，固定180秒/4GiB和原60秒fork监督器。
- `integration-fork-prefix-receipts` 复用三端回执装具，以 item-only 和过期观察 revision 创建真实远端前缀，核对尾部排除、实际捕获坐标及来源离线后的目标进程重启与重试；固定240秒/4GiB和单次60秒fork期限。
- `integration-fork-item-lookup` 通过真实消息、回放、前缀和完整 fork 入口生成非活跃、非祖先分支，核对 item-only/显式 view、inclusive/before 的真实有序前缀及观察 revision 推进，固定180秒/4GiB。业务 HTTP 使用 ASGI transport，Gateway 管理来源与 Terminal/Browser 为真实服务；不替代真实 Gateway 代理、task 前缀创建、一致捕获或恢复验收。
- `integration-fork-scope-contract` 覆盖最近完成normal/internal范围、真实空来源、原admission/hash和工具配对，以及公开idle fork的零member/initial intent；固定180秒/4GiB与单次60秒fork预算。来源由持久owner装具准备，定位为存储/服务合同回归，不代替消息/Agent/Gateway/Web完整验收。
- `integration-task-item-lookup` 复用消息／回放与完整 fork 生成独立根，通过真实模型 task 验证非祖先候选、显式旧 view、相同历史 ID 的精确 caller、先提交调用再捕获及 child 首次请求；另核无候选和来源覆盖拒绝。固定360秒/4GiB及单次60秒fork期限。业务HTTP使用ASGI transport，复用真实Gateway管理来源、Terminal/Browser与model_stream，不替代Gateway业务代理、进程重启或Web验收。
- `integration-fork-prefix-consistency` 通过真实消息及回放，在创建 intent 后与私有快照复制后设置屏障，核对来源追加／分支切换、真实独占 writer 等待、冻结成员顺序、同键重试和 child 首次 Provider 请求；固定240秒/4GiB与原60秒fork期限。业务 HTTP 使用 ASGI transport，复用真实 Gateway 管理来源、Terminal/Browser及model_stream；不替代跨端、OS重启或Web验收。
- `integration-fork-prefix-restart` 经真实 Backend HTTP 和 OS 硬重启，在 marker 已落盘／captured CAS 前及 Session main 已发布／回执前核对来源推进后不重选、原创建及 capture、两次冷启动、有序前缀和目标首次 Provider 请求。复用原进程、Terminal/Browser与model_stream装具，固定300秒/4GiB和单次60秒fork预算；不代表 Gateway 业务代理、跨端或Web验收。
- `integration-task-prefix-restart` 经真实模型task与Backend HTTP，在快照落盘、child发布及初始执行准入后绑定前硬杀，核排队输入推进后复用原capture/child/Job、有序前缀、首请求和二次启动幂等。复用原进程、Terminal/Browser与model_stream，固定480秒/4GiB及单次60秒fork预算；不代表Gateway业务代理、跨端或Web验收。
- `integration-fork-compacted-prefix` 经消息、HTTP压缩调度及后继模型执行生成真实摘要，再核对item-only／显式view、inclusive／before、有效上下文与展示顺序以及child首次请求，固定240秒/4GiB及原60秒fork预算。
- `integration-fork-frozen-prefix-integrity` 经真实消息与工具循环核对未闭合前缀拒绝；捕获后中断并损坏原revision／manifest／正文hash，公开重试不得回源或发布目标。固定240秒/4GiB及原60秒fork预算。两套件业务HTTP使用ASGI transport，复用真实Gateway管理来源、Terminal/Browser及model_stream，不替代真实Gateway代理、进程重启或Web验收。

- `integration-mcp-real-provider-discovery` 使用现有 `CCTQ_API_KEY`、真实 Provider 与受控 MCP，经 Gateway／Session／Agent 验证完整定义、两轮多调用、冷配对和请求 hash；固定660秒／4GiB。显式选择该 suite 前须将现有凭据加入进程环境，缺失时报告 UNMET_PREREQUISITE，不回退到回放；token 估算、Provider用量和缓存观测各自保留，不代表稳定命中或跨执行持有。

- `integration-mcp-tool-approval`沿真实Gateway、Session消息、原图interrupt及HTTP MCP验证逐调用审批、直接工具编辑、同schema替换、期限、撤权与取消；完整9场景实测582秒，保护期限720秒/4GiB。`integration-web-tool-approval`经真实Chromium验证刷新后读取原检查点、编辑与拒绝，固定240秒/4GiB。均复用model_stream，不代表真实外部Provider或旧socket可恢复。

- `integration-mcp-wait-admission`通过真实Gateway、Agent、OAuth/MCP及model_stream验证用户输入与after_turn唤醒串行、活动等待阻挡删除/取消后删除不复活、终态先完成后登记不丢通知，以及原300秒期限不因成功授权续期。固定600秒/4GiB，与原等待崩溃/child恢复套件使用不同测试文件工作区；不代替完整HITL、跨Gateway或真实Provider验收。

- `integration-web-persistent-tasks`经完整Workbench、真实Chromium、Gateway和工作区后端，由页面创建/编辑任务并核对试运行、仅保存动作、真实409回读、管理控制、刷新、后台恢复/失败、生成会话自动打开及跨工作区任务/Goal隔离。四个模块分别构建隔离Workbench产物后，整组保护期限600秒/4GiB；保留各场景原期限，复用既有model_stream产品装具，不代替生成器迁移回退或完整模型判断验收。

- `integration-runtime-context-publication` 通过真实 Session、模型准备与公开 fork 入口验证仅发布事件不创建 Job/Turn、已发送请求不变、下一次准备消费及 fork 锚点闭包；提交前/后故障经全新 Saver/dispatcher 核对冷 owner 恢复。复用 model_stream 和正式 fork watchdog，固定240秒/4GiB，不代表持久任务完整运行时、进程重启或主 Web 验收。

- `integration-web-mcp-authorization` 通过真实 Chromium、Gateway、Session/Agent、同一 OAuth/MCP 资产及 model_stream 验证会话授权入口、私有弹窗回调、工作区底部标签持久化、完整定义和真实调用；固定240秒／4GiB，不代替远程 Gateway、真实 Provider 或完整后台等待恢复。

- `integration-web-mcp-remote-authorization` 复用上述浏览器交互与协议资产，经三个独立工作区、Gateway 和真实 localhost SSH，分别核对 turn/model_call 下 A 页面回调到 B、唯一远端调用、请求 hash、凭据归属及 A/C 操作隔离，固定480秒／4GiB。远程投影只发送 Gateway 选择头，本地目标仍核对 Backend UUID；不代表真实外部 Provider、跨执行持续持有或完整错误展示验收。

- `integration-web-mcp-connection-errors` 复用同一 Chromium、Gateway、OAuth/MCP 与 model_stream 装具，从底部面板实际产生拒绝、取消、原300秒期限到期、重新授权和断开；按精确操作ID核对历史和原资源账本，不预填状态或缩短业务期限，固定480秒／4GiB。不代表自动continuation、外部Provider或进程重启验收。

- `integration-tool-discovery-policy` 通过真实 Gateway、Agent 和 HTTP MCP 核对首次发布前已禁用目标的身份、硬隐藏、配对拒绝、远端零执行及封存 request_hash，固定160秒／4GiB。

- `integration-mcp-connection-wait` 经真实 Gateway、Agent、OAuth/MCP 与 model_stream 验证原配对等待、无新用户 Turn 的独立 continuation、两种激活边界及 mailbox／dispatcher 崩溃窗口，固定600秒／4GiB；不替代 Web 授权交互或真实 Provider 验收。

- `integration-mcp-connection-control` 经真实 Gateway、Backend、Session 消息及 Agent 发现／信封入口，验证首次 OAuth 未授权时的本地控制、兄弟 Server 调用、受信 operation 身份、短 status、参数拒绝与 disconnect，固定240秒／4GiB；模型复用 model_stream，不替代授权 UI、长等待自动继续或真实 Provider 验收。
- `integration-mcp-oauth-startup` 使用真实 HTTP OAuth／MCP 与真实 Backend 启动，核对首次需授权的局部状态、兄弟 Server、同 request_id、SDK PKCE／state、冷刷新和敏感信息边界，固定90秒／4GiB；不替代产品 connect、授权UI、持久等待或Gateway／Web验收。
- `integration-mcp-stdio-recovery` 使用真实 Backend、单实例 OS 锁与 SDK stdio，覆盖 POSIX 非合作进程强制关闭、关闭期限失败及旧实例停止后的 OS 退出／claim 冷恢复，固定180秒／4GiB；不替代 Windows Job Object、OAuth、HITL 或 Web 验收。
- `integration-mcp-stdio-replacement` 使用真实单实例锁、stdio SDK、watcher、Runner和model_stream，核对停止意图先于关闭、停后再启、破坏性失败与同入口修复及兄弟工作区隔离，固定180秒／4GiB；不代替非合作进程终止、退役清理诊断、OAuth或Web验收。
- `integration-mcp-config-recovery` 通过真实 Backend OS 退出验证 MCP promotion 前后、立即／lease 到期重启、原 claim fencing、连接重建和真实 HTTP SSE 重放，固定240秒／4GiB；不代替 stdio 停后再启、OAuth或Web验收。
- `integration-mcp-config-replacement` 通过真实双挂载、配置 watcher、Runner、model_stream 和 HTTP MCP Server 核对旧 Turn／新 Turn 连接归属、部分失败、混合待重启及工作区隔离，固定180秒／4GiB；不替代进程崩溃恢复、OAuth或Gateway／主Web验收。
- `integration-extension-discovery` 复用正式 fork watchdog，覆盖 Owner 发布、冻结完整定义、搜索／分页／策略、实际输出中间件、冷恢复、真实 stdio 信封调用及 stdio／HTTP 进度与原请求取消，固定 180 秒/4GiB；不替代完整 Agent、授权或主 Web 验收。

- 所有 JavaScript 使用 ESM 并通过 Bun 执行。
- `integration-resource-record-errors` 经正式 Session、ASGI 资源 API 与真实 PTY/Chromium 核对记录发布失败、原资源与租约不变和显式重试，固定90秒/4GiB；不调用模型，不代替 Gateway/Web 或 Backend 进程重启验收。
- `integration-resource-lease-errors` 在同一正式测试文件内选择授权账本失败场景，核原工具 active lease、零授权派发、冷账本与显式重试，固定90秒/4GiB；与记录场景串行运行，复用同一隔离配置和辅助服务，不创建第二套装具。
- 矩阵显式声明层级、客户端、运行面、命令和真实前置条件。
- `integration-shutdown-message-preparation`以真实双挂载、Session/消息HTTP与上传/canonical提交核对准备窗口和关闭竞争，固定120秒/4GiB；复用model_stream，不替代Gateway/Web、Windows或独立进程重启验收。
- `unit-workspace-services` 递归发现 Browser、Terminal 的服务端、attach 页面纯逻辑及协议 JS 单测；它不启动实际辅助服务，不能代替真实进程 Integration 验收。
- `integration-terminal-startup-cleanup`经真实HTTP创建、PTY父子进程及显式就绪交付故障验证停止证明与原管理入口重试，正常／失败日志均核原进程树、worker及持久记录；另核cwd拒绝的原intent、零PTY及同入口恢复。固定60秒/4GiB，不代表Windows、Gateway/Web或未交付PID的原生阻塞。
- `run-workspace-services.mjs` 按静态导入的测试框架分组：`node:test` 交给Node原生测试器，`bun:test` 交给Bun。两组都执行并汇总失败，未知或混用框架在启动前拒绝；各进程沿用矩阵的超时与内存保护。测试器的编码等运行时语义须与被测行为匹配，不能用Bun的Node兼容行为代替Node服务端证据。
- `integration-browser-element-references` 使用真实Chromium验证辅助Browser服务的DOM引用与动作，固定60秒/4GiB；不代表Gateway或主Web产品链路验收。
- `integration-terminal-shutdown-signals`通过真实HTTP/PTY及POSIX信号核对在途创建与关闭重试，显式就绪交付屏障保留原Worker结果，固定90秒/4GiB；不代表Windows、Gateway/主Web或整个shutdown总期限。
- `integration-browser-checkpoint-form-state` 使用真实Chromium与HTTP表单验证检查点捕获、DOM变更后的定位、释放后恢复及大型匿名表单的扫描成本，固定60秒/4GiB；Manager落盘边界为空操作，不代表持久化或主Web验收。
- `integration-persistent-task-scripts`经任务API和真实子进程验证未就绪到首次就绪、冻结输入、四类决定、迟到revision、期限/输出失败及真实Backend硬重启，复用model_stream，固定240秒/4GiB；不替代条件触发策略、模型条件、Goal/生成器迁移或主Web验收。
- `integration-persistent-task-matches`经任务API、真实脚本和model_stream区分首次/边沿/每次命中，核对试运行与管理操作不重置事实及真实Backend硬重启，固定240秒/4GiB；不替代外部通知、模型判断、Goal/生成器或主Web验收。
- `integration-persistent-task-notifications`经受认证的任务入口、真实脚本和model_stream核对外部输入、并发同键重试、异内容冲突、暂停/编辑及Backend硬重启后的原输入/接受身份，固定240秒/4GiB；不替代工作执行事件、模型判断、Goal/生成器或主Web验收。
- `integration-persistent-task-rules`通过任务API与model_stream验证JSON Pointer、缺失/null、bool/number与结构相等、原条件策略、暂停试运行及checking硬重启，固定240秒/4GiB；不替代模型判断、Goal/生成器或主Web验收。
- `integration-persistent-task-execution-events`通过真实用户/内部执行、任务API与model_stream核对原canonical终态引用、来源游标、暂停/更换来源及接受前Backend硬重启，固定300秒/4GiB；不替代模型判断、Goal/生成器迁移或主Web验收。
- `integration-persistent-task-models`经原执行准入与model_stream验证四类决定、无效/取消、独立判断Thread、忙时串行、迟到/试运行及接受后硬重启；固定360秒/4GiB，不替代Goal/生成器迁移或主Web管理验收。
- `integration-persistent-task-states`经任务、消息及公开fork入口验证同值合并、A→B→A、发布/唤醒原子回滚、接受后硬重启及历史状态闭包；复用model_stream、真实Terminal/Browser和正式fork watchdog，固定360秒/4GiB，不替代Goal/生成器迁移或主Web管理验收。
- `integration-persistent-task-schedules`经任务API、受控时钟、model_stream和真实Backend硬重启，核对interval/cron、时区、补跑、暂停恢复及原计划身份，固定240秒/4GiB；不替代脚本/模型条件、Goal/生成器迁移或主Web验收。
- `integration-persistent-tasks-manual`通过任务 API、原 source owner 与 model_stream 核对手动触发、试运行、忙时排队、revision 以及接受后硬重启对账，固定300秒/4GiB；不代表定时、脚本/模型条件、Goal/生成器迁移或主Web验收。
- `integration-persistent-task-goal-storage`通过Goal、消息与fork公开入口、真实Backend重启及Terminal/Browser核对唯一任务owner、准备可见性、发布重试和取消清理；覆盖续跑接受前、执行完成后标记保存前/后、在途模型的原目标/暂停/新目标、受支持工具checkpoint续接、独立fork Goal发布窗口和无专用Goal准入根的用户创建目标。核对原归属、工具配对、精确用量及二次重启无重复；新增四项后总保护期限900秒/4GiB，原场景期限不变，不代替生成器完整门或主Web验收。
- `integration-persistent-task-goal-fork-restart`定向核对独立fork Goal安装后发布前、发布后回执前硬退出及原安装/发布/撤销回归，固定300秒/4GiB，复用原60秒fork watchdog。
- `integration-persistent-task-goal-user-restart`经真实消息、create_goal工具及Backend硬退出验证用户首轮在途/已完成且无Goal准入根的恢复；沿既有45秒Job终态入口核对原Goal、唯一内部续跑、用量及二次重启，固定240秒/4GiB。首次冷prepare性能单独留TODO，不代表恢复耗时SLO达标。
- 每个已启用 suite 都带数据段内存上限与硬超时；被上限或超时中止的套件必须以 TIMEOUT 或非零退出码报出，不得记成通过。
- `integration-resource-operation-admission`经正式创建及原工具operation验证真实PTY/Chromium资源锁等待到期和显式重试，固定90秒/4GiB；不代表整个工具执行、Gateway/Web或Backend重启验收。
- `integration-resource-thread-admission`经正式资源入口和导航worker验证短Session准入、同Session独立资源并行及原holder退出后的删除排空，固定90秒/4GiB；不代表整个工具operation的截止或终止核实。
- `integration-resource-cold-management`经正式Session/ASGI创建与执行、冷owner和真实Node/Chromium核对恢复取消后的stop/delete，固定60秒/4GiB；原脚本由context关闭后真实结束，不代表Backend硬重启、Gateway/Web或任意脚本强制终止。
- `integration-context-source-epoch-copy`复用正式fork watchdog，核待消费、已消费和继续执行后的来源版本、epoch transition与封存前缀复制，固定120秒/4GiB；不替代完整上下文或Web验收。
- `integration-resource-uri-aggregate`复用正式fork watchdog，核真实factory发布的内部聚合在seal、SQLite冷读及存储复制后保持显式null URI、正文和hash，固定60秒/4GiB；来源由factory装具生成，不代替公开fork、Backend进程重启或Web验收。
- `integration-resource-activation-rejection`经真实Session／消息ASGI与model_stream生成封存历史，核非法资源激活输入的冷读拒绝、完整诊断、已提交原件不变及原会话续跑，固定60秒/4GiB及集成闸门；与原聚合suite各选自身用例，不重复执行，不代表Gateway／真实HTTP、独立Backend重启、Windows或执行失败持久化出口验收。
- `integration-resource-uri-read`只选择A04资源目录、搜索和模型read场景，经三个真实Gateway、Backend及localhost SSH核对冻结目标、冷目标零物化和无路径投影，固定180秒/4GiB；复用model_stream，不包含延期的send/wait协作场景，不代替真实Provider或Web验收。
- `integration-context-source-wire`核来源封装经过同步/异步真实HTTP、跨协议fallback、ToolSet切换及冷fork/后继fork时的正文、引用与hash，固定240秒/4GiB并复用正式fork watchdog；外部模型使用现有协议资产，不代表真实Provider或Web验收。
- POSIX套件在独立进程组中运行，根进程退出后仍回收组内后代；覆盖正常、失败、超时和中断路径。Windows suite 通过 `app/runtime/windows_job.py` 在独立 Job Object 中启动，挂起根进程直到完成绑定，覆盖整个进程树的内存上限，并在根进程退出或监督器关闭时回收后代；Windows开发目标测试也复用这套实现。
- 首次SIGINT/SIGTERM请求终止套件，保留30秒升级宽限；再次中断立即强杀，原套件强杀期限不重置。POSIX按信号终止进程组；Windows用 `taskkill /T` 终止监督器进程树，并在升级阶段加 `/F`。等待监督器退出后才返回，中断仍为130，超时仍为3及原TIMEOUT回执。

- `integration-extension-definition-recovery` 通过真实 Runner 发现、压缩和 fork，使用正式 fork watchdog；不代表外部 Provider 或 Web 验收。

- `integration-mcp-tool-approval-recovery` 经真实 Gateway／消息／审批入口生成多工具等待，再硬退出并重启 Backend；验证旧决定失效、原资格释放、未知结果与原执行同事务归属、新执行唯一 MCP 收据及请求 hash，固定240秒／4GiB；不替代跨 execution 持续资源持有、跨 Gateway 授权或真实 Provider 验收。

`integration-web-conversation-lifecycle` 是 Section 8 完整场景唯一入口，保留原 12600000ms／4GiB 与 focus collection preflight；`e2e-web` 不包含模型回放场景。

`integration-process-port-lease` 在 Linux 上以真实 TCP 验证进程租约阻止自动客户端抢占端口、真实服务仍可复用监听、服务退出后保留至租约释放，以及既有监听者不受影响；固定60秒／4GiB。不修改宿主端口范围，内核不支持每 socket 的 IP_LOCAL_PORT_RANGE 时明确跳过竞争检查，不代替完整会话验收或其它平台证据。

`integration-electron-packaged-workbench` 从正式发布入口构建并校验 Linux 便携归档，解压后以真实 Electron 连接 Gateway/Backend，核对 UI 会话创建与发送、原版虚拟文件保存、原生正常关闭、固定来源重启缓存先显和 profile 隔离；使用精确模型回放及真实 HTTP 延迟代理，固定900秒／4GiB，产物全部归测试镜像。记录实际 `--no-sandbox` 参数，不以配置代替进程沙箱证据；不代表真实 Provider、Windows/macOS、崩溃恢复或完整 Electron E2E。

- `integration-web-model-call-source-freezing`经独立工作区的真实Workbench/Gateway/Backend与model_stream验证显式AGENTS model_call、工具配对后追加及安全重启冷检查；固定660秒/4GiB，沿原集成独占闸门，共用默认Turn入口的浏览器驱动。默认构建仍由公共fixture执行；不将临时复用已核正式资产的产品通过记作默认构建或整个矩阵通过。

- `integration-web-mixed-source-freezing` 复用正式 Workbench 和来源冻结唯一驱动，以真实 Composer/Gateway/Backend 与 model_stream 验证 Turn AGENTS/model_call Skill 的同请求混合边界、原调用 Skill 选择及安全重启后的完整 API/UI/冷 Saver 检查；固定660秒/4GiB，沿原集成独占闸门。不改公共构建 fixture，不替代 Section8、Windows、真实 Provider 或默认构建期限的验收。
