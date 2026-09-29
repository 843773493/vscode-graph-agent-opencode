# 目录用途

`epoch/` 是 v2 prefix epoch 与稳定前缀合同的领域 owner：epoch/reason 状态机、
`PendingPrefixEpochTransition` 两阶段应用、`seal_assembly`、stable prefix
byte length/hash 校验、Provider-profile item frame、精确 ToolSetRef/policy
compatibility key，以及新 epoch 的 root 编译投影。它只提供纯值对象与合同
校验，不读取 rollout、Provider、CSM 或任何运行时状态。

# 可修改内容

- 可以维护 `prefix_epoch.py` 的 epoch/reason 闭集、parent assembly 前缀校验、
  frame serialization 与 compatibility key。
- 可以维护 `root_compilation.py` 的 root 资格路由与 post-user wire role 投影。

# 不可修改内容

- 不得读取或写入 rollout JSONL/SQLite、detail 文件、Provider 请求或工作区文件。
- 不得导入 rollout_context、编排服务、Agent middleware 或资源平台实现。
- 不得为旧 import 路径提供 re-export 垫片或兼容别名。

# 规范

- 只有 initial、compaction、rewind、toolset_changed 四种 epoch reason，不存在第五类。
- pending transition 不是 applied epoch；只有首个 assembly 成功 seal 才应用并消费。
- 同一 epoch 内只能尾部追加，父前缀必须逐字节一致；hash 算法固定，不可由调用方覆盖。
