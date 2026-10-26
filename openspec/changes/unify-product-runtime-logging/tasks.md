## 1. 合同与配置迁移

- [x] 1.1 全仓检索日志路径、轮转、旧 logger 配置、环境覆盖及其直接/间接消费者，核对当前 Backend 多工作区生命周期基线。
- [x] 1.2 建立唯一的日志布局与默认轮转合同资源及跨语言样本，明确角色文件名、实例身份、pretty/compact 字段和 raw output 边界。
- [x] 1.3 在 Gateway inline/schema/dev 配置加入 `logging.runtime.level/pretty`，Gateway 从已持久化的 v2 推进至 v3，Workspace 从 v1 推进至 v2，迁齐策略声明并保持现有来源层次。
- [x] 1.4 实现显式跨域迁移的预览、备份、冲突选择、原子替换与中断恢复，覆盖用户级、local、明确指定工作区的文件、SQLite 来源层及 active snapshot，保留待结算状态与既有 CAS；删除旧 Workspace logger 运行期读写、schema 与 accessor。

## 2. 唯一日志输出与路径归属

- [x] 2.1 统一 Python 应用日志字段、等级与格式，将 Backend、Gateway、Uvicorn 接入唯一 handler 路径，消除重复传播和启动时按工作区比较 logger 的逻辑。
- [x] 2.2 为 Launcher、Terminal、Browser 接入共享 JS 服务端日志入口，迁齐产品 console 调用，保留第三方与 PTY 原始输出。
- [x] 2.3 贯通 Launcher、Gateway、detached、开发及显式单服务入口的日志路径分配，迁移新实例默认根并保留显式 Launcher 路径覆盖，移除旧默认路径和重复常量。
- [x] 2.4 统一打开时轮转及句柄所有权，确保现有 Gateway 重启/交接复用存活子进程时不轮转活动文件、不双写、不破坏存活合同；迁齐 SSH 转发日志入口。
- [ ] 2.5 在现有 runtime/config 诊断中展示进程身份、owner、绝对日志路径、来源、applied revision 和实际日志设置；stdout-only 入口明确标记。

## 3. 进程级热应用

- [x] 3.1 为真实进程提供受既有本机认证保护的日志设置应用/确认入口，按实例与 revision 幂等处理，不按工作区重复应用共享 Backend。
- [ ] 3.2 将日志注册为 Gateway 配置 runtime consumer，复用现有 Launcher 控制通道和托管服务管理链，完成 prepare、逐进程 apply、proof 和 promotion。
- [ ] 3.3 处理新增/退出进程、同端口换代及迟到确认，保证新实例 readiness 前取得有效设置；Launcher bootstrap 设置在 ready 前被权威设置替换。
- [ ] 3.4 贯通拒绝、超时、最终 CAS 冲突及补偿失败，复用 apply journal 与 recovery_required，逐进程暴露真实状态；日志 sink 错误避免递归和静默吞噬。
- [ ] 3.5 保留混合候选整体生效规则；移除日志必需重启的旧判断与 schema 文案，MCP 等其它依赖仍按其既有策略生效。

## 4. 集中验证与验收

- [ ] 4.1 完成受影响 Python 静态分析和 JS 静态检查；若修改 Web 诊断消费者，同批完成真实类型检查和 `bun run --cwd src/clients/web build`。
- [ ] 4.2 定向验证跨语言等级过滤、格式字段、异常栈、路径覆盖、轮转顺序、权限/写入失败与重复捕获，使用共享样本区分正确实现与现有分散实现。
- [ ] 4.3 验证仅旧配置、仅新配置、新旧一致、新旧冲突、不同工作区覆盖冲突、重复迁移及迁移中断恢复；确认运行期无旧字段双读。
- [ ] 4.4 通过真实 Launcher/Gateway 与隔离 home 启动 Backend、Terminal、Browser，并让一个 Backend 挂载两个隔离工作区；修改真实用户级配置，验证 INFO→DEBUG→WARNING、pretty 切换、有效 revision、各进程输出和 PID/资源身份保持。
- [ ] 4.5 在真实应用链注入一个消费者失败、最终 CAS 冲突与补偿失败，验证 active/pending 和逐进程状态；验证同端口换代、新实例启动及 Gateway 交接不丢失日志去向。
- [ ] 4.6 在 WARNING 配置下经过真实终端工具执行包含中文、ANSI、多行及 DEBUG 文本的命令，逐字节核对输出与退出码；核对会话 Trace/LLM/工具结果仍在原工作区路径。
- [ ] 4.7 所有测试使用现有 matrix 或进程外超时与内存保护；正式产物镜像测试路径保存，临时探索只用 `out/tests/temp/unify-product-runtime-logging/`，保留命令、源码指纹、原始输出和实际退出码。当前未要求新增 UI 交互；若实施新增交互则补真实浏览器验收。
- [ ] 4.8 检查差异并清理重复实现、旧常量/路径分支和失效测试，更新受影响使用说明及迁移说明，验证 OpenSpec；明确未通过与未验证的平台和行为，不以任务勾选代替业务验收。

### 1.1 实施基线与优先级

已核对当前源码的配置、落盘、进程交接和直接／间接测试调用方；完整检索命令、原始输出和128个命中文件的指纹保存在 `out/tests/temp/unify-product-runtime-logging/artifacts/inventory-001/`。这是源码盘点，不代表真实多进程验收通过。

- 两域 inline 当前均为配置 v1，但 Gateway connection_id 链已经持久化 v2；本变更 Gateway 推进至 v3、Workspace 推进至 v2。`configs/legacy_migrations.py` 的旧混合配置 v4不是两个当前域的版本。迁移同时覆盖 SQLite 来源层／active 快照、测试配置及 Workspace `get_logger_level/get_logger_pretty` 的全部调用方。
- Backend `app/main.py` 当前逐工作区比较日志设置后修改进程 root logger；Gateway lifespan使用默认设置。Backend group共用一个进程，辅助服务按成员持有，改动必须按实际进程应用。
- Launcher 的默认路径是 `state/launcher/logs/services.log`；Gateway在 `state/gateway/logs/` 分配子进程／SSH日志。两处重复登记64MiB／3份轮转。detached已通过 `BOXTEAM_SERVICE_LOG_CAPTURED` 交付输出所有权，迁移须贯通该入口和开发启动脚本。
- `runtime/local_workspace.py` 与 registry runtime显式接管、detach仍存活的Backend及辅助进程；新日志owner不得为了迁移路径重启这些进程或轮转其活动文件。
- 复用 Gateway config `on_runtime_config`／rollback、apply journal及Launcher supervisor控制socket。该socket当前主要承载handoff；日志应用须扩展原通道的消息合同，并补进程实例确认，不能假定已有热更新能力。

优先完成本变更的布局与落盘owner纵向切片，再做配置跨域迁移和进程热应用；它排在当前提示词文件输入切片之后、其余来源扩展之前。团队／协作延期项继续保持TODO。

### 1.4 显式迁移只读预览（2026-10-26，整项未完成）

- `uv run python -m configs.boxteam migrate-runtime-logging --preview` 读取用户/local、显式工作区、SQLite 来源层与 active，按字段报告冲突并支持显式选择；已有新 Gateway 层保持原覆盖语义。预览不安装、不初始化数据库、不扫盘、不提交候选，也不输出完整业务配置或密钥。报告明确为 `logging_preflight`，不代表完整 schema 校验或可写入证明。
- 使用只读 SQLite 事务，能看到尚未 checkpoint 的 WAL 提交；报告保留原来源 digest／revision，不用正文重新计算的摘要冒充 CAS 基线。pending、claim、active 恢复态及未结算 journal 明确报告，已完成 journal 保持历史。版本基线修正为 Gateway v3／Workspace v2，不能复用既有 connection_id 的 v2。
- 最终 `logging-migration-preview-tests-final-141` 为66通过；新增 planner／测试类型检查 `logging-migration-preview-new-types-final-142` 零错误。首次整体类型检查139发现CLI开发安装分支两项诊断；完整HEAD归档对照140同为两项、无新增，不声称整体类型全绿。准确命令、源码指纹、raw与退出码在既有 `out/tests/temp/unify-product-runtime-logging/artifacts/` 下；归档临时树与tar已删除。
- 备份、排他／CAS写入、中断恢复、schema／模板切换及旧logger消费者删除尚未实现，不带 `--preview` 的命令明确拒绝写入。1.3／1.4不勾选，当前仍6／22；此证据不替代真实迁移、跨进程自动热应用、完整Web或Windows安装验收。

### 2.3 日志布局切片（2026-10-26）

- 默认根、角色文件名与64MiB／3份归档只有 `resources/logging/runtime-contract.json` 一处声明，Python／JS读取同一发行资源及路径样本。现有两平台打包共同复制整个 `resources/`；Launcher使用已选择runtime的显式application root，缺资源明确失败。
- 迁齐初次启动、存量恢复、本机手动注册、联邦管理、SSH隧道／转发与开发重启。开发单Backend入口明确stdout-only；其余业务进程继续只输出流。Launcher与Gateway分别落盘，detached不再次tee Gateway到Launcher捕获流，显式覆盖仍只作用于Launcher。
- 证据根：`out/tests/temp/unify-product-runtime-logging/artifacts/`。最终定向Python `paths-python-final-012` 67通过，JS `paths-js-final-015` 60通过；真实启动日志查询 `paths-entry-final-014` 1通过；最终真实Launcher `paths-launcher-final-028` 2通过，核同一Gateway文件、无交接轮转、Backend单次启动、两文件分流及冲突路径在轮转前拒绝。`paths-lifecycle-010` 中106项生命周期单测和1项真实双挂载执行通过；当时发现的旧bootstrap路径导致日志查询失败，已迁齐并在014验证，不把该首次运行写为全绿。
- `paths-routing-017` 29通过、20项fixture准备失败，均引用已删除的 `registry.runtime.resolve_backend_workspace_id`。完整HEAD归档的 `paths-restore-baseline-022` 已复现同一fixture失败，本切片不扩展修复。`static-comparison-019.json`及`static-comparison-031.json`核Ruff46条基线／46条当前、两组Pyright分别7／7及12／12，无新增；10个JS文件语法检查及18个Python文件AST解析通过，不能称全仓静态全绿。
- 1.2仍缺实际日志记录格式及跨语言输出样本；2.4仍需完成运行实例的日志owner记录、跨Gateway存活接管的实际去向与冲突核验；2.5的进程诊断及第3节配置热应用尚未实现。该次切片结束时为2／22，未声称完成日志功能、Windows原生或完整4.4／4.5验收。


### 1.2／2.2 日志记录与进程身份切片（2026-10-26）

- Python 与 JS 的默认等级、格式、时间与关联字段从同一合同资源取得，真实格式器共同消费 `record-samples.json`。各进程生成独立运行实例 ID；重复配置保持身份，日志仅附真实存在的关联字段。标准 Python logging 与 Uvicorn 复用一个 handler，模块入口先于 Uvicorn 首条启动日志初始化；sink 写入失败直接保留原异常。
- Launcher、Terminal、Browser 受管服务端诊断迁到 `src/shared/serverRuntimeLogging.mjs`。原 Launcher 私有合同 loader 已删除；文件 IO 与 detached 调用方显式接收选定 runtime 的合同。CLI 的 help／version 等用户输出、第三方流、网页 console 和 PTY 字节保持原入口。发行源码复制器同时携带共享模块与资源；测试进程装具显式传 application root，不借工作区或服务 cwd 猜根。
- 证据根仍为 `out/tests/temp/unify-product-runtime-logging/artifacts/`：`record-python-final-047` 26通过，`record-paths-python-055` 61通过，`record-js-final-048` 100通过，`record-all-services-final-054` 为完整辅助服务 matrix（Node175／Bun383通过）。此前039的4项失败来自嵌套cause文本缩进的观察合同，053的20项失败来自遗漏下沉目录的两份console测试消费者；均迁齐后在054核验，不把首次运行写为全绿。
- `record-launcher-final-049` 2通过，真实交接核两代Gateway的首条Uvicorn日志、不同实例身份、Backend不重复启动及日志不双写／不交接轮转。`record-node-final-050` 23通过，包含真实Terminal／Browser错误入口、原生诊断及进程装具生命周期，不是纯helper验收。
- `record-static-comparison-051.json` 使用完整HEAD归档比较受影响Python文件：Ruff3／3、Pyright246／246，无新增；`record-static-js-final-056` 对35份实际JS文件语法检查通过。静态基线临时树已删除，原始命令、输出、指纹与退出码保留。未修改Web页面／DTO，未新增Web构建或Windows原生验收。
- 当前为4／22。2.1尚需与1.4一并移除Workspace日志比较及accessor；独立实例身份不等于2.4所需的活动写入owner证明。配置跨域迁移、受认证的逐进程热应用、revision诊断及WARNING下真实PTY逐字节验证仍未完成。

### 3.1 受认证进程控制切片（2026-10-26）

- Backend、Gateway 复用同一进程级内部路由；Terminal／Browser 复用同一 Node 控制入口。只接受无浏览器来源头的 loopback 管理请求，核对 Gateway 现有私有 token；Backend／Node 由启动 owner 显式交付凭据，未配置则拒绝，不把固定开发 token 或资源 owner 头当作认证。共享 Backend 路由不取得工作区容器，不按挂载工作区复制设置。
- Launcher 扩展原控制 socket／命名管道，不新增端口或 daemon；发行版也提供该私有控制能力，公开流量仍直接由 Gateway 监听。实际应用核对 `runtime_instance_id`、`expected_revision`，同 revision 的相同设置幂等、不同内容明确冲突；替换进程的旧实例请求拒绝。Python 从唯一 handler 取得实际设置，JS 复用现有设置，不新增配置持久化事实源。
- `control-real-launcher-062` 首次真实检查发现 Launcher 的 DEBUG 应用确认绕过原文件 sink；已改为复用原 Logger，并由最终 `control-real-launcher-final-073` 两项真实检查通过。实际覆盖五类进程的认证、非法参数、DEBUG／WARNING 和格式变化、单次确认落盘、PID／实例保持，以及原工作区执行与 Gateway 交接后旧实例拒绝；不是只验证 helper。此检查仍不代表配置候选自动驱动热更新、逐进程补偿或双工作区完整验收。
- 最终证据仍位于 `out/tests/temp/unify-product-runtime-logging/artifacts/`：`control-python-final-067` 49通过，`control-js-final-068` 193通过，`control-services-final-071` 为辅助服务完整 matrix（Node175／Bun383通过），`control-syntax-js-072` 8份JS语法检查通过。所有运行有进程外保护、原始输出、源码指纹与退出码。
- `control-static-final-069` Ruff通过；`control-static-comparison-074.json` 使用完整HEAD归档比较Pyright，11条基线／11条当前，无新增。基线临时树与tar已删除，原始输出保留。未修改Web，不新增Web构建或Windows原生验证。
- 当前5／22。2.4活动日志owner证明与冲突互斥、2.5完整日志去向诊断、跨域配置迁移及3.2起的候选应用／补偿仍待实现；不能将手动进程控制通过表述为全产品自动热更新完成。

### 2.4 Gateway 托管日志归属切片（2026-10-26）

- Backend、Terminal、Browser、SSH 隧道／转发和开发重启统一由现有启动入口登记日志归属，短临界区同时使用进程内锁与系统文件锁。打开时先核原写入者，控制器退出不释放存活子进程的归属，根退出仍核同组后代；交付中断明确拒绝重开。正常关闭先确认完整进程组退出、关闭父描述符，再释放归属。启动后登记失败会清理实际子进程，清理失败异常保留实际进程和日志句柄。
- Gateway 交付冻结的文件身份，Python／Node 控制状态同时核实际 stdout/stderr 与当前路径的 device/inode；接管在既有健康／完整组核验后，通过原私有凭据取得证明，不重开或轮转文件。`log_sink=null` 只表示未取得这份证明，不能用它把 Launcher 的真实文件 sink 宣称为 stdout-only。
- 最终定向 `ownership-python-final-085` 256通过；`ownership-js-078` 193通过；`ownership-real-launcher-final-086` 4通过，覆盖真实 Launcher 交接、受管 Backend／Terminal／Browser 的 PID、实例、revision 和文件身份保持，以及托管终端真实产品入口。原始输出、命令、源码指纹与进程外保护记录仍在既有临时证据根。
- 扩大的 `ownership-python-077` 为323通过／5失败；完整 HEAD 归档 `ownership-baseline-079` 在相同5项断言复现失败，均为既有测试捕获 httpx INFO 后断言日志全空。本切片不改写这些预期。`ownership-static-final-082` Ruff通过；`ownership-static-comparison-087.json` 核Pyright基线30条／当前8条，无新增；剩余8条不宣称全绿。
- 2.4仍未勾选：Launcher／detached 自身文件 sink 的同路径互斥与归属尚需迁齐。Windows 原生独占打开分支尚未实机验证；未修改Web页面，不新增Web构建。本切片不代表完整2.5诊断、配置跨域迁移或候选自动热应用完成，当前仍为5／22。

### 2.4 Launcher／detached 日志归属完成（2026-10-26）

- Launcher 前台、Gateway 文件与 detached 交付统一 await `openServiceLogFile`，旧描述符入口及全部调用方已迁齐。稳定 guard 内发布冻结文件身份；Unix 原生 flock 随实际输出描述符保留，Windows 使用原生共享句柄探测。活动写入者、存活后代及外部替换不能触发重开或轮转；只有捕获标记不能接管日志。
- 使用 Launcher 正式依赖 `@lickle/lock`，不依赖源码根 node_modules。最初候选 `fs-native-extensions` 在 Bun 原生调用崩溃，已移除；`launcher-dependencies-100` 验证 staged Launcher 自行解析并运行 Linux 原生锁，Windows 目标包也正确带入。便携版仅移除 Launcher 顶层 runtime 可选依赖，安装生产依赖时保留锁的 Windows 原生包。
- 所有权锁释放失败仍关闭未交付日志句柄；归属写入、关闭与临时文件清理失败按顺序保留原异常，失败正文不发布。`launcher-ownership-failures-final-123` 43通过；最终相关完整 JS `launcher-ownership-all-js-final-132` 263通过／1平台跳过；真实 Launcher／Gateway 交接 `launcher-handoff-real-final-133` 2通过。
- 真实 Windows `hyf-win` 最终 `launcher-windows-native-final-131` 12通过／1 POSIX 场景跳过，覆盖原生互斥、detached 分流、根退出后的继承句柄、关闭后重开、篡改证明及写入／清理故障。仅在隔离临时树执行，未重启目标已有服务。125在观察器启动阶段因深层 cwd 报 ENOENT，130暴露重开检查未恢复进程命令边界；已修正测试接线，未放宽业务断言。126另有固定端口 EADDRINUSE，结束并行网络操作后的132全组通过；原始失败记录保留，不宣称已证明具体端口占用来源。
- Python 定向 `launcher-ownership-python-096` 256通过；修改 Python 的 Ruff102及单文件 Pyright110通过；扩大类型检查115仍有8条既存错误，118对比无新增。098中一项托管终端启动报 BadStatusLine，104单独通过；保留现场与原始输出，未确认根因。116／117因中断没有完整结果，不计为通过。
- 证据仍在 `out/tests/temp/unify-product-runtime-logging/artifacts/`，正式测试现场按各测试路径留存；候选锁库的临时下载源码已删除，版本记录和失败证据保留。当前6／22；配置跨域迁移、2.5完整诊断和候选自动热应用尚未完成。Windows installed 全产品启动、完整热更新及 macOS 原生验收仍未验证，相关原生终止性能 TODO 保留；未修改 Web，不新增 Web 构建。

### 进程控制响应统一（2026-10-26，3.2 的基础切片）

- 接管和后续热应用共用严格的进程日志状态合同；只向本机管理根地址发送既有私有凭据，拒绝重定向。响应必须核对服务与 PID；应用确认还核对运行实例、revision 和实际设置，不能以 HTTP 成功替代确认。删除接管入口的裸 dict 身份判断。
- `process-control-contract-144` 为128通过；`process-control-real-handoff-146` 两项真实 Launcher／Gateway 验证通过，包含受管服务接管和原文件去向保持。新控制模块／schema／定向测试 Pyright147零错误，相关 Ruff通过。扩大Pyright145的两条 `GatewayServiceName` 诊断与既有归档基线084一致，不称整体类型全绿。
- 原始命令、源码指纹、输出与退出码保留在上述证据目录。当前仍6／22；此切片未完成候选协调、跨域迁移或完整诊断，不勾选3.2。

### 1.3／1.4／2.1 配置跨域迁移完成（2026-10-26）

- Gateway 模板／schema／来源／active 使用 v3，Workspace 使用 v2，旧 logger schema、accessor 和挂载工作区间的进程日志争用均已删除。显式命令现在支持预览、字段选择、原字节／一致 SQLite 备份、owner 排他、全配置账本 CAS、原子发布、中断继续及 `--restore`。中断标记覆盖尚未存在的来源与数据库，运行期拒绝读取未结算迁移，不自动恢复或双读旧字段。
- active 按原 active 独立转换，文件里的未生效业务变更不被提升；来源 revision／digest 与匹配的 active baseline 同步推进。共享 Workspace 来源仍由既有 owner 接续 generation；connection_id 复用既有算法和持久身份。已有 pending／claim／恢复事实阻断迁移，历史事件不改写。重复迁移不变，产品恢复后新增配置事实会使原件恢复 CAS 明确拒绝。
- 证据仍在 `out/tests/temp/unify-product-runtime-logging/artifacts/`：最终迁移 `migration-complete-012` 50通过；此前迁移／安装／布局 `migration-final-010` 67通过，后续中断重放及共享owner接续由012补验；Gateway `gateway-final-002` 120通过，Workspace配置消费者 `config-consumers-final-004` 89通过。`typecheck-comparison-006.json` 与 `ruff-comparison-002.json` 核对受影响文件均无新增诊断，整组静态仍有既存错误，不称全绿。
- 扩大真实生命周期 `migration-mounted-runtime-001` 为10通过／4项模型请求前等待超时；完整提交前源码 `mounted-goal-baseline-001` 在其中同一项、同一等待点复现超时，其余三项未逐项跑基线。保留原始失败与期限，并在该等待点登记性能测量 TODO。4498项归档源码已逐blob核对后删除，基线正式测试产物单独保留。
- 当前9／22。日志路径策略已登记，但暂按 Gateway pending_restart 处理，避免在3.2尚未接通时虚报热生效；逐进程 prepare／apply／proof／promotion、热更新补偿与新增实例处理仍未完成。未修改Web，不新增Web构建；本切片未进行Windows／macOS产品验收。
