## 1. 自动升级单链路

- [x] 1.1 明确已有支持来源、未知版本／数据取舍边界及原 owner 归属；修正规范与维护指引。
- [x] 1.2 正常启动只读识别升级，沿原 Launcher 停止入口停写，再接通配置／身份的原备份迁移与恢复；当前格式不重写。
- [x] 1.3 SQLite 原有有序链在锁内识别版本，备份 WAL、原子迁移及校验；迁齐共享／离线消费者。

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
- [ ] 2.4 TODO：真实旧快照的完整历史恢复尚未验收。固定来源 bf0f2e1d9d037efcb0210c458fa157fb4d54a27a 含非空 Trace JSONL，用户已将其裁定为未知来源，本轮保留原件、不新增导入；保留原测试及强断言并明确跳过，不能以支持链局部通过替代。
