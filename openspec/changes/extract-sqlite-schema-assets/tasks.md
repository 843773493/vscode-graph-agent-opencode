## 1. 盘点与统一加载

- [x] 1.1 盘点生产 DDL、迁移 SQL、动态标识符与所有直接/间接消费者，保存各 owner 的原文/结构基线。
- [x] 1.2 用标准包资源加载 SQL；缺失时明确失败，迁移框架账本建表也使用 SQL，保持事务和回滚语义。

## 2. Gateway

- [x] 2.1 提取 gateway_state 的完整有序迁移，保留 Python 数据转换回调及原版本位置，验证新库、逐版本升级、已有数据和冷重开。
- [x] 2.2 提取 federation store 的结构并验证 replay/channel/route 行为。

## 3. Session catalog 与 control

- [x] 3.1 提取 session_catalog_store 与 navigation queue 的表/索引/触发器及迁移，验证导航、创建/删除与修复拒绝。
- [x] 3.2 提取 thread catalog、owner binding、operation lease 的静态结构和迁移，验证现有 owner 及版本。
- [x] 3.3 提取 session_control_store 的 execution intent、thread creation、background/mailbox、collaboration/fanout、copy settlement 与 retention release 结构。
- [x] 3.4 提取 communication ledger 的表/索引和 observations 结构，验证消息及页账本。

## 4. 工作区与 rollout

- [x] 4.1 提取 workspace state/config source、workspace files operation 的结构与迁移，验证配置和恢复。
- [x] 4.2 提取 attachment blob catalog 的结构与迁移，验证 claim/owner/ref。
- [x] 4.3 提取 rollout storage 的 schema、plan、assembly refs、wakeup 与 activation 的结构/迁移 SQL，保持连接内执行顺序及条件分支。
- [x] 4.4 提取 fork snapshot 的静态索引迁移语句与 copy_items 的临时表结构，验证真实 fork owner 与历史保真。

## 5. 交付检查

- [x] 5.1 从真实发行资源收集目录加载 SQL 并初始化/重开代表数据库；缺失资产明确失败。
- [x] 5.2 全仓复核生产 Python 不残留静态 DDL/迁移文本，核对动态 SQL 原因、无重复来源；相关静态、定向集成、严格 OpenSpec 与独立审查通过，按 owner 提交并 push。

2026-10-21 首批：AST盘点保存29个生产owner的147个候选DDL字符串（含fork临时表；1.1的全消费者闭包尚待后续owner复核），原件位于 `out/tests/temp/openspec_takeover/artifacts/sqlite-ddl-baseline-001/` 与 `sqlite-ddl-inventory-001.json`。核心迁移账本、Gateway状态与联邦库已提取，清理SQL尾部空白后，展开load_sql的执行AST与原文仅此空白差异，见 `sqlite-gateway-equivalence-002.json`。`sqlite-assets-gateway-unit-001` 95 passed/3.28s，覆盖核心迁移错误/锁/账本与Gateway/federation行为；全17版本升级、原有行/账本时间保持与冷重开均通过。新增测试静态修正后 `sqlite-assets-gateway-final-002` 18 passed，含真实发行copy→隔离Python加载→建库/重开及删除资产明确失败，未修改产品源码。最终 `sqlite-assets-gateway-static-002` 对本批6文件Ruff exit0；首次包级检查的3条既有TRY004/PYI034位于与HEAD逐字相同的未修改文件，原件另存。非作者只读独审无阻断；准确命令、源码指纹、raw、实际退出码见同目录runs及 `sqlite-assets-gateway-validation-001.json`。本批不代表剩余Session/rollout/工作区owner已迁移，也不声称Windows完整安装包实机验收。

提交前diff检查发现原字符串的尾部缩进进入SQL资产；已清理尾部空白并统一末尾换行，其余执行AST等价。`sqlite-assets-gateway-final-003` 最终96 passed，源码前后指纹一致；最终strict `openspec-002` exit0。首次私有提交在更新分支前被diff-check拒绝，不计交付。

2026-10-21 Session批：16个Python模块的54份结构/种子SQL已迁出，保留逐条execute、事务和版本/旧库拒绝。catalog原8段指纹表达式索引冻结为与原生成结果相同的静态SQL；其它模块展开load_sql后执行AST相同，仅尾部空白与import顺序归一，见 `sqlite-session-equivalence-001.json`；147处直接/间接结构消费者检索保留。`sqlite-assets-session-core-001` 577 passed/14.16s；导航组001为31 passed、1 failed/19.79s，唯一崩溃子进程沿旧路径未绑定catalog owner，失败在领取操作前，不是DDL错误。该测试迁到现有fixture_catalog_scope，不改退出91、running、generation/fencing与同一operation恢复断言，`navigation-recovery-002` 1 passed/4.57s。本批16文件Ruff `static-001` 及fixture静态002 exit0；发行copy扩展到Session/catalog/control/navigation后 `release-002` 1 passed/5.13s，经历新建/冷重开；release001为新增测试误把无context协议的store当context manager，仅改用closing，不改产品。原命令、指纹、raw及退出码见 `out/tests/temp/openspec_takeover/artifacts/runs/sqlite-assets-session-*`，限定收据为 `sqlite-assets-session-validation-001.json`。这是存储/真实崩溃恢复验证，不声称真实HTTP或完整Agent执行。

2026-10-21 工作区/附件批：4个生产owner的48份结构/迁移/种子SQL已迁出，版本仍为Workspace14、文件operation3、附件3。源码与原文展开比对执行AST等价，source owner的事务仍由Python拼接协调且executescript方式不变；19份附件表/索引/触发器与1个种子共20份资产保留原顺序。新增source_path→uri旧库迁移冷重开与中途DROP拒绝验证journal/fanout保留及结构/数据回滚。`sqlite-assets-workspace-unit-001` 124 passed/46.12s，含既有配置/文件恢复/附件失败及扩展后的发行copy smoke；Ruff `static-002`、compile001 exit0。static001仅新增import顺序已修；另一个PYI034为与HEAD同AST的原有__enter__注解，限定检查显式忽略该既存项，证据 `sqlite-workspace-static-baseline-001.json`。独立只读审查无结构/事务阻断。release子进程继承run_task的RLIMIT_DATA，整个进程组有外部timeout，不是裸跑。基础设施全目录扩展检查 `infrastructure-001` 以SIGINT在硬期限前中止，exit2；1135 passed/9 setup errors/232.53s。九项fork用例均在fork_deadline fixture因要求正式matrix runner而拒绝，未进入业务；这是本次扩展入口选择问题，不计SQL失败，剩余目录未完成，不重复裸跑；不以该124项声称整个目录全绿。原件位于 `out/tests/temp/openspec_takeover/artifacts/sqlite-workspace-*` 及runs。

2026-10-21 rollout/fork收口：7个Python owner提取28份SQL，schema仍19、activation仍4；plan/tool-set的IF NOT EXISTS冻结为原唯一消费者替换结果，12份TEMP表和fork索引的execute/事务顺序不变。18→19迁移完整10000字节（无末尾LF），SHA256仍为1587d2803908f9437c986927bc0c3a10f53cdf757f321631f295d7ac5e284b75，保留显式迁移与checksum消费者。最终AST盘点29个原owner、153份生产SQL，运行期Python无剩余静态DDL，原件sqlite-ddl-inventory-final-001.json；仅说明文字不算DDL。展开资产的执行AST等价，equivalence-002 exit0。正式matrix unit-002为536 passed/219.20s；integration-001为120 passed/144.06s，覆盖assembly迁移、seal事务、真实fork首provider、portable配置及源不可用后深副本。unit-001在无关harness缺失import处collection失败，002只ignore该harness目录，未更改测试预期。发行copy→隔离Python创建/冷重开增加rollout/activation，release-001 1 passed/5.02s；最终static-002、compile-001 exit0。所有命令经外部deadline/内存保护，raw、退出码、源码指纹保存在out/tests/temp/openspec_takeover/artifacts/runs/sqlite-assets-rollout-*；并发无关源码漂移单独登记，不等同于本批owner变化。独立审查无结构/事务/迁移字节阻断。本变更纯SQL抽离，不提升数据库版本、不宣称完整Web或Windows安装实机验收。
