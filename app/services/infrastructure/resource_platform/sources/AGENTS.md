# 目录用途

`resource_platform/sources/` 存放实际资源来源 owner。当前内置工作区文件 owner 负责稳定读取文件并把结果发布为可供多个 Agent runtime 使用的快照。

# 可修改内容

- 可以维护文件来源 owner，以及认证 Gateway Skill 原始快照的 typed 消费 owner。
- `resource_navigation.py` 是 Workspace/Gateway 共用的固定根只读导航实现；普通导航不经过来源登记、快照或上下文激活。
- 可以定义来源的虚拟 URI、revision、可用性和错误状态。

# 不可修改内容

- 不得把文件读取、变化观察和 Skill/AGENTS 的业务注入规则写进同一个 middleware。
- 不得直接写 ContextStore、checkpoint、JSONL/SQLite 或向模型返回物理路径。
- 不得按请求重新扫描工作区；不得为每个 SessionThread 创建独占 watcher。

# 规范

- 显式追踪的本机文件来源必须通过共享 `WorkspaceFileWatchService` 接收变更并稳定读取；Gateway Skill 来源仅接受认证 owner 的无路径快照，在接受时校验身份、受众及 UTF-8/hash，不得回读 Gateway 宿主路径。普通导航从固定根有界读取，不创建 watcher/source/revision；Gateway 导航沿既有认证连接与 Workspace audience 交给同一个文件导航实现。
- 快照是不可变值；错误状态显式返回，旧有效快照只能作为保留事实，不能冒充新版本。
- 新源码目录必须遵守仓库的四段式 `AGENTS.md` 约定。
