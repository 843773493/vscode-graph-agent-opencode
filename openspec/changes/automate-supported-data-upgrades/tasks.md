## 1. 自动升级单链路

- [x] 1.1 明确已有支持来源、未知版本／数据取舍边界及原 owner 归属；修正规范与维护指引。
- [x] 1.2 正常启动只读识别升级，沿原 Launcher 停止入口停写，再接通配置／身份的原备份迁移与恢复；当前格式不重写。
- [x] 1.3 SQLite 原有有序链在锁内识别版本，备份 WAL、原子迁移及校验；迁齐共享／离线消费者。
- [x] 1.4 明确新增任意格式转换统一放在业务 owner 的 `migrations/` 下，采用两端版本和格式命名；同步 proposal／design／spec，区分数据合同版本、软件版本与数据库 schema，保留既有 SQL 名称和显式执行顺序。本项为规范补充，不表示已实现未知来源导入，也不触发已有模块批量重命名。

## 2. 验收与交付

- [x] 2.1 定向覆盖未知／冲突保全、正向续跑、回退方向、写入占用、SQLite 失败回滚、当前重入及受支持配置经真实 Gateway／Workspace 启动和重启。
  - Linux 支持链集成：`test_runtime_logging_startup.py` 2 passed；旧 Trace 快照独立 1 skipped。原输出：`out/tests/temp/2026/11/14/automatic_supported_upgrade/artifacts/supported-startup-integration-04.log`。配置／SQL 定向回归、Launcher 31项及资源账本68项另有原始证据，不与重叠分组累加。
  - 最终身份预检回归126项通过，涵盖未结算运行代／活动引用及远端凭据缺失在配置与布局首写前拒绝；原输出：同目录 `identity-preflight-final-01.log`。
- [x] 2.2 冻结候选后在 hyf_win 隔离执行同一升级链，保留命令、版本、原输出及未完成范围。
  - Windows 候选 `dc38827e8b716e454e66f646d9aa205f6e2fac33`：配置／SQL启动预检66项通过，原SQLite基础14项通过；按新锁完成bootstrap后，真实Gateway／Workspace自动升级和两次启动1 passed／49.55秒。原输出：`out/tests/temp/2026/11/14/windows_distribution_closure/artifacts/windows-upgrade-{unit-final,integration-after-bootstrap}.log`；前轮GBK测试fixture和未同步依赖的失败原件保留，未降低保护或放宽断言。
  - 目标快照 `9419e52bf2b4c45d81bfc1805e36fd6f8419723f` 与候选树一致；原命令、实际锁元数据及退出核对见同目录 `final-receipt.json`。Windows未另跑精确空资源账本单文件，保留Linux验证范围；全量产物收集因内存压力中止，仅清理本次collector及临时ZIP，原测试日志已在宿主保存，未以归档完成替代验收。
- [x] 2.3 完成最小充分实现复审、静态分析和精确本地提交；不自动同步 dev/release/main。
  - 候选 `20c86faf4ee33041cfff826eb4ff436950609c25` 已本地提交；Ruff、Node语法、差异与OpenSpec严格校验通过，复用Sol只读复审确认首写顺序已闭合。未push或同步固定dev/release/main。
  - Windows编码收口另提交 `bddf1713b`、`353a4b9ef`、`dc38827e8`；Launcher子进程输出明确UTF-8，测试静态资产及journal显式UTF-8，不改变目标全局locale或静态文件的读取合同。
  - 2026-11-19 当前源码复验：配置／身份／SQLite 定向回归149项、Launcher回归31项通过；真实受支持来源升级与两次Gateway／Workspace启动1项通过（47.27秒），核精确原件备份、工作区身份和重入不重写。已同步 configuration-evolution／workspace-identity 主规范，未改产品代码。命令、源码指纹、退出码及正式业务原件见 `out/tests/temp/2026/11/19/upgrade_contract_sync/artifacts/verification.json`。本轮未重跑Windows，2026-11-14 Windows原件只证明其冻结候选；下述2.4仍未完成，不能以这些支持链结果替代。
- [x] 2.4 按用户最新授权，以固定来源 bf0f2e1d9d037efcb0210c458fa157fb4d54a27a 的完整旧源码经真实消息入口生成历史；由当前 initialize 升级 catalog／control／rollout／Trace，再经真实 Gateway／Workspace 两次启动核原身份、正文、item／请求 hash、备份和重入不重写。无法补齐新事实的旧 Trace 事件明确丢弃并报告；不新增历史兼容协议。本轮完成 Linux 验收；既有 Windows 支持链结果不代替本次新增历史链验收。
  - 2026-11-20 固定旧源码真实消息入口 → 当前 initialize → 两次真实 Gateway／Workspace 启动：1 passed／109.22秒。旧 Trace 原字节及创建 preimage 备份可核验，目标丢弃 job_started 1 条并报告；消息、item／请求 hash、身份及重复初始化的 journal／备份数量保持。
  - 原定向回归761项通过；索引入口修正后补验125项（与前组重叠，不累加）；原事务迁移、失败回滚及多版本链11项通过。真实旧原件恢复探针核中断续跑、完整回退、恢复方向、重复进入及 WAL 新写入冲突；临时探针工作区已删除。Ruff、差异和 OpenSpec 严格校验通过，未新增测试文件。命令、候选源码指纹、退出码和正式原件位置见 `out/tests/temp/2026/11/20/full_history_upgrade/artifacts/verification.json`。
  - 新增历史链未重跑 Windows，未同步 dev/release/main、未 push；缺少已验收 release 基线，本次仅按用户指定实验来源验收。
