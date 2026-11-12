# OpenSpec 实施性能审查

按需检查当前实施的性能与模糊约定，并维护本文件，不要求定时检查。时间使用北京时间；仅写有源码或原始测量依据的条目，不能把疑点当作已测瓶颈。

## 运行约定

- 只读源码、活跃 OpenSpec 和既有测量；本文件可追加／更新。同一问题按编号更新证据，已解决项保留闭环，不重复复制全文。
- 重点看普通请求中重复 schema／全库完整性检查、全历史 hash／materialize、目录全扫描、重复初始化、每层重新分配完整 timeout，以及性能验收只有 helper 计数没有总耗时的位置。
- 任何新完整性检查必须说明由谁校验、何时触发、数据版本如何失效；普通读取仍保留 owner、鉴权、CAS 和实际数据检查，不能用吞错或旧缓存提速。
- 定向验证使用现有矩阵或进程外超时与内存保护；不默认全仓测试，不启动／重启开发服务，不更改环境，不自动修改产品代码，不提交或覆盖他人的工作。
- 记录时间、源码版本／位置、实际证据或待证疑点、用户影响、最小验证及建议。无新发现时只更新“最近检查”，不发无意义状态通知。

## 最近检查

2026-11-05 13:29（北京时间）：相关配置／导入链路减法后，复用原场景与预算完成一次真实浏览器补验，首次Composer820.52ms、六次均达标，目录与文件及DOM终检通过。旧5.22秒失败原件保留，不将结果归因于单个提交。

## 问题记录

### PERF-001：验证回执计数通过，不等于普通请求快

- 状态：已确认验收缺口；尚未证明具体请求慢。
- 位置：`tests/integration/backend/sessions/test_rollout_storage_benchmark.py::test_warm_initialization_measures_body_validation_receipts`。
- 证据：该用例比较 canonical body 检查次数和初始化时间，没有Gateway/HTTP/Workbench完整请求时间。`session-turn-history`虽有100–200ms reader预算，但不覆盖其它层叠加。
- 处理：补独立真实导航／会话／文件打开总预算，原回执测试保留，并注明证明范围。下一项有价值的扩展是128Turn历史经Gateway的冷／暖读取，不能把空会话用例当作该项已通过。页面可能预取目录，`firstReadMs`只代表数据准备后的首次显式读取，不能声称证明存储冷缓存。

### PERF-002：进程300秒和页面等待30秒容易被误当交互预算

- 状态：已确认验收缺口。
- 位置：`tests/runner/matrix.jsonc`的Web套件及`tests/e2e/clients/web/session_information.mjs`等页面等待。
- 证据：进程保护包含构建和清理；`waitFor`成功只能证明期限内出现，不能证明普通操作在1–2秒内完成。
- 处理：新套件独立断言API p95≤500ms／单次≤1500ms、导航总p95≤1000ms／单次≤2000ms、会话和文件正文可用≤2000ms，慢样本不删、不重试取最好结果。

### PERF-003：全量审计必须由正确边界执行，不能一律删校验

- 状态：静态归属提示，不是已测热点。
- 位置：`app/services/infrastructure/rollout_context/storage/rollout_index_reads.py`、`rollout_schema_introspection.py`、`rollout_offline_restore.py`。
- 证据：代码存在显式`validate_integrity`和离线恢复的`PRAGMA integrity_check`；这些检查的存在本身不证明普通请求执行了它们。schema/正文验证路径还需通过真实请求和原trace判断调用频率，不能仅搜到函数就下结论。
- 处理：OpenSpec明确无变化暖读不无条件全库审计；原迁移／恢复／损坏拒绝合同保留。没有原始请求证据，不擅自改这些生产链路或增加另一套监控。

### PERF-004：会话恢复不再等待无关工作区健康探测

- 状态：已通过受控无响应目标的真实页面对照；未将其推广为真实远端故障或所有多目标场景已通过。
- 位置：`src/clients/workbench/src/react/chatView.tsx::initialize` 改用 `gateway.workspaces(false)` 取得登记归属；原目标校验、实际 Session／Thread bootstrap 和显式全目标健康检查保留。
- 根因：原初始化 Promise.all 默认探测所有登记工作区。A 已能提供正确会话时，仍等待无关 B 的 HTTP 超时。沿项目既有 `resolveWorkspaceTarget` 的登记读取边界消除该依赖，没有新增健康缓存；参考仓库原 ChatEditorInput 按明确 session resource 调用 `acquireOrLoadSession`，也不把打开单会话扩展为所有目标预检。
- 对照：`integration-web-thread-restore-health` 使用正式 Workbench、真实 Gateway／A 后端，B 仅在外部健康 HTTP 边界保持无响应。旧实现 `run-cc4c3836e55e` 打开耗时2570.53ms，确有 `check_health=true` 请求，超过原2000ms预算；改后 `run-e5670d78c7b5` 耗时732.85ms，恢复期间登记请求均为 false。两次均核精确 Session／Thread、bootstrap200及原request_id，未调用模型。
- 故障透明：修复后同一页面显式 `check_health=true` 耗时2118.27ms，B 仍显示 offline 和原 ReadTimeout，A 输入框继续可编辑。没有用登记路由的存在代替当前业务请求成功，也没有删除探测或吞掉故障。
- 范围：各一次未筛选样本，只证明该等待依赖被移除，不是20次暖读统计、真实远端断网、深历史或 Electron 验收。原始命令、源码指纹、构建、页面结果及旧失败保留在同名正式测试产物和 `out/tests/temp/frozen_wire_20261105/artifacts/thread-restore-{before,after}-001-*.json`。

### PERF-005：只读 Agent 列表重复构建模型对象已移除

- 状态：原调用链已收口；配置错误和实际执行时的 SDK 错误仍分别暴露。
- 位置：`app/agents/agent_factory.py::provider_configuration_error`、`app/services/business/agent_service.py::AgentService.list`。
- 当前证据：列表只核本地配置和必要凭据；`build_model_from_provider` 留在实际执行，不再由列表导入／构造 SDK 模型。无变化配置复用已有校验事实，不缓存可变业务成功状态。
- 真实补验：`run-1bd5f356f776` 首次Composer820.52ms，仍包含首次初始化；这是近期相关改动的联合结果，不证明它单独贡献全部差值。

### PERF-006：网络字节指标不能混用解码长度与实际传输量

- 状态：已确认测试观测问题并修正，不是产品丢失正文的证据。
- 位置：`tests/e2e/clients/web/interactive_latency.mjs` 的 Chromium `Network.dataReceived` 观察。
- 证据：独立本机 Chromium 探针以 Blob 消费131089字节正文时，事件为 `dataLength=0`、`encodedDataLength=131089`，`request.sizes().responseBodySize=131089`。直接用 `dataLength` 会把已传输正文误判为零；早先 Playwright正文提取为零的现象未单独归因，不能由它断言服务没有传数据。
- 处理：最终文件场景按精确 `request_id` 关联真实200响应、`encodedDataLength`及网络完成事件，并核Monaco正文可见与128KiB传输量；不只相信Content-Length。探针只验证观测方法，不能冒充产品E2E或替代产品失败记录。

### PERF-007：首次会话打开超预算已在代表性场景补验闭环

- 状态：原失败保留；相关减法后同一代表性场景通过，未放宽2000ms预算。
- 源码／原件：`run-472d73bce50c` 的 `source.json`、`latency.json`、`gateway/gateway.stderr.log` 和 `browser.stderr.raw`；源码HEAD为 `723db7a950e4c8044857bb90e3e73e6ba6e36c5b`，工作树关键文件及正式构建另有SHA收据。
- 证据：两个空历史Session交替打开六次，耗时依次为5217.70、911.03、877.39、871.25、915.35、886.67ms；每次已核精确Session／Thread、bootstrap200及request_id、正确Composer可编辑。首开计入全部初始化，没有把它排除成“启动”。首次Workbench静态启动4888.99ms另列。
- 请求关联：首个bootstrap为 `03bdf46b-35ac-40e6-bad2-befa82020bed`；先于它返回的Agent列表为 `c7da1436-675d-41d9-a201-cccaa5412d53`。现有Gateway访问日志不能独自分解这5.22秒，不能直接宣称PERF-004或005是唯一原因。
- 处理与补验：移除只读Agent列表的SDK构建、目录查询的执行服务导入及未变化配置资源的重复校验后，沿原正式矩阵补验一次。`run-1bd5f356f776` 首次820.52ms，六次640.41–825.29ms；未预热、未剔除首次样本、未改预算。当前证据不能分解每个提交的贡献，也不代表多目标离线或深历史已通过。

## 本次验收

正式命令：`bun run test:matrix -- --suite=e2e-web-interactive-latency`，矩阵300秒／4GiB进程外保护。`run-1bd5f356f776` 退出码0，1 passed／92.28秒，包含构建、启动和清理，不把该时间作为单次交互耗时。正式源码HEAD为 `8c42d5464b727ce6997765f1ce7f01c7f79629c1`，产品测试源码首尾SHA一致。首次静态工作台加载5271.75ms单列，数据准备后的首次显式目录读取44.70ms；不能声称是真正存储冷读。

| 场景 | 原样本 | 实际结果 | 判定 |
|---|---:|---|---|
| 三步导航组合 | 20 | p95 288.68ms，max 318.18ms | 通过 |
| 工作区列表API | 20 | p95 191.90ms，max 207.50ms | 通过 |
| 当前工作区API | 20 | p95 44.30ms，max 46.00ms | 通过 |
| 会话目录API | 20 | p95 41.60ms，max 42.40ms | 通过 |
| 原Monaco打开128KiB文件 | 6 | 289.71–632.78ms，六次真实200及完整正文传输 | 通过 |
| 正确会话Composer可操作 | 6 | 首次820.52ms，全部640.41–825.29ms | 通过 |

正式Workbench构建退出码0；页面错误及禁止执行请求为空，DOM终检1134节点通过。旧 `run-472d73bce50c` 的5217.70ms失败及早期接线／字节观测失败原件均保留；没有相关变化前不重复取样，相关减法后只做本次补验。未验证128Turn深历史、真正存储冷读、多工作区不可达、Electron及Windows。

## 证据入口

- 规范：`openspec/changes/enforce-interactive-latency-budgets/`。
- 定向命令：`bun run test:matrix -- --suite=e2e-web-interactive-latency`。
- 最新正式原样本／浏览器输出／源码与构建收据：`out/tests/e2e/clients/web/test_interactive_latency/runs/run-1bd5f356f776/nodes/node-540209378b75/artifacts/`。
- 本次准确命令、源码首尾指纹及实际退出码：`out/tests/temp/interactive_latency_current_20261105/artifacts/product-1793856490071999727.json`。
- 旧性能失败：`out/tests/e2e/clients/web/test_interactive_latency/runs/run-472d73bce50c/nodes/node-540209378b75/artifacts/`。
- 旧失败原件：同一测试目录的 `run-e47581e88d8f`、`run-bef5f3dc329c`、`run-69b1567b06e4`，未覆盖或删除。
- 最终静态检查命令、输出、退出码与指纹：`out/tests/temp/interactive-latency/artifacts/delivery-static-i13s45a3/`。
- 观测方法小探针原始输出：`out/tests/temp/interactive-latency/artifacts/network-probe.log`。临时探针源码已删除，正式测试产物保留。
