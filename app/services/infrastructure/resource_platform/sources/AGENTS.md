# 目录用途

`resource_platform/sources/` 存放实际资源来源 owner。当前内置工作区文件 owner 负责稳定读取文件并把结果发布为可供多个 Agent runtime 使用的快照。

# 可修改内容

- 可以增加文件、Gateway snapshot 或权威内存状态的来源 owner。
- 可以定义来源的虚拟 URI、revision、可用性和错误状态。

# 不可修改内容

- 不得把文件读取、变化观察和 Skill/AGENTS 的业务注入规则写进同一个 middleware。
- 不得直接写 ContextStore、checkpoint、JSONL/SQLite 或向模型返回物理路径。
- 不得按请求重新扫描工作区；不得为每个 SessionThread 创建独占 watcher。

# 规范

- 来源必须通过共享 `WorkspaceFileWatchService` 接收变更，并在发布前完成稳定读取与 UTF-8/hash 校验。
- 快照是不可变值；错误状态显式返回，旧有效快照只能作为保留事实，不能冒充新版本。
- 新源码目录必须遵守仓库的四段式 `AGENTS.md` 约定。

