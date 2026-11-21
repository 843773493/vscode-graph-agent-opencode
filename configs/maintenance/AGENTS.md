# 目录用途

保存跨配置文件、数据库或身份资源的显式维护操作及其预检、提交和恢复逻辑。

# 可修改内容

- 可以维护跨资源迁移、只读预检、备份、提交和恢复流程。

# 不可修改内容

- 普通配置 loader 不执行迁移；受支持旧版本由启动／升级生命周期在停止原 writer 后调用本目录既有 owner。
- 不在这里复制各数据库 owner 的 SQL、schema 或业务写入实现。

# 规范

- `session_history.py` 只协调固定实验来源的 catalog／control／rollout／Trace 候选，复用原 journal、CAS、备份和 Workspace ownership；丢弃计数及来源 SHA 写入迁移报告，普通运行不双读。显式恢复用 `uv run python -m configs.maintenance.session_history --workspace <绝对路径> --restore`，不自动反转回退方向。
- 受支持且无需取舍的迁移由生命周期自动触发，显式命令保留预检、选择及恢复用途；两者复用原资源 owner 的写入与并发合同，不维护两套实现。
- 缺少来源证据或发现冲突时明确失败；不猜测来源、不静默择一。
