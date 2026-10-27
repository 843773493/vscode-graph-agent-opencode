## 1. 核对契约与迁移闭包

- [x] 1.1 全仓盘点 Goal、Gateway 生成器、Session Generation、Job 事件、工具/API、配置消费者、协议类型和测试，形成实施所需调用方清单，确认不存在旁路调度。
- [x] 1.2 核对当前上下文注入、itemized rollout、多工作区挂载与持久资源合同，明确复用端口及 owner 边界，避免另建上下文、thread 执行或资源状态源。
- [x] 1.3 将 Goal 状态/预算/激活恢复及定时任务三种策略、时区、补跑、overlap、回报、命名和 UI 行为映射到规格场景；确认旧数据迁移归属与停用顺序。

## 2. 工作区任务模型与管理入口

- [x] 2.1 定义带类型和版本的统一任务、组件、判断结果、运行记录与管理 DTO，区分生命周期、运行阶段和应用状态；定时执行只是时间触发加显式无附加条件判断的组合，不另建任务模型，迁齐相关协议与消费者。
- [x] 2.2 在工作区 `.boxteam/` 建立唯一任务 owner，支持 revision 校验、完整对象返回、幂等创建及运行状态事务写入。
- [x] 2.3 提供工作区级查询、创建、编辑、暂停、恢复、终止、立即检查、检查试运行和历史 API；默认工作区沿用启动解析规则，已保存任务始终明确寻址。
- [x] 2.4 接入现有 SSE 和多工作区挂载/卸载流程，核对同进程多工作区状态隔离及 Gateway 仅透明代理。

## 3. 触发、条件判断与可靠投递纵向链路

- [x] 3.1 实现统一触发记录与 interval/cron、关联工作执行结束、外部幂等通知及手动触发，持久化计划游标与定义 revision；时间到期由调度器判定，不启动脚本或模型检查时钟。
  - [x] 3.1.1 手动、interval/cron沿同一运行账本触发；计划游标与触发原子提交，保留时区、skip/run_latest/catch_up和冻结定义，恢复期间的新计划只排队。关联工作事件仍待实施；外部通知见3.1.2。
  - [x] 3.1.2 外部通知复用同一触发事务、冻结定义与原运行账本，持久payload/原request_id；覆盖并发同键、异内容冲突、暂停/编辑及真实Backend硬重启后的原接受结果。
  - [x] 3.1.3 执行结束绑定明确Thread URI，从原canonical终态提交恢复；原运行引用与来源游标原子接受，覆盖用户/内部执行、取消修正、重复事件、暂停/编辑/换源及丢失通知后的硬重启。
- [x] 3.2 提供规则判断及脚本判断组件，明确工作区执行位置、结构化输出、运行期限、资源保护与失败诊断，打通服务未就绪到首次就绪的真实入口。
  - [x] 3.2.1 script v1经共用原生监督器执行，冻结结构化输入、工作区cwd、UTF-8结果、期限/内存/输出限制及明确失败；真实入口覆盖服务未就绪到首次就绪、迟到判断和硬重启。规则、外部通知及条件策略另行验收。
  - [x] 3.2.2 rule v1消费同一冻结输入，用JSON Pointer和JSON值比较返回wait/proceed；真实入口覆盖缺失/null、bool/number、对象/数组、暂停试运行与checking硬重启。Linux本切片通过，Windows完整产品边界仍保留原长路径TODO。
- [x] 3.3 提供判断与动作上下文构造组件，保存输入来源/revision，支持一次性事件与同一来源状态更新，不复制上下文 owner 存储。
- [x] 3.4 通过现有 thread 执行准入实现模型判断器，绑定独立判断 thread、本次输入与执行身份，校验明确决定与理由，并防止判断重入和自触发。
- [x] 3.5 补齐仅发布上下文的业务端口，并复用 source 加 durable wakeup 实现发布后请求执行；覆盖忙时串行投递和明确的投递时机。
  - [x] 3.5.1 实现不可变事件的仅发布端口、原子 canonical view membership、幂等与冷 owner 恢复结算；通过空闲/忙时真实模型准备及公开 fork 验收，不以该子项代替任务动作协调、状态更新和投递时机验收。
- [x] 3.6 实现 first_match/rising_edge/each_match 条件策略与已触发事实，重复通知及同键不同内容冲突使用统一幂等入口。
  - [x] 3.6.1 三种策略复用原运行判断/接受事实，决定与策略选择原子提交；覆盖持续就绪、真实下线再上线、试运行、暂停/恢复/编辑及决定前后硬重启。外部通知证据见后续切片。
- [x] 3.7 实现任务暂停/编辑/终止与迟到判断、在途动作的 revision 协调，不隐式取消已接受执行，检查试运行不执行后续工作动作。
  - [x] 3.7.1 always/script检查不持管理锁，返回后重验revision；试运行四类决定、失败与重启后的结果未知均不改变正式生命周期、不投递动作。模型判断及应用迁移仍待实施。
  - [x] 3.7.2 model判断已接受后保留原执行与迟到结果，暂停/编辑抑制动作；暂停任务试运行实际调用判断模型，保持正式revision/生命周期。Goal/生成器应用迁移仍待实施，3.7不代替其验收。
- [ ] 3.8 接入通用运行时的受管理恢复 worker：start 仅等待存储、订阅、调度权与 worker 初始化回执，逐项恢复和条件执行在后台有界推进；保留任务 recovering 阶段、新触发持久排队及补跑策略，对账判断/投递/工作执行，不盲目重放未知脚本结果。
- [x] 3.9 实现统一始终通过判断组件，无脚本或额外模型调用；每个有效定时触发都可执行，添加附加条件只更新同一任务定义，非法或缺失判断配置不隐式通过。
  - [x] 3.9.1 interval/cron复用显式always判断与原source动作，多次有效计划均执行，无额外检查模型/脚本；不以此替代附加条件和前端统一编辑验收。
- [x] 3.10 将 worker 初始化失败、运行期异常与停止收敛接入工作区生命周期及健康状态；保存句柄和恢复进度，禁止丢弃后台异常或虚报就绪后的任务恢复成功。

## 4. 将 Goal 迁入统一运行时

- [x] 4.1 将目标提示、预算记账及完成/阻塞审计构建为 Goal 应用组件，保持 main-thread 限定和既有状态语义。
- [x] 4.2 平移激活、目标更新、启动恢复、Agent/Job 事件处理并验证去重；Goal 工具和 API 直接操作统一任务 owner。
- [x] 4.3 实现可重试的一次性 goal.json 导入，保留稳定身份、预算、revision 和已续跑标记；迁齐fork冻结Goal、preparing节点安装、发布/取消/恢复及旧文件校验消费者，独立fork Goal保持新身份和零用量，child不获得Goal；迁齐消费者后物理移除旧 Goal 状态存储与专用调度实现。
- [x] 4.4 物理删除 GoalRuntimeService 及其文件、import、容器字段与装配、独立事件订阅/注销和 close-admission/shutdown 步骤，删除 workspace_lifecycle 中 goal_runtime_service.resume_active_goals 启动 hook；由通用运行时统一接管，禁止保留转发包装层或改名后的独立 Goal 调度器。

## 5. 将定时生成器迁入统一运行时

- [ ] 5.1 将现有生成器迁为定时触发、统一无附加条件判断和会话动作的预设组合，不保留独立定时任务模型或运行分支；保留三种会话策略、回报及 UI 激活行为，复用当前 catalog 定位与导航预览。
  - [x] 5.1.1 目标工作区统一计算命名与 catalog 导航预览，实际生成消费同一渲染结果；未创建时明确物理定位尚未分配，继续会话显示现有导航。迁齐前端/协调器/协议和测试后删除 Gateway 旧预览接口与物理 children/folder 路径模板；生成器定义、调度与账本整体迁移仍未完成。
- [x] 5.2 将定义校验、运行协调、执行引用和终态归一到工作区任务 owner，保留 GenerationRun 身份及既有幂等，不复制第二份可变账本。
  - [x] 5.2.1 保存前的 provider/版本/config、工作区关系、catalog 节点与 Thread 归属校验由目标工作区唯一执行，执行复用同一 provider 解析；Gateway 仅逐个解析路由身份并传递结果，删除旧 schema 校验与目录探测。此子项不代表运行账本与调度 owner 已迁移。
- [x] 5.3 实现 Gateway 旧定义、计划游标及未完成运行的一次性迁移，按原执行工作区解析；离线/缺失目标显式报告并禁止改绑默认工作区。
- [x] 5.4 将前端、API、工具和配置消费者全量迁移到工作区入口，移除 Gateway 旧生成器调度、业务存储、监控、接口和无用配置，不保留兼容转发或生产双读。
- [ ] 5.5 核验迁移重试、核验后切换和备份回退流程，证明同一任务没有同时运行的新旧 producer。

## 6. 前端持久任务管理

- [x] 6.1 在主窗口底部面板“自动化”实现当前工作区任务列表及详情，展示生命周期、当前阶段、等待原因、目标、最近结果和下次触发。
- [x] 6.2 抽出共用列表、详情、组件配置表单和运行记录；提供五类快捷模板与纵向高级编辑，统一称为持久任务；定时执行隐藏始终通过配置，只需时间与动作，可在同一表单开启脚本/模型附加条件，共用完整任务定义并明确显示未知类型/版本错误。
- [ ] 6.3 实现暂停/恢复/编辑/终止/立即检查及试运行，成功用后端完整对象替换镜像，失败重取并显示具体错误。
  - [x] 6.3.1 当前v1普通任务通过统一组件表单创建/编辑、管理和检查，SSE与写入回执均为完整对象；真实浏览器覆盖试运行无投递、仅保存动作及另一客户端造成的真实409后完整回读。Goal联动、模板及会话生成动作仍由其对应整项追踪。
- [ ] 6.4 展示触发到判断、投递与工作执行的因果记录，提供判断/工作 thread 跳转，区分仅保存、已接受、已消费和结果未知。
- [ ] 6.5 将右侧独立 Goal 标签接到同一任务 owner，展示当前会话关联任务摘要并跳转完整管理；工作区/会话切换清除旧展示，不引入 Gateway 任务入口。
- [ ] 6.6 展示任务后台恢复的阶段、进度与错误，区分工作区就绪和任务恢复完成，验证恢复期间暂停任务不会被迟到恢复重新激活。
  - [x] 6.6.1 列表展示原owner的recovering阶段、剩余恢复任务数及读取错误；定向hook核对恢复期间暂停回执和迟到快照不能重新激活，工作区切换撤销旧订阅、请求及动作闭包。完整慢恢复/worker失败的Web联动仍待验收。

## 7. 行为验证与最终清理

- [x] 7.1 使用隔离工作区验证脚本未就绪、首次就绪、连续命中、再次上线、脚本超时、外部重复通知与冲突；通过真实任务入口证明不会误调用工作模型。
- [x] 7.2 验证模型判断的四类决定、无效输出/失败、明确输入关联、判断重入、自触发及过期 revision 结果，覆盖现有执行准入而非仅测试 helper。
- [x] 7.3 验证仅注入、忙时排队、任务暂停与已接受执行、投递中断恢复，核对真实序列化、source/wakeup 引用及内部执行不产生伪用户 Turn。
- [ ] 7.4 验证 Goal 既有状态/预算/激活与恢复入口、定时三种会话策略/时区/补跑/overlap/回报及迁移重复运行和离线归属，补充受影响定向回归。
  - [x] 7.4.1 产品入口验证 Goal 续跑接受前，以及执行完成后标记保存前/后的 Backend 硬重启；沿原准入/canonical 用量恢复，保留原身份、精确记账且不重复执行。完整在途模型/工具、目标编辑与旧执行及 fork 发布窗口的硬重启仍待完成。
  - [x] 7.4.2 产品入口验证在途模型硬重启后的 execution_lost、暂停不重启及新目标独立执行；受支持 read_context 工具结果提交前硬杀后沿原 checkpoint 恢复，核对原 source/job/execution/model_call/assembly/request_hash、工具配对、精确用量、零伪用户 Turn 与再次重启无重复。无 Goal 准入根的历史用户执行、fork 发布窗口硬杀及生成器迁移仍由整项追踪。
- [ ] 7.5 完成后端相关静态分析、真实前端类型检查与 `bun run --cwd src/clients/web build`；全部测试采用进程外超时与内存保护，保留源码指纹、命令、原始输出和实际退出码。
- [ ] 7.6 在隔离工作区通过真实 Gateway 和浏览器验证创建/编辑/检查/暂停/恢复、运行记录、Goal 联动及工作区/会话切换；当前开发环境以实际 8111 启动配置为准，核对初始化 API 和 request_id，产物遵守 out/tests 路径规范。
- [ ] 7.7 交付前全仓检索旧接口与 producer，检查悬空调用、双轨、第二事实源、隐式回退和无关改动；如实列明通过、失败及未验证范围。
- [x] 7.8 验证慢 Goal/条件判断恢复不阻塞工作区就绪、同任务恢复前新触发只排队、不同已恢复任务可推进，以及 worker 初始化/运行期失败均明确报告；断言没有独立 Goal 恢复入口。
- [x] 7.9 验证普通定时任务对多个计划点均执行且无额外脚本/判断模型调用，同任务开启附加条件后保留身份、游标与历史；真实前端验证默认隐藏判断配置和统一编辑流程。

## 实施证据

### 快捷模板与同一任务附加条件（2026-10-27）

- 新建入口提供Goal、定时执行、脚本条件、模型判断、自定义；Goal跳转现有会话主Thread专用视图，其余模板直接预填同一完整组件DTO。普通任务不展示始终通过选择器，显式开关启用附加条件；高级表单保留已有字段，未知组件继续明确拒绝编辑。
- `template-unit-039`：8 passed，覆盖原高级值/null保留、规则重新选择、普通任务隐藏判断器、增加模型条件不改变时间/动作/策略，以及旧工作区闭包和迟到快照隔离。`template-web-043`：真实Gateway/Backend/Chromium 1 passed（41.45秒），核实五类入口、Goal原视图、真实保存always、同任务加条件后ID/计划不变、试运行/仅保存、真实409回读、管理及刷新；没有调用模型。
- `template-types-041`真实类型检查及`template-build-042`构建通过。首次类型检查038发现自定义触发缺少协议必填timezone，已补齐UTC后复验；不把仅Vite构建成功当作类型通过。后端多计划点、零额外判断调用及开关条件后保留身份/历史复用既有`task-schedule-product-069`及其产品场景的证据，本切片未改后端。
- 准确命令、源码指纹、原始输出和退出码保存在`out/tests/temp/task-generation/artifacts/runs/template-*`；正式浏览器产物位于`out/tests/integration/clients/web/persistent_tasks/test_management/runs/`。运行消费状态、当前会话关联任务摘要、完整慢恢复/失败Web联动仍待后续任务完成。

### 会话生成与迁移统一到任务 owner（2026-10-27）

- 会话动作复用同一触发、判断与 worker；命名、准备消息和执行身份写入原运行，canonical 接受后移除准备正文。等待执行及回报归还 worker 槽位，继续指定 Thread 保留串行排队和任务间推进。删除 Gateway 的定义 store、scheduler、coordinator、健康依赖、旧接口与轮询配置，以及工作区旧 JSON 运行和消息 intent writer；普通运行不双读。主窗口自动化与全部测试调用方迁到持久任务，清理旧组件、hook 和样式。
- Workspace SQL 22实际增加原运行内会话动作状态及一次性导入回执；Gateway配置删除旧轮询字段并升至4，显式配置迁移保留原备份。管理导入先冻结原字节、目标稳定身份和指纹，目标单事务安装原定义/计划/运行，再凭回执退休源文件；离线或改绑不回退默认工作区，重试不覆盖导入后的编辑。
- `task-generation/artifacts/runs/generation-runtime-007`历史切片3 passed、1 deselected，不代替当前新增场景的整组验收。`generator-product-018`：6 passed、1 deselected，覆盖真实Gateway导入及命名/新建/继续会话。`migration-unit-019`：86 passed；`gateway-caller-023`：79 passed；这些范围有重叠，不累计为独立场景数。`inflight-import-027`另经真实消息与模型入口生成在途状态，证明导入沿用原message/job/execution、一次模型调用、暂停不取消已接受执行，旧格式仅作迁移输入。
- 最终`generation-final-036`整组7 passed（189.13秒），覆盖普通任务身份保留、会话创建/接受/回报接受窗口硬重启、终态与真实在途旧运行导入。`generated-history-web-035`真实Chromium长历史验收通过，保留130工具调用、刷新恢复与内部生成来源断言；前置初始化失败未计为业务通过。
- `ruff-delta-final-037`核对相关61个Python文件，HEAD基线61项、当前44项、新增0项；不宣称全仓Ruff通过。`static-final-034`定向Pyright零错误，`web-final-types-024`真实类型检查及`web-final-build-025`构建通过。`web-management-026`真实Chromium/Gateway/Backend验收1 passed，覆盖统一表单的生成导航预览、试运行、编辑、真实409后完整回读及管理/刷新；未据此关闭全部模板、会话切换与恢复UI验收。
- TODO：正式`integration-task-generation-strategies`本轮5 passed/1 failed；fork新会话、来源与执行已经通过，回报在原40×0.1秒窗口结束时仍为queued/reporting。保留原期限及性能TODO，不据局部成功勾5.1/7.4。备份回退及其余完整迁移验收仍由原任务追踪。命令、源码指纹、raw和实际退出码保存在`out/tests/temp/task-generation/artifacts/runs/`；不将前置失败或中断的委派专项记为通过。

### 脚本检查的共用进程保护边界（2026-10-27）

- Windows Job Object实现从测试支持目录移入 `app/runtime/windows_job.py`，matrix、Windows开发目标及安装测试全量迁移，旧文件物理删除。补齐标准流传递与可选执行期限；POSIX独立监督器提供每进程数据段限额、执行期限和退出后的进程组回收。两平台的内存限制语义不同，未宣称POSIX具有Windows整树提交内存限额。
- `native-guard-linux-072`：9 passed；`native-guard-target-contract-073`：39 passed、1 Windows专用skip；POSIX定向Pyright零错误，相关Ruff通过。真实标准流/cwd、非零退出、内存超限、超时/中断及根命令退出后的后代回收均由原生子进程核实。
- `native-guard-windows-074`在测试启动前发现pywin32标准流常量的模块归属错误；经目标原生库查询修正后，快照 `e08dfade0eb2` 在 `hyf_win` 通过16项、跳过1项POSIX信号场景（075），覆盖Job绑定/通知失败、内存、期限、后代回收、matrix watchdog及两种安装入口。原始命令、源码指纹、输出和退出码保存在既有证据目录；未把首次启动失败记为测试通过。
- 这一步只完成脚本判断的进程保护基础；后续脚本纵向切片证据见下文，3.2仍保留未完成的规则等范围。

### 1.1—1.3 调用闭包与行为映射（2026-10-27）

- design补齐Goal、Gateway生成器、工作区SessionGeneration及其工具/API/前端/协议/配置/恢复消费者，特别登记工作区运行账本、Session内消息intent与fork preparing节点goal.json检查。不是只移动cron或替换GoalRuntimeService名称；团队协作保持TODO。
- `inventory-002`在HEAD `1b228309d`及当前工作区通过三组全仓rg取得Goal 60、生成73、source/lease 99个命中文件（集合重叠，不累加），记录准确命令、退出码与逐文件SHA-256，保存在 `out/tests/temp/persistent-task-migration/artifacts/`。第一轮较窄检索原件保留，第二轮补齐间接前端函数/组件引用。权威proto及其全部生成资产列入迁移，未将检索命中数当作测试通过数。
- 明确复用WorkspaceStateStore、原source mutation owner、source+wakeup事务、thread单事务队列和外部资源owner；保留Goal六状态/逐Job记账、三种会话策略、六种回报、时间/补跑/overlap/UI与旧数据归属，新增独立fork Goal的既有合同场景。当前3/41，只完成盘点与规划闭包，新任务模型、存储、通用worker和迁移尚未实现，产品测试未运行。

### 手动触发至 source 动作切片（2026-10-27）

- 在原 WorkspaceStateStore 增加 SQL 16 的任务、不可变定义和运行表；任务 row revision 与定义 revision 分离，创建/检查幂等、同键异内容冲突、管理 CAS、冻结定义引用均走同一事务链路。组件仅开放 manual/always/literal/runtime_source v1，缺失判断或未知版本保存即拒绝。
- 工作区 API 提供完整对象管理、试运行、历史和 SSE，后台受管理 worker 初始化与逐任务恢复分离，有界推进、不持锁等待模型；目标校验与恢复沿原 Gateway、Session 和明确 Thread，保存并传递原请求 request_id。恢复先向原 source/wakeup owner 查询已接受事实，之后再判断新准入 revision，暂停/编辑不抹掉原工作、不取消已接受 Job。
- `manual-task-product-030` 的8场景及 `manual-task-product-039` 通过的新增慢恢复场景，加上 `manual-task-final-cases-046` 的4场景，合计12个不同产品场景已分别通过；039中的健康错误断言曾错误期望 ASGITransport 抛异常，按既有 TraceMiddleware 的HTTP500合同修正并补验。并非一轮整套12 passed。覆盖两种投递、忙时队列、零伪用户Turn、真实HTTP SSE、双挂载隔离，以及动作接受后暂停/编辑并硬杀后端的原身份恢复。
- `manual-task-regressions-042`：65 passed；`manual-task-contracts-051`：26 passed。SSE描述查询改用官方模块DESCRIPTOR，静态检查0错误（056），定向SSE复验2 passed（057）。Buf lint/build/breaking及全部绑定生成通过（050）；任务定向Pyright（043）、Ruff（044/052）、真实Web类型（053）及构建（054）通过，构建仍有既有chunk体积提示。
- 全部证据保留在 `out/tests/temp/persistent-task-migration/artifacts/<运行名>/`，记录命令、HEAD、文件指纹、原始输出和实际退出码。扩展Pyright仍有已核对的既有容器/资源类型和assembly object错误，未宣称全仓类型全绿。
- 2.1、3.1、3.3、3.6—3.9等大项仅完成手动无附加条件切片；interval/cron、规则/脚本/模型判断、状态来源更新、Goal/生成器迁移及任务UI尚未完成。原Goal/生成器生产者仍只管理原业务，不消费新手动任务；后续必须沿已登记闭包迁齐并物理删除旧owner，不建立兼容转发。

### interval/cron与增量恢复切片（2026-10-27）

- 时间计算收敛到 `core/time_schedule.py`，Gateway旧生成器删除原算法并直接消费共享计算；保留时区、100计划点分批及skip/run_latest/catch_up策略，补齐大积压时恰好到期的cron点。工作区任务只通过现有通用worker推进，不创建定时专用执行器。
- SQL 17增加计划游标和运行接受序号，迁移原v16手动历史并保留冻结定义、request_id及创建幂等身份。时间触发和游标在同一事务提交；改名/非时间配置编辑保留原游标，修改时间触发器才建立新锚点。恢复期间新计划持久排队，任务按接受序号处理；暂停不重锚计划，恢复按原补跑策略推进。
- 初轮062实际4 passed/2 failed：补跑批次相同时间戳下按随机ID排序，导致顺序错误；已改为持久接受序号，并用显式倒序ID验证正确实现。另一失败来自真实Backend测试使用冻结墙钟却未推进测试时钟，修正控制输入后继续实际定时及硬重启验收，未放宽身份/顺序断言。
- 最终 `task-schedule-product-069`：7 passed，92.68秒，覆盖多次真实内部模型执行、三个补跑策略、时区与暂停恢复、慢恢复期间新计划排队后暂停抑制，以及已提交计划在真实进程硬重启后复用run/trigger/request_id。`task-schedule-unit-067`：34 passed，含Gateway原调度回归、夏令时、v16迁移、游标写入失败的整事务回滚和倒序ID接受顺序。
- 手动完整matrix在066通过12项；接受序号改动后补跑受影响的真实试运行、忙时队列和两种接受后硬重启场景，070通过4项（142.79秒）。后端定向Pyright068零错误；Ruff通过；协议合同065通过26项，Buf与全部绑定058生成通过，OpenAPI059已更新；真实Web类型063及构建064通过，仅既有chunk提示。后续接受序号变更未影响公开DTO、前端或协议资产，上述结果按源码依赖复用。
- 证据仍保存在 `out/tests/temp/persistent-task-migration/artifacts/<运行名>/`，所有最终测试均使用进程外期限/4GiB保护且无源码漂移。计划与手动产品资产位于各自正式 `out/tests/integration/backend/sessions/test_persistent_task_schedules/` 和 `test_persistent_tasks/`。本切片未实现规则/脚本/模型条件、Goal/生成器owner迁移和主Web任务管理，相关大项不勾选。

### script v1判断与恢复切片（2026-10-27）

- 复用唯一平台监督器，命令argv无隐式shell、cwd固定所属工作区，stdin/stdout使用UTF-8。stdout/stderr共用总量上限，超限终止后继续排空管道；期限由进程外监督器执行。输入只引用原冻结定义及触发，不复制prepared message。四类决定与结构化result沿原运行事务保存，并以不可信正文交给原source动作；原生诊断只进具名原始日志入口。
- 检查先持久checking再释放管理锁，暂停/编辑能及时完成，迟到结果保留但不投递。checking中断显示结果未知、不重跑脚本；decided和已接受动作恢复原结果、request_id、定义及原canonical item。试运行的有效决定、失败和中断均不修改正式任务生命周期。
- 最终完整matrix `task-script-final-matrix-091`为8 passed、147.85秒。审查随后修复失败试运行停用active任务的问题，093定向失败场景通过；094通过真实和试运行两种checking硬重启（2 passed、58.35秒）。合计9个不同场景按依赖分别验证，未声称最终9项在同一轮完整运行。手动085为12 passed、198.58秒；时间086为7 passed、95.67秒。082协议/存储及入口校验29 passed，090补验script缺配置拒绝。095 Ruff通过，096定向Pyright零错误；真实Web类型081、构建083、Buf/全部绑定076及OpenAPI077完成，仅既有chunk提示。
- 初轮078的正文匹配断言忽略了实际XML/JSON封装，改为解析并严格比对完整result；084的测试在Backend停止后未释放故障屏障，修复测试协调后088通过原身份恢复。080命令引用不存在路径、无测试运行，纠正后082通过。上述失败原件均保留，不作通过统计。
- Windows原生监督器075的16项通过仍有效。产品087暴露共享fixture的GBK读UTF-8，089暴露重复拼接产物路径；修正编码及重复路径后，092实际进入第三次proceed，但旧rollout锁owner在深层Thread目录创建时触发`WinError 206`（`storage/primitives.py::_open_handle`），继而关闭失败并占用catalog，最终1 failed/5 errors/3 deselected。TODO：单独处理Windows原生长路径边界后补完整脚本产品套件；本切片不更改目标系统设置、不将该运行记为Windows产品通过。
- 所有证据仍在既有临时证据目录，包含准确命令、源码指纹、原始输出和退出码；正式脚本产物在`out/tests/integration/backend/sessions/test_persistent_task_scripts/`。数据库结构未变，仍schema17。first_match/rising_edge、模型条件、Goal/生成器迁移及主Web验收继续保持未完成。

### 条件策略与原运行事实复用（2026-10-27）

- `policies.match_mode`显式支持first_match/rising_edge/each_match，默认each_match保留原手动与定时行为，默认字段不改变旧创建幂等身份。首投只查原接受回执，边沿只查按接受序号排列的有效wait/proceed；不增加第二份可变匹配状态，也不改数据库结构。决定、策略选择、阶段和适用生命周期在原SQLite事务提交，移除编排层原局部结算逻辑。
- `task-match-product-102`完整matrix为5 passed、177.74秒。通过真实任务API、子进程脚本及model_stream严格核对三种策略的实际模型调用次数、连续就绪、下线再上线、重复同键、试运行、暂停/恢复/改名和两种Backend硬重启窗口。接受后重启复用原canonical item，决定后重启复用原决定；后续真实请求只含应投递的事件。
- `task-match-contracts-099`为30 passed，含v16旧历史/幂等身份、真实协议/SSE及任务入口，并注入生命周期写入失败证明决定和任务更新一并回滚。107定时完整回归7 passed、95.54秒；106脚本完整回归9 passed、179.93秒，覆盖上个切片审查修复后的完整9项。101后端Pyright零错误，105 Ruff通过；103真实Web类型、104构建、097 Buf/全绑定和098 OpenAPI完成，仅既有chunk提示。100首次Ruff仅有新增测试import排版，格式修正后105通过，未改产品源码或产品断言。
- 全部产品运行均有进程外超时/4GiB保护且无源码漂移；命令、指纹、原始输出和退出码仍保存于既有证据目录，正式条件策略产物在`out/tests/integration/backend/sessions/test_persistent_task_matches/`。尚未实现外部通知、模型条件、Goal/生成器迁移及任务Web界面，3.6与整体验收仍不勾选；Windows产品范围沿用前述长路径TODO。

### 外部通知与冻结触发输入（2026-10-27）

- `builtin.external`沿原工作区认证路由接受幂等键和JSON对象，触发、定义revision、原request_id及payload一次持久化，既有check/worker/source链路统一消费。调用方不必追踪管理revision；已接受通知在编辑/暂停后仍返回原完整运行，新通知则按当前生命周期拒绝。脚本读取冻结payload；always和script动作均通过原source owner发布不可信输入，不建外部通知专用执行器。
- SQL 18实际扩展原运行表的触发类型与不可变payload，约束external才有payload、时间触发才有计划点。v16/v17逐次升级保留运行ID、原请求、定义、创建幂等身份和接受序号，物理删除旧表，不双读。`task-notify-contracts-110`为34 passed，包含旧库、原MCP操作、协议/SSE和离开源码树的发行资源加载；Gateway自身schema17不随Workspace schema18变更。
- `task-notify-product-111`完整matrix为3 passed、126.06秒。实际通知驱动原生脚本及model_stream，等待/持续命中不误调工作模型；8个并发同键请求只接受一次，不同内容冲突；编辑/暂停后原通知仍复用原结果，恢复后新边沿沿新定义执行。两个Backend硬重启窗口保持原payload、request_id、定义及原canonical item，后续真实请求严格比对原输入。
- 117补充回归有3 passed/1 setup error：并行运行同一script测试文件的两个pytest进程，共用正式文件级workspace，一个准备fixture删除目录时另一个正在打开SQLite；不是运行期任务失败。两进程结束后仅补跑该失败场景，119为1 passed、24.48秒；118的原script决定/接受后冷恢复2 passed、88.81秒。没有修改产品或放宽断言，保留117失败原件。
- 112 Ruff通过；116新增业务模块Pyright零错误。113扩展到API全文件时发现原有`sse_responses`返回`dict[int,...]`与FastAPI的`dict[int | str,...]`不变性错误，已对照HEAD `a95db93f6`确认原`/events`注解相同，未声称API全文件类型全绿。114真实Web类型、115构建、108 Buf/全部绑定和109 OpenAPI通过，仅既有chunk提示。
- 命令、源码指纹、原始输出及退出码仍在既有证据目录，产品产物在`out/tests/integration/backend/sessions/test_persistent_task_notifications/`，测试均有进程外期限/4GiB保护。Linux本切片验收通过；Windows产品长路径、模型判断、规则判断、工作执行结束触发、Goal/生成器迁移及主Web管理仍未完成，团队协作继续TODO。

### rule v1与统一条件正文（2026-10-27）

- 规则使用官方jsonpointer解析冻结检查输入，必填expected按RFC8785比较；字段缺失与null、bool与number保持区别，对象键顺序不影响相等，数组顺序保留。非法pointer与缺失expected保存即拒绝。判断结果复用原决定/条件策略事务，所有非always动作复用同一不可信正文链路；不增加数据库结构，仍schema18。
- `task-rule-product-123`完整matrix为3 passed、109.01秒，真实任务API及model_stream验证嵌套/转义pointer、结构相等、首次命中策略、暂停试运行不消费首次命中，以及checking中硬重启后仍使用原payload/request_id。后续通知排在原检查之后，不读取新输入替代旧输入，也不伪报未知脚本结果。
- `task-rule-contracts-128`为20 passed，覆盖v16/v17升级、原创建幂等、决定/生命周期原子回滚、协议/SSE与手动真实入口；`task-rule-script-regression-129`为3 passed、111.29秒，核对未就绪到就绪、决定后及接受后硬重启，仍使用原脚本结果与canonical item。所有产品运行有进程外期限/4GiB保护且无源码漂移。
- 120 Buf/全绑定、122 OpenAPI、124定向后端Pyright、125真实Web类型、126构建和127 Ruff均通过，仅既有构建chunk提示。121在完整源码快照验证直接依赖锁，根目录`uv sync --frozen --offline`成功，无传递包版本变化；根目录setuptools扫描递归node_modules的既有问题未扩展修复。临时完整源码快照及协议staging验证后删除，保留准确命令、指纹、原始输出、退出码和正式产品产物。
- 模型判断、工作执行结束触发、状态来源更新、Goal/生成器迁移及主Web管理仍未完成；Windows完整产品验收沿用已有长路径TODO，团队协作继续TODO。3.2勾选只对应已实现且经Linux产品验证的规则/脚本组件，不代表上述后续范围已完成。

### 执行结束触发与原终态引用（2026-10-27）

- `builtin.execution_end`绑定同工作区、当前Gateway的明确Thread URI。Job事件只唤醒原worker，从canonical已提交终态增量对账；引用原execution/job/request_id，不复制执行正文或维护第二份事件账本。新建/换源从当前明确起点订阅，改名保留游标，暂停后恢复跳过未接受的暂停期间结束事件。来源删除显示needs_attention，历史回退或缺失明确失败。
- Workspace SQL19扩展原运行表并新增来源订阅游标；原v18历史、接受序号、冻结定义与幂等身份保留，旧表物理删除。运行引用与来源游标同一事务提交。134合同/迁移/发行资源验证25 passed，覆盖v16/v17/v18升级及接受失败后的事务回滚；Gateway仍schema17。
- 初轮133为2 passed/2 failed：取消先出现execution_lost再修正cancelled，读取改为接受前快照中的最新终态；不存在来源测试原来使用格式非法ID，改用格式合法但不存在的SessionID，仍严格断言404。141修复后完整4场景通过。随后分页审查发现按修正位置推进会跳过其他执行，改为按首次结束位置排序/推进、另存精确终态引用。144为7 passed，通过真实owner创建A/B两次执行并依次收敛lost(A)、cancelled(B)、cancelled修正(A)，limit=1仍严格返回A、B且不重复；来源游标倒退也明确拒绝。
- 最终`task-events-final-order-product-145`完整matrix为4 passed、184.93秒，覆盖真实用户与内部执行、取消、重复事件、动作结束不误触发来源、暂停/改名/换源，以及canonical已完成但任务未接受时硬杀Backend、两次重启后复用原运行。138定向手动/忙时/冷恢复/慢恢复/worker失败7 passed，139定时完整回归7 passed；全部有进程外期限/4GiB保护且无源码漂移。
- 130 Buf/全部绑定、131 OpenAPI、135真实Web类型、136构建、147最终Ruff均通过；146任务业务模块Pyright零错误。140/142扩展检查原执行mixin时分别有82/17个诊断，补齐新读取端口真实storage类型后新增方法无诊断，剩余为未修改方法的既有索引/content/兄弟方法类型问题，未扩大修理或声称这些文件全部类型通过。构建仍仅既有chunk提示。
- 准确命令、源码指纹、原始输出与实际退出码在`out/tests/temp/persistent-task-migration/artifacts/`，正式产品产物在`out/tests/integration/backend/sessions/test_persistent_task_execution_events/`。一次读取最多100项，长历史查询耗时尚未测量，不宣称扫描成本有界。模型判断隔离、状态来源更新、Goal/生成器迁移和主Web管理仍未完成，Windows产品长路径及团队协作继续TODO。

### model v1独立判断与精确结果恢复（2026-10-27）

- 明确绑定判断Thread URI与自然语言条件，沿原source/wakeup/执行队列接受，使用判断Thread自己的Agent/工具配置；保存时拒绝与动作或结束事件来源相同的Thread。输入复用规则/脚本的冻结构造器，运行只持原source item/job/execution引用。Workspace SQL20只增加evaluation引用字段，v16—v19旧运行保留身份、接受顺序及幂等preimage；没有第二份模型请求或消息正文。
- 结果只读取该execution最后一次ModelCall的canonical完成输出，忽略工具循环中间说明及其他执行历史；JSON严格校验本次run_id、definition_revision、四类决定和非空理由。未知执行结果保留checking，释放worker槽并等待原终态通知；新触发按原接受顺序排队。暂停/编辑后对账原准入，保留迟到结果但不接受动作；同一运行时stop/start清除内存等待标记后从原事实恢复。
- 152首轮被主动中断，实际1 passed、2 failed、4 errors、exit2：空模型回复触发原执行器既有重试，但测试缺少后续响应资产；读文件调用误用file_path，且失败后未释放后续检查屏障导致关闭/后续setup失败。将无效输出用例改为明确的空reason（仍严格验证拒绝及固定调用次数），读文件改用真实path协议并在finally释放屏障。158完整复验为5 passed、1 failed、281.76秒；唯一失败的真实工具结果明确要求Gateway身份，原无Gateway fixture不符合文件工具入口合同，没有修改产品绕过该要求。
- 162使用既有managed_gateway fixture补验忙时/工具循环场景，1 passed、61.35秒。实际读取工作区文件、只解析最后回复、同任务两次检查串行及同实例stop/start均通过。连同158其余5项，共6个不同产品场景分别通过，未声称最终6项同轮完整matrix全绿；覆盖四类决定、非法/错误身份/错误revision/空理由、执行取消不伤其他任务、迟到管理/暂停试运行，以及判断已完成但任务未登记回执时硬杀Backend、再次重启后仍一判断/一工作执行。
- 155合同/迁移/发行SQL资源为27 passed，含不可变evaluation身份冲突和v16—v19升级；153因命令误写不存在测试路径而未运行测试（exit4），保留原件后改用已有合同文件。159规则完整回归3 passed、103.39秒；161脚本试运行/失败回归2 passed、18.84秒。最终产品运行均有进程外期限/4GiB保护且无源码漂移。
- 148 Buf/全部绑定、149 OpenAPI、154真实Web类型、156构建、163最终Ruff及160最终任务相关Pyright均通过，仅既有chunk/依赖弃用提示。owner仍有前述旧mixin类型限制，未声称全仓类型全绿。准确命令、HEAD/文件指纹、原始输出和实际退出码沿用现有证据目录；正式模型产物在`out/tests/integration/backend/sessions/test_persistent_task_models/`。状态来源更新、Goal/生成器迁移、主Web管理及Windows完整产品验收仍未完成，团队协作继续TODO。


### 同一来源状态发布与历史恢复（2026-10-27）

- `context.mode`显式选择event/state，缺省event保留原创建幂等身份。state正文只取冻结字面内容、触发payload与判断result，运行身份及理由留在审计；原canonical owner保存每次发布，原控制事务分配来源版本，同值复用当前版本，A→B→A取得新版本。状态、短引用通知、执行准入、队列及wakeup同事务接受；旧请求重试不回退后继状态。冷prepare/rebuild与fork只读取冻结canonical正文，不另存任务上下文历史。原source构造器直接迁为同模块纯函数并迁齐全部调用，无旧方法或转发层。
- `task-state-final-product-189`完整matrix **5 passed／251.55秒**，覆盖无假Turn/无重复注入、两种动作接受后硬杀Backend并编辑/暂停、事务中断回滚后原键重试，以及来源后继状态已发布时从旧历史点fork、目标首次prepare重建原版本。`task-state-manual-regression-180` **13 passed／222.21秒**，包含event/state忙时串行、暂停保留已接受工作及挂载隔离；`task-state-event-regression-187` **5 passed／129.05秒**，复验原事件发布、忙时工具循环、冷owner结算与fork。所有产品批次均有进程外期限/4GiB保护且无源码漂移，继续复用model_stream。
- 初轮168暴露内容hash不能充当状态生命周期版本，173暴露来源revision格式不符合原合同；修正分配算法，未放宽domain冲突检查。178为3 passed/2 failed，分别是fork漏启真实辅助服务及断言混入其他来源；183的fork保护要求正式runner而未进入业务，原子重试已通过；186正式runner的fork通过，最终189又加严并覆盖全部5项。185为29 passed/2 failed：旧中断测试的Gateway Skill fixture误将快照作为发布通知，另有configured owner未就绪，尚未进入中断裁决；该旧测试消费者仍待迁齐，不把它报为通过。
- 166 Buf/全部绑定、167 OpenAPI、175合同/旧库/发行SQL/协议 **26 passed**；170真实Web类型及171构建按未变生成资产复用。188/191 Ruff、190相关业务模块Pyright与OpenSpec strict验证通过。184扩大到存储mixin仍exit1、53项诊断，未声称存储全文件类型全绿。准确命令、指纹、原始输出和退出码保留在`out/tests/temp/persistent-task-migration/artifacts/`，正式产物在对应`out/tests/integration/backend/sessions/test_persistent_task_states/`；临时协议staging已清理。当前完成3.3/3.5，Goal与生成器迁移、主Web管理、Windows既有长路径范围仍未完成，团队/协作继续TODO。
- 185暴露的旧CSM测试消费者已迁齐：非lifespan夹具走真实配置prepare/activate，Gateway协议桩按精确路由区分快照与发布等待，关闭时先排空source观察。192 Ruff通过；193中断裁决 **2 passed／37.09秒**；194真实Runner动态Skill发现、覆盖、降级与tracked来源不改绑 **1 passed／80.76秒**。均有进程外期限/4GiB保护、无源码漂移；185失败原件仍保留，不以修正后的结果覆盖。

### Goal应用规则（2026-10-27）

- 目标校验、未完成状态、active预算收敛、按Job单调增量记账及三类内部提示集中到`business/persistent_tasks/goal.py`；原Service及Runtime直接消费唯一实现，移除旧私有构造器和字符串转发方法。提示正文AST与原版一致，目标仍通过既有提示工厂转义，主线程归属及六种状态不变。该组件不持有存储、事件订阅或后台任务。
- 195为 **31 passed／8.53秒**，覆盖工具、归属、替换、预算、重复/乱序记账、冻结用时、续跑去重、关闭及后台失败；迟到记账保留状态扩为全部五种非active状态。199经ASGI API/SDK/model_stream产品入口 **1 passed／81.62秒**，从Goal激活到模型工具完成，保留内部执行、无伪用户Turn及后续终端/中断来源；该测试不证明后端真实网络传输。所有测试有进程外期限/4GiB保护且无源码漂移。
- 200 Ruff、201应用模块Pyright通过。197误用未安装的`uv run pyright`未执行分析，198改用已有离线uvx后发现原Service的object哨兵两项类型错误、原Runtime delivery_policy缺少Literal注解一项；均对照HEAD未修改声明确认，不将扩大检查报为全绿。准确命令、指纹、原始输出和退出码仍在既有证据目录。
- 本切片完成4.1；统一owner、goal.json导入、fork preparing安装/发布/取消/恢复及GoalRuntimeService删除仍由4.2—4.4追踪，没有将纯规则迁移表述为调度迁移完成。

### 定时任务开启与关闭附加条件（2026-10-27）

- 原always组件、规则/脚本/模型组件共用定义、判断及source动作；本次补齐从普通定时执行开启脚本条件再关闭的产品验收。通过API创建唯一任务并形成五个真实计划触发：always、always、脚本wait、脚本proceed、always。四次工作模型调用、两次脚本检查严格对应原计划点，检查输入的task_id/revision、全部旧运行及游标保持不变，仍只有内部execution且无用户Turn。
- 202为1 passed/1 failed：缺失/非法组件及真实试运行通过，新用例在第三次工作完成后因整体JSON转义的字符串匹配失败；改为复用现有提示解析器，精确比对完整condition_decision。204新场景 **1 passed／64.69秒**，205 Ruff通过；所有运行有进程外期限/4GiB保护且无源码漂移，失败原件保留。时间计划/补跑/恢复的原139完整7项证据沿未修改产品源码复用；未为增加覆盖重复整套运行。
- 本次无产品、协议或前端改动，不新增定时执行分支。完成3.9；主Web的隐藏无附加条件、统一表单及浏览器验收仍由6.2/7.9追踪，不能将后端验收替代它们。证据仍在既有临时目录，正式产物在`out/tests/integration/backend/sessions/test_persistent_task_schedules/`。

### 终止与迟到结果的产品验收（2026-10-27）

- 原管理revision、判断结算与接受事实对账链路无需另加终止分支。206 **3 passed／72.46秒**，覆盖event/state两种忙时排队：终止发生在原wakeup已接受但任务未记回执时，排队工作仍按同一item/job/execution完成；尚未接受的动作则被抑制。真实Gateway、任务API和model_stream保留原归属、无伪用户Turn。原忙时测试按新增control参数重命名，全仓无旧selector调用。
- 207 **1 passed／16.69秒**验证迟到脚本保留原结果、不投递、任务继续terminated。该命令的全局`-k terminate`同时排除了显式选择的模型场景（实际10 deselected），不将其计为模型通过；209另行精确运行模型场景 **1 passed／62.83秒**，覆盖pause/edit/暂停试运行/terminate四次实际判断、工作模型零调用、终止后恢复及新试运行均409且完整对象不变。208 Ruff通过，全部运行具进程外期限/4GiB保护且无源码漂移。
- 完成3.7；与原脚本/模型试运行、暂停/编辑及接受后冷恢复证据按未变产品源码共同证明管理边界，不宣称本次重新完整运行全部套件。准确命令、指纹、原始输出和实际退出码仍在既有临时证据目录，正式产物分别镜像三个测试文件。Goal/生成器迁移与主Web管理仍未完成。


### Goal调度并入通用worker（2026-10-27）

- 物理删除GoalRuntimeService及原测试文件，迁齐API、容器、事件订阅、启动和关闭消费者；Goal的激活、目标编辑、终态及启动恢复工作与普通任务共享同一worker和有界槽位。单项Goal函数不持有循环或生命周期。初始化回执先交还工作区，慢恢复在后台推进；暂停后迟到工作重读状态，不重新激活。此切片只完成4.4，goal.json仍是当前唯一Goal状态来源，统一任务owner、一次性导入与fork完整迁移仍由4.2/4.3追踪。
- JOB_STARTED同步冻结Goal归属，工作入队冻结request_id/gateway_id及事件观察时间；终态携带Job快照，避免删除流程先移出注册表导致后台重读失败。关闭先停止动作准入并排空已开始动作，再关闭Job和排空终态，最后停止共享worker；终态记账不触发关闭后的续跑。Session删除结束对应工作项，其他任务继续；后台错误进入原健康失败并保留多个在途异常。
- 227最终单测 **40 passed**；228真实API/SDK/model_stream产品场景 **2 passed／46.28秒**，验证激活后模型完成Goal、原请求身份、内部执行无伪用户Turn，以及慢Goal恢复期间其他任务推进、暂停后迟到恢复不续跑。230共享worker回归 **3 passed／146.48秒**，覆盖忙时接受后暂停，以及两种动作接受后硬杀Backend再恢复。所有最终运行都有进程外期限/4GiB保护，source_changes为空。
- 229相关Runtime/Goal应用Pyright零错误，231本批Ruff通过；232核对API的27项B008诊断与HEAD完全一致，未声称全仓静态分析全绿。全仓app/tests/scripts检索旧类、字段、恢复hook及测试selector无命中。没有协议、数据库或前端改动，不需要schema提升及Web构建。
- 226产品测试发现naive Job时间与UTC观察时间相减错误，改为timestamp差并补齐四种时间组合；227/228已通过。210/211/220的日志调度、类型及非法测试Gateway身份错误均保留原件，最终对应检查通过。216原Goal/Terminal/interrupt综合测试exit124、150秒超时，仅输出一个点，不能算完整通过；221多挂载关闭首项在既有ready.wait 8秒、模型准备前失败，随后生命周期未关闭导致其余5项失败，未放宽期限，也不将这些场景计为通过。性能TODO及Windows既有长路径范围保留。
- 准确命令、HEAD/源码指纹、原始输出和退出码保留在out/tests/temp/persistent-task-migration/artifacts/；正式产品产物沿对应测试路径保存。Goal存储/fork、生成器和主Web管理仍未完成，团队协作继续TODO。


### Goal唯一任务owner与旧文件导入（2026-10-27）

- Goal工具/API、共享worker、fork安装与取消统一使用容器显式复用的PersistentTaskStore；物理删除SessionGoalStore及原测试文件。Workspace SQL21在原任务表保存Goal应用状态、Session归属和退休标记，原定义表保存版本化目标/预算；main Thread只从catalog派生。用量/续跑变化复用原定义，普通任务不消费Goal定义。通用生命周期列由Goal应用状态同事务派生，不是独立可修改的第二状态源。
- 旧goal.json仅由启动时显式导入器消费，目标与原字节指纹同事务提交，随后删除并fsync；删除失败重试不覆盖后续编辑，不复活已清除/替换的Goal，异内容或现有owner冲突明确失败并保留原件。导入枚举原catalog active节点和精确preparing创建记录，不扫盘吸收新Session。SQL16—20升级保留旧任务/运行/计划身份，实际结构变化才将Workspace schema升至21。
- fork仍使用原冻结Goal ID与目标创建时间，preparing由原journal限定能力且不出现在Goal恢复列表；发布后重试不重装，失败先退休任务再由原创建owner清理。移除SessionCreationService允许/校验goal.json的旧物化分支；Session删除排空时也退休当前Goal。现有child Goal拒绝合同保持不变，Goal API与外部DTO不改变。
- 239 **58 passed／4.01秒**，包括导入删除失败、清除/替换后重试、指纹冲突、事务回滚及SQL旧库升级、Goal业务/工具/worker行为；248最终存储检查 **9 passed／1.15秒**，增加定义缺失明确报错，不将损坏误读为空Goal。242调用方、导航生命周期、建库及发行SQL资产 **78 passed／31.64秒**；245 fork业务单测 **51 passed／2.04秒**。
- 243真实API/SDK/model_stream的激活到模型完成与慢恢复暂停场景 **2 passed／34.99秒**。247完整正式matrix **2 passed／63.74秒**：真实Backend三次重启保留修改并不复活已清除Goal；真实Terminal/Browser与公开fork入口验证准备不可见、独立身份/零用量、发布后编辑不被重放覆盖及安装后发布前失败的撤销。关键状态由产品入口生成；迁移单测手工准备旧文件仅用于验证旧库输入。
- 249新增存储/导入模块、251完整任务store及共享worker应用Pyright均零错误；246本批Ruff及250最终变更文件Ruff通过。所有最终运行具进程外期限/4GiB保护且source_changes为空。无公开协议/前端源码变化，不需要重新生成绑定或Web构建；全仓搜索旧store/import/测试selector无生产调用。
- 235为51 passed/7 failed，237同样发现导入枚举漏传必填workspace_id，236因此主动停止（exit130、两个setup error）；修正后239/240通过。241为1 passed/1 failed：空历史测试误用必须有点击item的view_prefix_fork；改用既有full_rollout_copy。244亦1 passed/1 failed，实际故障已通过HTTP返回500但测试误期待Python异常；改为严格核对500、原故障事实、request_id及原创建aborted/任务退休，最终247通过。失败原件均保留。
- 本切片完成4.2/4.3的实现与定向验证；通用应用传输/列表仍由2.1及6.5迁齐，store中已留明确TODO，不将旧自动化DTO强行解析Goal。完整Goal在途进程恢复及fork发布窗口的硬重启组合继续由7.4追踪；原多挂载准备长尾、Windows长路径、团队协作TODO不扩大本次范围。准确命令、HEAD/源码指纹、原始输出和退出码沿既有证据目录保留，正式新增产物在out/tests/integration/backend/sessions/test_persistent_task_goal_storage/。


### Goal 统一查询、SSE 与恢复展示（2026-10-27）

- 公开任务 DTO 增加 application_type/application_version，严格校验 automation 组件与 Goal 状态互斥及同一任务身份；工作区列表/详情直接读取同一 owner 的当前 Goal，保持 Goal 六状态、独立的任务 revision/定义 revision 和原 Goal revision。退休/清除/替换的旧身份及 preparing 节点不可见，缺失不可变定义明确报错。普通组件调度显式读取 automation 应用，Goal 仍由同一 worker 推进；没有新增持久状态或数据库结构。
- Goal 原业务入口修改/清除后，原有有界 channel 发送完整列表失效通知，HTTP SSE 重读权威快照。初始化只枚举恢复身份，逐项工作仍在初始化回执后进行；Goal 恢复工作冻结原身份/revision，恢复期间展示 recovering，暂停保持原 Goal 应用状态，完成后恢复真实阶段。Goal 的通用编辑/控制、历史展示及右侧摘要仍由2.1/6.5迁齐，当前通用组件操作明确拒绝 Goal，不修改派生 lifecycle 或伪造空历史。
- 261 正式 matrix **3 passed／91.96秒**，通过真实 Backend HTTP、SSE 与重启验证统一列表、Goal 编辑/清除通知、原身份/预算及无额外模型执行，同时复验 fork 安装/发布/撤销。266 **5 passed／45.56秒**，验证慢 Goal 恢复的真实管理投影、暂停与其他任务推进、普通任务恢复期间排队及暂停、worker 初始化/运行失败和健康错误、原模型工具完成 Goal。慢 Goal 的 active 准备仍经 service 设置，只证明受控生命周期恢复；完整在途执行/fork 发布窗口硬重启仍由7.4验收，未以它代替完整恢复产品入口。
- 267 最终单元/存储/SSE合同 **42 passed／4.75秒**；268 相关任务 DTO/store/service/runtime/Goal应用 Pyright零错误，269 Ruff通过。264 在恢复展示改动前另有4项管理/Goal/真实SSE回归通过，重叠场景不累计冒充独立覆盖。最终运行均有进程外期限、4GiB保护且source_changes为空。
- 253 Buf lint/build及对真实提交e4d9ce890的breaking检查与全平台绑定生成通过，254 OpenAPI更新仅涉及本任务响应。257真实Web类型检查、262构建通过，只有既有chunk提示；后续冻结声明修正不改变公开schema及前端资产，按依赖复用。没有实现新Web界面，不将构建或Backend HTTP验证表述为Gateway浏览器交互验收。
- 255最初有5项类型诊断；259剩3项字段收窄诊断，改为静态分析可识别的frozen类声明后263/268均零错误，没有忽略诊断。256为41 passed/1 failed：preparing创建尚未发布catalog节点，实际已正确拒绝；新测试误期待另一个错误文字，改为精确核对原catalog错误及目标Session身份，260/267均42 passed。失败原件保留。
- 本切片完成7.8；2.1仍含会话生成器应用合同，6.5仍含UI与通用管理动作，不提前勾选。准确命令、HEAD/源码指纹、原始输出和实际退出码沿用out/tests/temp/persistent-task-migration/artifacts/；正式产品产物在对应test_persistent_task_goal_storage及test_persistent_tasks目录。生成器整体迁移、主Web管理、完整进程恢复组合及Windows既有长路径范围仍未完成，团队协作继续TODO。

### Goal 续跑接受前硬重启（2026-10-27）

- 原实现先保存 last_continued_job_id 再提交准入，进程硬杀无法执行异常补偿；272通过真实 Goal PUT 和 Backend 硬重启复现 active Goal 永不执行，15秒内无模型调用。改为在原 Goal 锁内先接受同一 source/wakeup 再保存标记，删除提前写入和异常回写；没有增加持久状态或第二条执行链路。
- 277定向产品验收 **1 passed／63.43秒**，在接受前屏障硬杀，严格验证原 Goal 身份、一个 wakeup、两次模型调用、零用户 Turn，以及完成后再次硬重启不重复执行。274相关业务/锁/worker/存储/工具 **52 passed／3.81秒**；278 Ruff通过。276 Pyright仍有原 object哨兵两项既有诊断，与198相同，新续跑方法无诊断，未声称该文件类型全绿。最终测试均有进程外期限、4GiB保护且source_changes为空。
- 271在准备阶段查询尚不存在的rollout库，修正为严格验证库尚不存在后272才完成业务复现。273完整套件为3 passed/1 failed/1 teardown error：Goal工具已完成目标，测试却在最后文本回复前检查两次调用；改为查询唯一wakeup并等待原Job终态，保留严格次数断言，277通过。275只因import空行失败，278已修正；所有失败原件保留。
- 本切片只证明接受前中断，接受后标记前、完整在途执行与fork发布窗口继续由7.4追踪，不勾选整项。无公开协议/前端变化，不重复Web构建。命令、源码指纹、原始输出和退出码沿用既有临时证据目录，正式产物位于out/tests/integration/backend/sessions/test_persistent_task_goal_storage/。

### Goal 已完成执行的启动对账（2026-10-27）

- 280经真实 Goal API、model_stream 与 Backend 硬杀复现：执行在接受后标记保存前完成，重启的 active Goal 在15秒内没有下一轮。修复沿当前 Goal ID/revision 的原 idle/objective 准入根及后继幂等引用查找尾执行，unknown仅恢复内存归属，已结束状态复用实时终态处理函数。原队列核验 execution/admission identity，原 canonical 最终输出提供总用量；不新增持久状态、数据库结构或第二份 Goal 结果。
- 292完整正式 matrix **6 passed／288.02秒**。新增两种标记窗口均通过产品入口生成状态：原执行完成后硬杀，恢复保留原 wakeup；两个执行各记11 token、合计22，严格核对两个原Job、三个模型调用、零用户Turn，再次硬重启无额外执行。295原真实API/SDK/model_stream激活完成与慢恢复暂停回归 **2 passed／35.26秒**。289相关单元 **72 passed／6.92秒**；293补充准入身份核验后的定向检查 **40 passed／5.89秒**，重叠结果不累计。所有最终测试具进程外期限/4GiB保护且source_changes为空。
- 291领域进度合同、Goal应用与共享runtime的Pyright零错误；294全部本批Python文件Ruff通过。287扩展infra Pyright仍有10项既有sibling方法声明诊断，新增进度读取无诊断；不宣称infra或全仓类型全绿。无公开协议/前端变化，按依赖复用之前Web结果。
- 281因把codec返回的dict当消息对象读取导致启动500，改用原project_annotations；282为3 failed/62 passed，统一终态后对失败分支读取不适用时间，修正为仅agent_end计算实时elapsed；285因测试给原_sql多传参数未进入重启，改用原签名严格核对唯一执行终态。288旧240秒总期限下exit124、241.27秒，无完整summary；suite从3增至6项后调整总envelope为360秒，场景期限与身份/次数断言未放宽，最终292通过。失败原件均保留。
- 深历史续跑链仍逐项读取原准入，代码留有测量后优化TODO，尚无长历史性能数据。完整在途执行、无Goal准入根的历史用户执行、目标编辑与旧在途Job的全部重启组合及fork发布硬杀仍由7.4追踪，不勾选整项。准确命令、HEAD/逐文件SHA256、原始输出和退出码保存在out/tests/temp/persistent-task-migration/artifacts/；正式场景产物位于out/tests/integration/backend/sessions/test_persistent_task_goal_storage/。

### Goal 在途模型与工具 checkpoint 恢复（2026-10-27）

- 新增在途模型的原目标、暂停、修改目标三场景及受支持 read_context 的工具结果提交前硬杀恢复。读取本工作区 inventory 走真实 query owner；原执行、模型调用与 assembly/hash 不变，后继模型收到一次且正确配对的完整工具结果。精确核对 token、归属、零伪用户 Turn 及再次硬重启无重复；未扩展产品自动恢复工具名单。
- `goal-recovery-full-matrix-312` 正式 matrix 为 **10 passed／523.51秒**，实际退出0，源码前后指纹相同；该套件从6项增加至10项，将总期限由360秒调整为660秒，场景自身期限和业务断言未放宽。此前299的三项、311的工具场景为定向证据，与整套重叠不累计。313 Ruff、314 OpenSpec strict通过。
- 保留308文件工具不能自动续接、310 canonical限定ID与Provider wire ID不同等失败原件；最终使用已支持的本地inventory入口，删除排错期间无用Gateway fixture增强。命令、源码指纹、raw及退出码在 `out/tests/temp/persistent-task-migration/artifacts/runs/`。7.4整体、fork发布窗口硬杀及生成器迁移继续未完成，团队协作TODO不变。

### Web 普通任务管理与权威回读（2026-10-27）

- 主窗口底部自动化接入原工作区任务API/SSE，列表与详情展示Goal/普通任务、生命周期、阶段/原因、目标、恢复任务数及下次触发；配置/管理写入采用完整对象，旧订阅在写入及切换时撤销。组件表单覆盖现有v1触发/判断/字面上下文/source动作与高级策略，保留原revision；未知组件不可编辑，JSON null和未展开策略不被默认值覆盖。运行记录区分仅保存与执行已接受，未把投递称为执行完成。
- `ui-state-012` 6通过，覆盖跨后端切换、旧闭包、真实错误回读、恢复期间暂停的迟到快照、null/高级字段和未知版本；`ui-types-013`真实tsc、`ui-build-014`Web构建及`ui-python-static-005`Ruff通过。`web-product-017`正式matrix **1通过／38.50秒**，实际退出0；真实页面生成关键状态，Backend保留2条原运行，试运行无投递，规则检查只保存上下文，模型零调用，Goal原对象未变；另一客户端经真实API编辑后原请求返回409，界面读回完整新定义，暂停/恢复/终止及重新加载均核实。上述最终运行源码前后指纹一致。
- 006/010暴露表单的可访问名称与布局问题并修复；015为测试过早unroute导致原请求已处理，016已走通至终止但刷新时未等待页面/偏好初始化。017通过具名用户保存偏好并等待初始化，不伪造错误或成功响应。008仅有测试回调变量的TS收窄错误，改用明确收据数组后013通过；业务断言未放宽。准确命令、指纹、raw和退出码在 `out/tests/temp/persistent-task-ui/artifacts/`；截图/DOM及后端回执沿正式同名测试路径保留。
- 当前只关闭6.1和上述子项；五类快捷模板、Thread跳转/历史冻结提示详情、会话关联管理和完整恢复Web门仍未完成。旧定时生成器保留原功能入口，并以代码TODO追踪后续唯一owner迁移和物理删除；未新增生成器账本、调度或第二执行链路。团队协作继续TODO。
- 交付差异与截图复查修正两条CSS选择器落点，内容容器使用纵向滚动且不改变标题截断。最终018重建通过，019正式浏览器再次通过（包含真实拖动底部面板高度后截图），退出0、源码无漂移；013类型与012状态测试的TS依赖未变，复用其结果。017保留为前一布局版本证据，不将其截图视为最终布局。

### 生成命名与导航预览的工作区归属（2026-10-27）

- 目标工作区提供唯一预览计算，渲染的标题和导航段供原创建链路消费；继续会话使用 catalog 当前位置，预览不创建节点或分配物理 locator。删除 Gateway 旧接口、预览DTO、模板渲染和 children/folder 物理占位，迁齐协调器、前端、proto各平台生成资产及工作区OpenAPI。非法导航段/未知变量/时间格式与错误kind或缺失节点明确拒绝，前端切换目标或表单时撤销旧预览。
- `units-final-016` 46项通过，`ui-contract-final-021` 1项通过；`gateway-008` 2项通过／24.09秒，验证原手动运行幂等和明确目标路由。`product-014` 2项通过／44.13秒，真实Gateway/Backend验证目录/继续会话预览、400/404、catalog及manifest不变，以及预览后真实生成沿相同导航登记稳定ID物理目录。`web-product-015` 1项通过／41.39秒，真实Chromium从底部面板预览并核对后端catalog不变，同时复验原任务管理、真实409回读和零模型调用；已查看最终预览截图。
- `types-final-020`真实Web类型、`web-build-final-022`构建、`python-types-013`相关Pyright、`protocol-final-023`Buf lint/build均通过；OpenAPI变化仅新增预览入口与3个相关schema。`static-final-019`逐项对比HEAD的Ruff诊断，无新增；两份旧路由/协调器保留既有诊断，未宣称全仓静态全绿。上述最终测试/检查均有进程外期限、4GiB保护及前后相同源码指纹；浏览器后仅修正JSON null的TypeScript适配，运行时链路未变，复用其产品结果。
- 004为46通过/2失败：旧API测试缺少显式workspace_id，在进入被测服务前返回400；006保留原始响应与服务未被调用证据，未改这些无关测试或算为通过。007为1通过/1失败：已删除POST受同路径通用PATCH/DELETE路由影响实际返回405，修正准确预期后014通过。018暴露新TS适配遗漏JSON null，修正后020/021通过；011仅因基础环境缺pyright无法启动，013通过uv隔离附加依赖完成。失败原件均保留。
- 命令、逐文件SHA256、raw与实际退出码保存在 `out/tests/temp/generator-navigation/artifacts/`；正式产品输出沿同名测试路径保留。此切片只完成5.1.1，定义/调度/三份可变账本的一次性迁移仍由5.1—5.5追踪；团队协作继续TODO。


### 生成器定义校验的工作区归属（2026-10-27）

- 新增工作区`session-generations/validate-definition`，保存校验和执行共用唯一provider解析；provider缺失/版本不支持明确返回不可用，既有Gateway创建仍保存为blocked。非法config、工作区分离、节点类型和精确Thread归属由工作区校验，400/404不被mount_missing=pause误当作离线而接受。
- Gateway逐个将placement/context/strategy/execution路由ID解析为各自backend UUID，保留目标身份和同一request_id，单次POST校验；删除Gateway JSON Schema验证、breadcrumb探测及重复工作区关系规则。嵌套PATCH候选与权威写入均完整解析DTO，修复裸dict进入后续消费者的问题。
- `unit-003` **50 passed／3.28秒**，含既有生成幂等/恢复/回报与控制面认证；Gateway单测现在证明单次传递、逐个身份解析和错误类别，实际业务校验迁到真实产品入口。
- `product-definition-004` **1 passed／25.72秒**，真实Gateway/Backend与产品创建的Session验证provider/版本、config、catalog kind、Thread所属、跨工作区拒绝、嵌套编辑持久化与blocked；catalog和manifest未因校验新增。`gateway-007` **2 passed／25.07秒**，验证显式目标的校验/执行路由、CRUD及运行幂等。`product-strategies-011` **2 passed／56.45秒**，复验真实新建和继续会话动作。
- `static-final-008`逐文件对照HEAD无新增Ruff诊断；`python-types-baseline-013`确认修改的服务/DTO无错误，Gateway协调器仍有两项HEAD既有optional访问诊断。`python-types-006`的35项全目录错误均位于未改动的message_dispatch/reporting mixin；未把此结果表述成全目录类型检查通过。`static-005`的新增无用变量已删除。
- `protocol-generate-001`完成协议生成、Buf格式/依赖及相对HEAD的breaking检查；`web-types-012`真实tsc、`web-build-010`Web构建通过，仅既有chunk提示。生成绑定与OpenAPI随同更新。
- 准确命令、逐文件指纹、原始输出与退出码保存于`out/tests/temp/generator-definition/artifacts/`，正式产品产物沿同名测试目录保留。本次未重复fork完整产品验收；运行journal/调度和一次性迁移仍由5.1—5.5追踪，整体保持未完成。原生成执行请求的路由身份与既有journal合同暂不改写，后续随唯一owner切换迁移，不建立生产双读。
