## 1. 核对原入口与实现边界

- [x] 1.1 核对 design 盘点表中六项任务的所有直接／间接调用、测试与启动／关闭顺序，确认回调可重复扫描；区分 startup、周期、即时操作及 durable 业务恢复，不误迁 terminal steering 或 fork 恢复。
- [x] 1.2 盘点 Node／Python 公共管理、命令、日志、诊断、`LifetimeScope` 及收尾原语的真实依赖和所有产品／插件／测试／打包消费者，确定 SDK 迁入清单；只抽取真实通用运行库，不迁业务 owner 或复制产品模块。
- [x] 1.3 按 design 的进程职责表核对 Gateway、插件和 Backend 边界；明确公开入口、生成协议唯一归属及包内归档依赖，不要求所有服务继承公共基类，不新增每服务管理后台或跨进程维护 RPC。

## 2. SDK 目录、公开入口与协议

- [x] 2.1 按 design 建立 `sdk/node`、`sdk/python` 的实际包结构与公开子模块，声明包名、必要依赖和 Node exports；根入口与维护／生命周期入口保持无启动副作用，不传递加载管理／诊断重依赖；软件包版本沿根 `package.json` 派生。新增源码子目录补齐四段式 `AGENTS.md`，不铺空框架。
- [x] 2.2 调整 `buf.gen.yaml` 及 `scripts/generate_protocol.mjs` 的输出根、包标记、staging、check：全部 Python 生成协议只输出到 `sdk/python/src/boxteam/` 并完整进入 wheel；产品／SDK 全部消费者正常导入 `boxteam.*`，删除旧生成目录和 `sys.modules` 别名。生成／check 不导入应用、不安装 SDK，check 不回写；将原必需 HTTP／OpenAPI 校验及构建／CI 消费者迁至当前 SDK 正常安装后，发行验收仍用 wheel，核本次 descriptor。迁齐 Node SDK 所需生成依赖，移除客户端私有类型引用，不手写平行合同。

## 3. 公共能力按语言迁齐并删除旧入口

- [x] 3.1 将 Node 插件实例管理、命令及必要日志／诊断／控制公共实现迁入 SDK，同批迁齐 Terminal、Browser、Debug、其他产品消费者和测试；核原 HTTP 合同、请求身份与日志热应用保持，删除迁出旧入口，不保留 re-export 或回退导入。
- [x] 3.2 将 Python 插件命令、管理、诊断迁入 SDK；唯一 request_id 上下文置于纯标准库 `request_context.py`，请求 state 依赖及插件本机控制认证置于 `http.py`，保留 FastAPI 可解析的真实类型且不反向导入 runtime／commands。迁齐所有原消费者；TraceMiddleware 仍负责入口 ID、绑定／恢复和响应，gateway_id／actor 鉴权留在产品。删除旧入口，不复制 ContextVar、不注入可替换 getter、不保留 re-export；Node 保持显式 ID 参数。
- [x] 3.3 将原 `LifetimeScope` 与必要收尾原语迁入 SDK，迁齐所有消费者；在原 close 中结算已结束任务结果并向该次调用交付错误，后续显式 close 不重复历史任务错误，保留未释放资源重试及原顺序／取消保护。不增加 acknowledge API、错误账本或维护私有关闭器。
- [x] 3.4 从唯一 `resources/logging/runtime-contract.json` 构建两种 SDK 的只读包资源并纳入构建／check；加载改为包相对定位、按需初始化，迁齐原消费者与构建清单，移除顶层 sink 环境解析和 cwd／产品根寻址，不复制可写配置或新增热应用状态。

## 4. SDK 后台维护调度与真实接入

- [x] 4.1 按 design 的最小公开签名实现 Python SDK `maintenance.py`；统一整数 `interval_ms` 及原生等待上限，先验证整个固定集合再创建循环，后项非法不得部分启动；原 scope 持有任务，原生命周期拒绝重复装配，完成后等待正常周期，无重入／积压／动态调度，不再新增 `app/core/maintenance.py`。
- [x] 4.2 在 Node SDK 实现相同调度约束，使用原生信号与可等待排空；周期错误在开放阶段处理，停止期间的真实错误交给关闭调用；已结算任务不重放、失败 Promise 不永久卡住显式再次关闭，不增错误账本或完整 Node scope 框架。
- [x] 4.3 将五项小时级清理和 Backend Browser sweep 迁齐至真实工作区装配；保留首次一小时／Browser 首次立即及原 startup 恢复，维护 scope 在 periodic 关闭阶段排空后再关依赖。删除对应旧循环，全仓清理悬空调用；fork 周期任务与 Terminal steering 保持原位。
- [x] 4.4 将 Browser 插件截图周期维护改用 Node SDK，保留后台首次立即、每小时和原清理函数；删除原定时循环、重复 timer／Abort 持有，插件 shutdown 先等待维护排空再释放其依赖。Terminal 保留启动与创建时历史清理，不新增周期任务。
- [x] 4.5 接通周期到期／停止竞态、并发关闭、关闭等待者取消及启动后续失败；停止后不新进回调，Node 共用本次在途排空，Python 使用唯一 scope 结算，全部在途结果结算后才允许依赖释放，不加关闭状态机或额外 deadline。

## 5. 可重定位 SDK 与插件制品

- [x] 5.1 沿原脚本实现共用完整目录构建函数：SDK 归档放入 `vendor/sdk/<归档 sha256>/<标准文件名>`，以同一声明和标准锁准备依赖；内容改变采用新来源路径，不强刷包管理器缓存。插件开发加载目录，release 只打 ZIP；插件制品清除源码直跑、workspace／editable、绝对路径与隐含父级依赖，安装／启动不补装。
- [x] 5.2 调整 MCP stdio bridge 打包，删除复制产品 `app/` 文件的清单，使用随插件携带的 SDK；迁齐 Node developer-tools 包的 SDK 依赖和类型，保留原 ZIP 清单、目标 ABI 和原子发布行为。
- [x] 5.3 调整产品 Backend 依赖，开发使用标准 editable、发行安装构建好的 Python wheel，保证完整协议与轻量入口可用；已安装插件继续使用自身明确依赖，不新增产品侧隐式 SDK 注入或自动重打包。
- [x] 5.4 同批调整原依赖校验器和构建器的锁解析，增加仅 SDK 包内 tarball／wheel 来源，正确处理 Bun 三项锁记录、uv path／requires-dist，核原生摘要、声明／归档／依赖树身份与来源；保留 registry 规则，不放开其他本地来源，不新增摘要账本。
- [x] 5.5 迁齐开发启动清单、Launcher 解析／预检、bootstrap 和目录／ZIP 安装入口，共用校验；同一 Home 同版本不同原制品摘要仍拒绝，原来源同摘要重复安装仍幂等，不新增有效载荷摘要或自动卸载分支。先发布新制品，再经原配置 owner 更新所选绑定版本，保留 enabled／参数／范围，由原 reconcile 交接实例。新工作区默认项单独操作，不按 instance_id 更新，不直接写配置或另造启停编排。
- [x] 5.6 分离缓存输入与制品身份：输入指纹仅作缓存键；开发包在完整目录组装后，以基础清单版本计算实际目录摘要再派生版本，随后仅写 version 字段。SDK 更新来源及锁，纯插件代码复用未变依赖；不覆盖运行目录，不新增版本账本或依赖恢复分支。
- [x] 5.7 将先构建 SDK 归档、再准备产品／插件依赖、最后运行启动器的顺序接入原 worktree／依赖 bootstrap；需要重生成时先走纯协议生成，原应用合同校验只在当前 SDK 正常安装后执行，发行验收仍用 wheel。打包入口不导入待安装 SDK、产品模块或产品日志，避免初次安装自举依赖环。

## 6. 定向验证与交互成本

- [x] 6.1 从 Node／Python SDK 公开入口测试长回调不重入／不补跑、独立任务继续、失败后下一周期、重复注册拒绝、取消不重试；使用可控等待而非小时级真实 sleep。检查原管理、命令取消、日志与诊断合同，完成 Node 真实类型检查、两种 SDK 构建和独立包导入。
- [x] 6.2 经真实工作区生命周期装配验证活动清理 I/O 失败不妨碍到期附件清理、故障解除后下一周期恢复；已接受附件、配置保留事件及被引用详情不删除。另在真实 worker 的 I/O 边界用测试屏障核维护在途时的读取与保护规则，真实 owner／SQL 不替换，屏障等待不计性能，不新增生产测试开关。
- [x] 6.3 复用 Browser 资源装具验证 lease／Workspace 保留、原 owner／Gateway／插件核实链、失联不伪报释放、两个工作区及同插件两个实例的归属与停止隔离。
- [x] 6.4 用真实 worker／写入屏障验证取消后排空与依赖顺序，迟到错误由首次关闭完整返回，显式再次关闭不重放终态任务、仍保留失败资源重试；覆盖父子 scope、并发关闭和取消等待者，保留既有生命周期其他合同。Node 真实 SDK 装配验证同样结算、首次清理一次及失败恢复；补验 Terminal 事件清理与 fork／启动恢复语义。
- [x] 6.5 经既有安装／运行装具验收仓库外离线制品：Node developer-tools 的 Terminal／Browser／Debug、Python MCP stdio bridge 完成管理握手、各自真实业务操作与停止退出；无仓库导入路径、editable 安装或下载依赖。以独立 Python SDK 插件真实回调验证周期能力，不修改 bridge 业务造需求；验收完整 Backend 制品的 SDK 加载与工作区初始化。
- [x] 6.6 复用真实 Gateway／Backend，固定 3 批各 10000 候选／100 有效对照、一次 owner 清理及 20 并发暖分页请求，采集阶段外部硬超时 60 秒；全部 60 请求核内容／大小、request_id、p95≤500ms、max≤1500ms。单列真实重叠次数与样本，不设最少重叠通过门，无重叠如实报告观测范围；并发正确性另由 6.2 验。禁止 health、性能屏障／sleep／mock、自动扩数据／补批／调参／重试，保留失败样本，冷读沿原预算。
- [x] 6.7 对本批 Python／Node 改动完成相关静态分析、真实类型检查及上述受影响测试；所有测试带进程外超时／内存保护，保留合法输出目录中的命令、源码指纹、原始输出及退出码。不默认全仓／全套 Web／全平台打包，未验证平台如实说明。

  本轮证据位于 `out/tests/temp/2026/11/20/background_maintenance/artifacts/` 与 `out/tests/temp/2026/11/20/unify_background_maintenance_python/artifacts/`。SDK 测试按 `tests/unit/sdk/`、`tests/contracts/sdk/` 和 `tests/integration/sdk/` 分层，并在内部区分 Python／Node；通过正常安装的公开入口验证，独立制品另由离线集成验收。

  Gateway 维护在途读取的确定性补验见 `gateway-maintenance-concurrency-final.log`（1 passed，84.93 秒），完整响应及原 worker 结算状态位于 `out/tests/integration/gateway/test_maintenance_concurrency/`。该屏障仅用于正确性，不进入性能采集；原 60 请求性能样本保持不变。

  扩大验证仍有既存装具阻塞：fork scope 的两项在准备用户消息时未领取队列；frozen-prefix 的四项在启动时缺少 backend generation；Terminal SIGTERM 在冷重启前把带 `/services/http` 的 URL 当作端口解析。原件分别见 `fork-scope-contract-final.log`、`fork-frozen-prefix-integrity-final.log` 与 `sdk-test-consumer-terminal-signals.log`，未放宽断言或将其计为通过。真实 fork 启动恢复五项通过；本轮未重新进行 Windows／macOS 及正式发行包验收。
- [x] 6.8 在无插件环境／运行中事件循环的子进程验证轻量入口无启动副作用及重依赖加载，实际初始化仍报告非法配置；两种导入顺序核 Python wire 唯一及真实序列化。隔离装具保留旧 SDK 并新增由应用消费的协议类型，验纯生成→当前 wheel 构建／正常安装→原应用合同检查；check 不改源码／环境且拒绝过期产物。经真实中间件并发请求核命令／日志上下文同源、嵌套恢复、缺失 ID 拒绝，无第二 ContextVar、随机 ID 或旧入口。
- [x] 6.9 从构建制品换 cwd 验证日志包资源及源一致性，经原管理入口验日志热应用、去向、revision 和 request_id；不借源码资源补缺。
- [x] 6.10 用同一个构建目录，在隔离 Home／工作区验目录加载与 ZIP 安装的有效载荷、实际 SDK 来源、ABI 和真实业务行为一致；同一 Home 验目录→ZIP 及反向的同版本不同摘要拒绝、原来源同摘要幂等，原安装及绑定不变。经真实入口覆盖归档篡改、缺失、越界及名称／版本／来源冲突拒绝，不能 mock 校验器。
- [x] 6.11 隔离环境验首次 bootstrap；SDK 版本／文件名不变而内容变化时来源路径、锁及实际加载更新，旧归档不被覆盖；纯插件代码复用依赖。相同输入但实际载荷不同须获不同开发版本，同一构建的两种交付保持版本；失败不破坏旧目录。真实配置／reconcile 验绑定更新、禁用保留、未选绑定／默认项不变及原配置冲突拒绝。

## 7. 最小充分实现复审

- [x] 7.1 从原代码重新检查更直接实现，删除不必要包装、重复状态、fallback、结果 DTO、扫描缓存及测试专用生产配置；确认 SDK／原目录无双轨、无中央 GC／远程维护协议、每服务独立管理后台或无关清理策略变化。
- [x] 7.2 核对 SDK 不依赖产品私有模块／客户端／具体插件，生成合同只有一个源码 owner，Backend 与 Python 插件共用同一调度和生命周期实现；检查 Node／Python 包依赖完整、旧复制脚本和旧入口已删除。
- [x] 7.3 按实际迁入更新原目录和新 SDK 目录的 `AGENTS.md`；核对规范、最终实现、公开入口和验收证据一致，明确 Gateway 自身维护、federation 清理、其他插件清理策略及完整扩展 API 未纳入本次实施。

## 8. Thread 闲置周期接入

- [x] 8.1 AgentExecutionService 注册 Python SDK 周期扫描，移除自带定时循环；保留等待首次检查、60 秒周期、空 Context、独立故障观察及 tracker 的原闲置规则。SDK 返回原任务观察句柄，取消排空仅由原 scope 负责。
- [x] 8.2 维护 scope 沿原 runtime.shutdown 关闭，保留唯一关闭任务、30 秒观察、依赖持有、迟到原错交付和显式重试；同步直接测试及规范，不扩大其他 owner 范围。
- [x] 8.3 完成定向静态检查、SDK wheel 构建安装、驻留和真实双 Workspace 关闭验收，并复审无双轨、无私有任务集合读取或新关闭框架。
- [x] 8.4 修复 owner 关闭准入早于 scope.close 的同轮竞态：拒绝新扫描、交回在途原错；补齐失败重现与回归，并迁齐 runtime worker 意外终止集成用例的故障边界及 SDK 原错归属。
- [x] 8.5 移除维护任务存活与 Job 准入的耦合；意外停止独立报告。经用户／内部执行真实入口核维护取消及异常退出后连续新 Job 正常完成、runtime 构建及复用、canonical 完成终态，并保持 owner 关闭拒绝准入、用户取消和原关闭错误交付。
- [x] 8.6 修复等待已有闲置卸载的 admission 误收正常 blocker 撤回异常；重新检查并复用同代 runtime，保留配置换代阻断及真实释放错误。补齐确定性竞态单测与真实后台工作／用户及内部 Job 集成验收。

  本轮证据：`out/tests/temp/2026/11/21/thread_residency_maintenance/artifacts/`。Python 静态检查、当前 SDK wheel 构建／正常安装及来源 check 通过；55 项定向单测通过（3.76 秒），真实双 Workspace 驻留关闭三项通过（47.76 秒，含默认 30 秒观察期限），未运行 Web 或跨平台发行验收。复审确认只移除原周期循环和手工取消排空，实际驻留规则未变，无新账本或关闭框架。

  P2 复审证据沿用上述目录的 `p2-*` 原件：两个同轮竞态修复前均失败；修复后 57 项定向单测通过（2.86 秒），5 项驻留关闭／runtime 生命周期集成通过（57.21 秒），静态检查通过。未新增关闭状态或 SDK 接口，只沿原 owner 准入拒绝新扫描并将关闭期原错误交回唯一 scope；其他工作区及失败 generation scope 的保留／重试断言保持。

  最终解耦证据见同目录 `decouple-*`：57 项定向单测通过（3.03 秒）；补充正常关闭不误报停止的断言后，owner 单测 9 项补验通过（2.43 秒）。10 项真实 runtime／生命周期集成通过（89.28 秒），覆盖维护取消／调度异常 × 用户／内部执行，各连续完成两个 Job，验证 canonical 完成、首次编译及同代复用；同时保留关闭拒绝准入、真实用户取消、迟到原错和实际 30 秒观察期限。维护错误继续独立报告和沿原关闭链结算，未新增监督器或自动恢复。静态及 OpenSpec 校验通过，本轮未改 SDK 接口、Web 或跨平台发行链。

  卸载撤回竞态证据见同目录 `admission-race-*`：修复前同代 admission 回归失败；修复后 64 项驻留／runtime／关闭定向单测通过（2.86 秒），保留换代阻断及真实释放原错。2 项真实 Runner 集成通过（23.31 秒），由真实后台工作登记 blocker，用户及内部 Job 均完成、canonical 终态正确并复用原图和 scope。最小复审仅在等待已有卸载处分辨原阻断异常并重查，无新状态、重试任务或兼容链；静态与 OpenSpec 校验通过。未运行 Web 或跨平台验收。


## 9. Python SDK 本地 editable 开发

- [x] 9.1 Backend 开发来源迁到 uv 标准 editable，准备入口只同步版本、依赖声明和生成日志资源，不再为普通源码变化构建 wheel 或改锁。
- [x] 9.2 迁齐 release／CI 普通安装与产品依赖导出，保留带摘要 wheel 和插件制品规则；旧 wheel 自举与导入合同按实际安装方式验证，不用源码路径兜底。
- [x] 9.3 隔离环境验证源码编辑无需重装、包资源、只读一致性检查、移走源码后的 wheel 独立性及相关初始化／打包接线，完成静态检查和最小充分实现复审。

  本轮隔离证据位于 `out/tests/temp/2026/11/21/python_sdk_editable/artifacts/`：53 项 Bun 测试通过（含 SDK 安装元数据不触发无效重建），5 项导入／旧 SDK 自举合同通过；完整 Backend 制品初始化在前轮通过（同轮的自举失败已定位为继承外层 UV_PROJECT_ENVIRONMENT，隔离后补验通过）。实际验证 editable 源码修改无需重装、包资源一致性拒绝及修复、移走源码后 wheel 独立加载。Python 静态、JS 语法、锁文件和 OpenSpec 校验通过。未改主线程当前 .venv、未重启服务，未进行 Windows／macOS 实机或完整发行包构建。

## 10. Node SDK 本地 workspace 开发

- [x] 10.1 仓库内 Node 产品依赖迁到 Bun workspace；准备入口只同步版本、声明及生成日志资源，不为普通源码编辑构建 tarball，不引入搜索路径修改或全局链接。
- [x] 10.2 保留插件制品的独立归档合同；真实产品运行时清单入口仍生成包内 tarball 来源，不携带根 workspace 声明或开发源码链接。
- [x] 10.3 隔离安装验证源码编辑即时生效、日志资源一致性与只读 check、移走源码后的发行独立性，完成冻结锁安装、公开类型及相关运行测试和最小充分实现复审。

  本轮证据位于 `out/tests/temp/2026/11/21/node_sdk_workspace/artifacts/`：两种 SDK 的源码即时生效／独立归档安装场景通过；原 worktree／源码复制十项及发行清单七项通过。隔离 workspace 的 47 项 Bun SDK 场景通过，另两项原生 Node Worker 场景用 Node 入口通过；首轮误将 Node 原生测试混入 Bun 时收尾 hook 超时，保留原件且未修改产品或放宽断言。真实 TypeScript、冻结锁安装、准备一致性、JS 语法、差异和 OpenSpec 检查通过；源码指纹确认测试期间受影响实现未变。未重新安装根 node_modules、未重启服务，未进行跨平台实机或完整发行包构建。

## 11. 共享 SDK 测试归属

- [x] 11.1 迁齐 Python／Node 的纯 SDK 测试、公开合同和真实运行验收；拆开产品协作及打包自举，删除旧位置及悬空引用，不减少原有用例与断言。
- [x] 11.2 接通各引擎的受保护矩阵入口，迁齐镜像产物与目录指南；SDK 单测只准备必要的隔离环境，保留原进程保护及集成闸门。
- [x] 11.3 核对迁移前后用例清单，完成相关静态、定向运行与最小充分实现复审，保留实际验证证据。
- [x] 11.4 P2 复审修正 Python 集成套件外部期限：计入公共名额最多600秒的等待，自举及运行组合分别保留原500秒／90秒执行余量，不绕过独占闸门或改变内存保护。

  证据位于 `out/tests/temp/2026/11/21/sdk_test_layout/artifacts/`。原119个 Python 参数化用例及25个 Node 测试声明均保留；68个 Python 测试函数的 AST 核对仅有迁移后的堆栈文件名断言变化。Python SDK 单测59项、合同6项、运行组合3项、离线 SDK／Backend 制品3项及协议自举1项通过；五个 Node 矩阵入口全部通过，runner合同24项通过。复审无旧路径调用、转发文件或第二测试运行器；SDK 单测不再逐例复制产品配置，真实进程及安装验收保留。首次 Node 检查加载了安装目录中的旧 SDK 副本，正常冻结锁安装恢复 workspace 来源后补验通过，原失败证据保留；未改 SDK 生产行为，未进行 Web／跨平台或完整发行包验收。
