## 1. 盘点与统一加载

- [ ] 1.1 盘点生产 DDL、迁移 SQL、动态标识符与所有直接/间接消费者，保存各 owner 的原文/结构基线。
- [x] 1.2 用标准包资源加载 SQL；缺失时明确失败，迁移框架账本建表也使用 SQL，保持事务和回滚语义。

## 2. Gateway

- [x] 2.1 提取 gateway_state 的完整有序迁移，保留 Python 数据转换回调及原版本位置，验证新库、逐版本升级、已有数据和冷重开。
- [x] 2.2 提取 federation store 的结构并验证 replay/channel/route 行为。

## 3. Session catalog 与 control

- [ ] 3.1 提取 session_catalog_store 与 navigation queue 的表/索引/触发器及迁移，验证导航、创建/删除与修复拒绝。
- [ ] 3.2 提取 thread catalog、owner binding、operation lease 的静态结构和迁移，验证现有 owner 及版本。
- [ ] 3.3 提取 session_control_store 的 execution intent、thread creation、background/mailbox、collaboration/fanout、copy settlement 与 retention release 结构。
- [ ] 3.4 提取 communication ledger 的表/索引和 observations 结构，验证消息及页账本。

## 4. 工作区与 rollout

- [ ] 4.1 提取 workspace state/config source、workspace files operation 的结构与迁移，验证配置和恢复。
- [ ] 4.2 提取 attachment blob catalog 的结构与迁移，验证 claim/owner/ref。
- [ ] 4.3 提取 rollout storage 的 schema、plan、assembly refs、wakeup 与 activation 的结构/迁移 SQL，保持连接内执行顺序及条件分支。
- [ ] 4.4 提取 fork snapshot 的静态索引迁移语句与 copy_items 的临时表结构，验证真实 fork owner 与历史保真。

## 5. 交付检查

- [x] 5.1 从真实发行资源收集目录加载 SQL 并初始化/重开代表数据库；缺失资产明确失败。
- [ ] 5.2 全仓复核生产 Python 不残留静态 DDL/迁移文本，核对动态 SQL 原因、无重复来源；相关静态、定向集成、严格 OpenSpec 与独立审查通过，按 owner 提交并 push。

2026-10-21 首批：AST盘点保存29个生产owner的147个候选DDL字符串（含fork临时表；1.1的全消费者闭包尚待后续owner复核），原件位于 `out/tests/temp/openspec_takeover/artifacts/sqlite-ddl-baseline-001/` 与 `sqlite-ddl-inventory-001.json`。核心迁移账本、Gateway状态与联邦库已提取，清理SQL尾部空白后，展开load_sql的执行AST与原文仅此空白差异，见 `sqlite-gateway-equivalence-002.json`。`sqlite-assets-gateway-unit-001` 95 passed/3.28s，覆盖核心迁移错误/锁/账本与Gateway/federation行为；全17版本升级、原有行/账本时间保持与冷重开均通过。新增测试静态修正后 `sqlite-assets-gateway-final-002` 18 passed，含真实发行copy→隔离Python加载→建库/重开及删除资产明确失败，未修改产品源码。最终 `sqlite-assets-gateway-static-002` 对本批6文件Ruff exit0；首次包级检查的3条既有TRY004/PYI034位于与HEAD逐字相同的未修改文件，原件另存。非作者只读独审无阻断；准确命令、源码指纹、raw、实际退出码见同目录runs及 `sqlite-assets-gateway-validation-001.json`。本批不代表剩余Session/rollout/工作区owner已迁移，也不声称Windows完整安装包实机验收。

提交前diff检查发现原字符串的尾部缩进进入SQL资产；已清理尾部空白并统一末尾换行，其余执行AST等价。`sqlite-assets-gateway-final-003` 最终96 passed，源码前后指纹一致；最终strict `openspec-002` exit0。首次私有提交在更新分支前被diff-check拒绝，不计交付。
