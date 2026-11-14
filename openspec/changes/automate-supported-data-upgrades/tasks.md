## 1. 自动升级单链路

- [x] 1.1 明确已有支持来源、未知版本／数据取舍边界及原 owner 归属；修正规范与维护指引。
- [x] 1.2 正常启动只读识别升级，沿原 Launcher 停止入口停写，再接通配置／身份的原备份迁移与恢复；当前格式不重写。
- [x] 1.3 SQLite 原有有序链在锁内识别版本，备份 WAL、原子迁移及校验；迁齐共享／离线消费者。

## 2. 验收与交付

- [x] 2.1 定向覆盖未知／冲突保全、正向续跑、回退方向、写入占用、SQLite 失败回滚、当前重入及受支持配置经真实 Gateway／Workspace 启动和重启。
  - Linux 支持链集成：`test_runtime_logging_startup.py` 2 passed；旧 Trace 快照独立 1 skipped。原输出：`out/tests/temp/2026/11/14/automatic_supported_upgrade/artifacts/supported-startup-integration-04.log`。配置／SQL 定向回归、Launcher 31项及资源账本68项另有原始证据，不与重叠分组累加。
  - 最终身份预检回归126项通过，涵盖未结算运行代／活动引用及远端凭据缺失在配置与布局首写前拒绝；原输出：同目录 `identity-preflight-final-01.log`。
- [ ] 2.2 冻结候选后在 hyf_win 隔离执行同一升级链，保留命令、版本、原输出及未完成范围。
- [ ] 2.3 完成最小充分实现复审、静态分析和精确本地提交；不自动同步 dev/release/main。
- [ ] 2.4 TODO：真实旧快照的完整历史恢复尚未验收。固定来源 bf0f2e1d9d037efcb0210c458fa157fb4d54a27a 含非空 Trace JSONL，用户已将其裁定为未知来源，本轮保留原件、不新增导入；保留原测试及强断言并明确跳过，不能以支持链局部通过替代。
