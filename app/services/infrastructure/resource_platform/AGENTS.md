# 目录用途

`resource_platform/` 提供进程级资源观察与已发布快照能力。它连接底层监视器和业务 owner，但不解释业务语义，也不写入会话上下文。

# 可修改内容

- 可以维护可复用的资源快照、revision 和观察任务。
- 可以装配现有的 `WorkspaceFileWatchService`，为 AGENTS.md、Skill 等文件来源提供内存快照。

# 不可修改内容

- 不得在这里决定 Skill 是否激活、上下文使用什么 wire role 或何时开启 epoch。
- 不得持有 ContextStore、RolloutCheckpointSaver 或业务数据库 writer。
- 不得把宿主机路径、正文或凭据放入模型可见协议。

# 规范

- 监视器只发送轻量变更，资源 owner 负责读取、验证并发布 immutable snapshot。
- 读取失败必须保留上一个有效快照并暴露 unavailable 状态，不得伪造最新内容。
- 所有长任务都必须由显式 owner 启停，不得自行创建第二套 dispose/lifecycle 抽象。

