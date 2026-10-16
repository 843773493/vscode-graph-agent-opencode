## 1. Domain owner 与工作区级持久记录

- [x] 1.1 为 Browser、Terminal 等 Agent 资源创建工具显式提供 `retention_scope` 参数并默认设为 `thread`；支持用户或显式工具参数选择 `workspace` 创建，或将已有资源提升为工作区范围，提升前后 `resource_id` 不变。
- [x] 1.2 将工作区资源权威记录存入该工作区 `.boxteam/` 下的领域 owner 存储，并持久化不可变来源 Thread、当前关联、`unattached_since` 和回收状态；实现关联去重、资源版本和 operation lease 冲突校验。目标工作区 MUST 由显式 `workspace_id` 经 `add-multi-workspace-backend-mounting` 的已挂载工作区注册表解析，MUST NOT 依赖「当前激活工作区」。
- [x] 1.3 按已授权的旧开发数据清理策略只保留新格式 owner；物理删除旧 Node/Session 领域记录读写和历史工具输出反推入口，不新增升级、迁移、双读写或兼容逻辑。新格式同版本重启仍须保持资源身份、关联与原始孤儿期限。
- [x] 1.4 实现 backend 重启后的 owner 核实/恢复；外部资源或结果不可确认时持久暴露 unknown/reconcile_required 状态，不重复创建或盲目重放。

## 2. Thread unload、Session 删除和 owner 操作

- [x] 2.1 将 Thread runtime generation unload 和 Thread 删除接入 owner 的幂等释放事件；owner 按来源 Thread 找到全部 Thread scope 资源，释放句柄和 lease、自动 detach 并立即停止/核实/delete；Workspace scope 只移除本 Thread 关联。
- [x] 2.2 将 Session 删除接入各资源 owner：先关闭准入并收敛 Session 来源的全部 Thread scope 资源（含已 detach 的孤儿资源），再允许物理删除；保留 Workspace 记录并移除被删 Session/Thread 的引用。
- [x] 2.3 在每个 owner 实现独立的 promote、attach、detach、stop、delete 操作；Thread scope 只允许关联来源 Thread，Workspace scope 可关联同工作区有效 Thread；detach 不停止资源，带有效 lease/关联的冲突明确失败。
- [x] 2.4 删除通用 `ResourceManager` 的 `cleanup_policy`、参数猜测和进程内 stopper 决策；确保外部 stop/delete 的已完成状态只由实际领域 owner 核实。
- [x] 2.5 实现 Thread scope 零关联资源的 30 分钟孤儿回收：以最后一次 Thread detach 持久化计时，回收前复核 scope/关联/lease，并与 attach/promote 串行化；Workspace scope 无关联时不回收。

## 3. Workspace API 与完整投影

- [x] 3.1 在工作区后端提供 `/api/v1/resources` 列表/详情及 type、scope、当前关联和来源 Thread 过滤，并支持查询无 Thread 关联的 Workspace 资源与宽限期内临时孤儿。
- [x] 3.2 提供 owner-routed promote、attach、detach、stop、delete 操作；成功返回完整 owner 投影，失败返回稳定错误/阻断事实，不产生部分状态。
- [x] 3.3 更新 `SessionResourceProviderRegistry` 为只聚合和路由的 projection；验证 Gateway 只透明代理且不读写工作区 `.boxteam/`。
- [x] 3.4 收口 `available_actions` 为按已核实状态和持久关联筛选的候选用户动作：关联非空时不列 delete，实际命令仍校验 lease/关联/revision；验证 Terminal/Browser 关联期间的投影与直接 delete 拒绝零副作用、detach 后完整投影及删除成功。2026-10-15：R1884 两份真实 API 测试共 12 passed/5.44s、Ruff 通过；主核原件 `out/tests/temp/2026/10/13/fork-contract-implementation/coordinator/artifacts/resource-delete-api-original-verification-1505.json`，R1886 独审无 finding。首轮 fixture Context 失败保留，修为异步依赖后复验；本项不代表 §7 Fork 资源接入完成。

## 4. 前端工作区资源管理

- [x] 4.1 在资源类型所属的 UI 区域提供工作区范围列表，并在 30 分钟宽限期内展示无 Thread 关联的临时孤儿及计划回收时间，支持重新关联或持久化。
- [x] 4.2 在 Session 资源表面展示当前 Thread 关联和持久化快捷操作；区分资源关联 attach/detach 与浏览器/WebSocket/终端客户端传输连接。
- [x] 4.3 按后端完整结果整体替换成功对象；失败后重新读取 owner 状态并展示明确错误；Terminal 资源归主窗口底部面板。

## 5. 生命周期与恢复验证

- [x] 5.1 为默认 Thread scope 与显式 Workspace scope 创建、Thread unload 即时 detach/delete、有效 lease、promotion/unload 并发、detach 冲突、Session 删除和 owner 重启增加 owner/API 验证；覆盖孤儿资源在 29:59 重新 attach 取消回收、满 30:00 自动回收、lease 阻止回收、attach/回收竞态和 Workspace scope 永不按期限回收。
- [x] 5.2 在 `tests/e2e/clients/web/test_basic_chat_tool_loop.py` 这一既有唯一 Web E2E owner 中增加用户持久化 Browser/Terminal 资源、unload 后保留、另一个 Thread attach/detach、Session 删除后仍可从 Workspace UI 访问及明确失败反馈的完整场景。
- [x] 5.3 用外置 process control 验证 owner 在外部操作结果未知时崩溃和重启；恢复孤儿期限时不得重置计时、重放未知副作用、伪报删除成功或遗留无权威记录的外部资源。
- [x] 5.4 修复已证实的 Gateway 非 preserve 整组关闭竞态：现有 runtime owner 先等待 Workspace backend 完成 drain/unload，再终止它依赖的 Terminal/Browser 辅助服务；保留精确进程 handle 和失败结果，不把辅助服务已退出当作资源已核实。通过正式非 preserve 关闭入口与真实两类资源验证 revision 同步完成、关联按 scope 收口、无 `Application shutdown failed` 和本任务遗留进程；restart-safe 成功不代此项。
- [x] 5.5 修复已删除 Browser 的迟到传输授权请求：owner 对已核实非 running 资源在网络前明确拒绝，保留确定终态；目标端状态或 revision 竞争提供明确的拒绝合同，不以普通 HTTP 500 把确定拒绝改写成 unknown。验证 owner 预检与真实目标端竞争入口、失败零 grant 副作用及终态不污染；真实结果未知仍显式阻断，不吞异常或假报成功。

1.4/5.3 验收（2026-10-06）：`tests/integration/workspace_services/test_resource_owner_crash_recovery.py` 使用真实 Node 资源、外置 SIGKILL 与新 Python registry 重开；Thread/Workspace × Terminal/Browser × 已发生/未发生共 8 个 stop 场景通过，崩溃后且恢复前直接核持久 owner/lease 与 Node 同一身份；未知操作保持阻断且不重放，Workspace 资源在 Session 释放后保留。原始孤儿期限、29:59 重新关联与 30:00 回收的 4 场景未变，复用既有通过结果，不声称最终单轮 12 绿。固定测试 SHA `fdab7b1d49e50ce60e632d9e64ae96addff2587b389631879e512a8bd190538f`；8 stop 实际退出 0 / 344.113 秒，生产/支撑指纹未变。证据 `out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/sol-efficiency-trial/persistent-crash-primary/post-sigkill-authority-attempt-1/`，独审 `wire_http_acceptance_reviewer/artifacts/persistent-crash-boundary/`。owner/ledger 已提交；fixture 的 storage 初始化确为间接依赖；schema_plans DDL 移除 schema3_import 许可、旧 schema 拒绝收紧，本测试 fresh acceptance 不写 plan 导入记录，差异不影响此输入。基础 DDL 入口与版本一致不等于其全部导入 SQL 同 blob，不以直接 owner blob 相同宣称全部依赖相同。5.2 完整 Web 验收见下方独立收据。

5.2 验收（2026-10-06）：固定候选 `f29222ec3dd8953edab997abc72e11f30f39f1a5`；真实模型 Job、PTY、Chromium 与正式 Gateway `restart-safe`，完整 pytest 1 passed / 101.14 秒，外部保护 runner 实际退出 0 / 102.798 秒；112 个现存运行依赖前后未变并匹配候选，原 Web 用例不变。用户 promote、真实 Thread unload 后同 ID 保留、另一 Thread attach/detach、Session committed 删除后工作区 UI 访问、409 明确反馈与 owner 重取均通过。`recover()` 不清关联；旧后端 lifespan 经 tracker unload listener 调用 `release_thread` 才移除关联。证据 `out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/sol-efficiency-trial/persistent-web-primary/attempt-16/`，独审 `persistent_web_closure_reviewer/artifacts/report.md`（SHA `f00cea9017fc99a60fd87d93f88a7bf7025e6ff5355df15461bf689fad9376d9`）。本场景覆盖进程关闭型 unload，不外推 30 分钟 idle unload；本项不覆盖非 preserve 整组关闭及已删除 Browser 迟到 grant；两处缺陷的后续独立修复与验收见 5.4/5.5，不将本 Web 场景外推为全部关闭安全。

5.4/5.5 验收（2026-10-07，北京时间）：实现已集成 `1b6305d7`，最终17路径tree `340dcbd74a9051584a20b730d8f90525e84800ca`（Sol十一生产、主六测试）。Linux同组回收在 `process_groups.py` 核精确PGID/session、leader starttime及非zombie活成员；根退出不等于整组退出，默认8秒不增，强制回收继续保原失败和handle，重试不洗掉unload失败。主7项unit含Managed/Adopted及root已退/child未登记的清理故障窗口，actual0/5.659秒；另48相关caller在生产等字节627f通过actual0/11.881秒。真实非preserve Gateway/backend/Job/residency/PTY/Chromium关闭1pass/actual0/35.707秒，三个登记服务PGID的活成员全部退出，四资源按Thread/Workspace scope收口、owner/Node revision一致且无shutdown failed；模型是TCP Responses cassette，不称完整模型E2E。上述1458–1461依赖匹配固定候选且前后未变；最终变化仅unit测试，复用生产与真实关闭收据。

5.5的Node15、client17、真实目标端grant HTTP竞争2场景及ROOT35定向检查均按未变路径/依赖复用；停止目标/推进revision后确定拒绝无token，owner保verified、revision不倒退、lease收口。P08报告SHA `0aec4a80ede270d38c3eaca2c38efb51650a50390a16c9af650b3e10c634d063`；P07最终生产/测试增量报告SHA `4f6fd4accdc28b718264d1bbfd9737101f9bba63ba6c1596a18342c632ad1599`，保留旧627f清理P2及新340dc闭合事实。17路径strict重建最终tree、guard与双祖先均通过，共享index精确SHA不变。证据 `out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/sol-efficiency-trial/persistent-shutdown-grant-focused-dispatch/` 的final-group-real-shutdown、final-group-caller-unit、final-probe-cleanup-unit、mechanical-delivery-final-unit-attempt-2及ROOT-integration-final收据。各批重叠不相加，不称整仓覆盖；Windows/其它POSIX未行为验。首次Chat回放在关闭前的canonical typed provenance冲突仍归后续R producer待验，Responses通过不证明Chat已修。

## 6. 持久化引用收紧为规范层义务（只新增约束与引用）

- [x] 6.1 落成规范层禁止项：工作区持久记录、API 响应体与模型可见载荷只承载 `资源身份 / ResourceIdentity` + `资源 URI`（必要时并列 revision 字段），MUST NOT 承载 `真实路径 / real path`；检出即 fail-closed。`作用域 / scope` 闭集、`scope_id` 取值语义、URI 语法、kind 闭集与 `拒绝码 / rejection code` 一律具名引用 `add-unified-virtual-resource-addressing`，本 change MUST NOT 复述或自造。
- [x] 6.2 消除已确证的 real path 持久化：终端记录去掉真实绝对 `cwd` 与顶层 `workspace_root`；终端工作目录作为 owner 自身运行态字段按规范化边界改存工作区内相对目录（`cwd_relative`）并在 PTY 启动调用栈内由工作区根重推导（`terminalSession.js`、`terminalManager.js`），不为此自造 URI kind；旧开发格式按已授权清理策略下线，生产不提供隐式升级或兼容读取。浏览器记录去掉 checkpoint 真实文件路径（`browserSession.js`、`browserStateStore.js`）与下载真实文件路径（`browserStateStore.js`），位置可由 owner 身份确定性推导者 MUST NOT 落盘（裁定 D-A2），产物路径改由 `(browser_id, download_id, filename)` 在调用栈内重推导。除 owner 自身运行态字段外，「凡记录需要表达资源所在位置，该位置 MUST 以 URI 表达」的义务不削弱。 **（证据（2026-10-01 第四轮复核补登）：实现提交 `7afb3b65`（父 `f6070abd`）与 `c2f6c844`（关闭终端与截图端点仍泄漏的 real path）；负向断言 `tests/unit/services/business/test_session_resource_service.py::test_terminal_resource_projection_excludes_real_path`、`::test_browser_resource_projection_excludes_checkpoint_real_path`，另 `tests/unit/agents/tools/test_terminal_tool.py`、`tests/integration/workspace_services/browser/test_browser_custom_tools.py`。）**
- [x] 6.3 消除 real path 上浮：`session_resource_mapper.py` 的 `cwd`/`checkpoint` metadata 分别收窄为不含路径的 `cwd_relative` 与显式布尔 `checkpoint_available`；`app/agents/tools/terminal.py` 的 `exec_command` 工具结果与 `/api/terminals` 快照的 `cwd` 改为工作区内相对表达 `cwd_relative`，不得出现绝对路径；截图 `image_path` 改为 `screenshot_id` + `screenshot_url`（既有浏览器管理器只读端点，裁定 D-A3），且该端点对 id 不存在、browser 不存在、文件缺失返回稳定结构化错误、不回吐真实路径；新增负向断言「持久记录、API 响应体与模型可见载荷不含 real path」。 **（证据（2026-10-01 第四轮复核补登）：实现提交 `7afb3b65` 与 `c2f6c844`；`cwd_relative` 见 `app/agents/tools/terminal.py:356/389` 与 `app/services/mapping/session_resource_mapper.py:50`，`checkpoint_available` 见同文件 `:138`，`screenshot_id` 见 `src/workspace-services/browser/server/browserStateStore.js:249`。截图只读端点形态与 `app/agents/tools/browser.py:540` 的 `screenshot_url` 措辞须在实施期核对一致（本轮未运行时验证）。）**

### 6.4 越限项登记（判据式，含二级子目录）

判据沿用仓库既有门槛：目标目录 MUST 满足「直接源码文件 ≤20」「单文件 ≤800 行」「不与两个以上领域职责混合」，任一越界即 MUST 附拆分方案或含文件清单、职责/owner 映射、import graph、行数统计与复核结论的架构审查证据。豁免：生成目录（`src/workspace-services/protocol/generated/**`）由上层 `generated/AGENTS.md` 声明不可手改，不计越限。

- 实测快照（提交 `7fe347dc`）：**本 change 归口的单文件 >800 行**：`src/workspace-services/browser/server/browserSession.js=2515`、`src/workspace-services/browser/client/main.js=1814`、`src/workspace-services/browser/server/backend.js=928`（本 change 6.2 已具名 `browserSession.js`/`browserStateStore.js` 并落地其 real path 收紧，三者均须在实施期按浏览器会话生命周期 / 客户端事件与传输装配 / server 协议与生命周期编排职责拆分）。
- **本 change 归口的目录直接 `.js` >20（含测试）**：`src/workspace-services/browser/server=23`，其中13个生产模块、10个测试模块；生产文件单独计数未越20，全部代码文件计数触发结构审查。下沉按真实职责与owner决定，不能为了数字搬移文件。历史同一范围内 `src/workspace-services/terminal/server=12`、`src/workspace-services/browser/client=11` 等其余目录均未越限。

- 当前固定 `c58b6cab` 复核：三个生产文件分别为2586/1860/963行，测试`browserSessionOperations.test.js`为1014行；53路径/46JS完整清单、import graph、owner映射与职责拆分方向见 `out/tests/temp/2026/10/04/024121-team-execution/coordinator/artifacts/sol-efficiency-trial/persistent-browser-architecture-review/attempt-1/report.md`、`inventory.json`。主核这五个目标文件至ROOT973098无差异。BrowserManager/BrowserSession继续拥有唯一生命周期；后续独立结构切片按session操作族、backend HTTP/WS、client view/connection拆分并全迁caller，纯workspace校验解除client/server对BrowserManager的反向依赖。只读审查证据完成，不表示结构实现已完成，不插当前R生产切片。

- 上述登记只补录越限事实与归口，**本 change 不拆这些文件**；拆分与相应架构审查证据属后续独立实施。

## 7. Fork目标资源关联（2026-10-13，待实施）

- [ ] 7.1 向统一目标检查提供现有owner scope/关联/revision/状态和真实操作能力；显式复用只调用owner，默认fork不附加Terminal/Browser，不保存第二资源状态或实例fork布尔字段。
- [ ] 7.2 验证workspace资源身份/实例复用和source删除后目标关联、thread范围不自动promote、lease/revision冲突、不可移植跨端资源明确失败，操作结果和恢复仍由owner核实。
