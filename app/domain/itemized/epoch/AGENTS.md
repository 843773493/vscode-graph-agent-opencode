# 目录用途

`epoch/` 是 v2 prefix epoch 与稳定前缀合同的领域 owner：epoch/reason 状态机、
`PendingPrefixEpochTransition` 两阶段应用、`seal_assembly`、stable prefix
byte length/hash 校验、Provider-profile item frame、精确 ToolSetRef/policy
compatibility key，以及新 epoch 的 root 编译投影。它只提供纯值对象与合同
校验，不读取 rollout、Provider、CSM 或任何运行时状态。

## 职责树

下面按领域流程组织文件职责；叶子路径就是当前物理结构：

```text
epoch/
├── 状态机与稳定前缀
│   └── prefix_epoch.py       epoch 转移、frame 和稳定前缀校验
├── 输入快照与绑定
│   ├── tool_set.py           工具快照、ToolSetRef 绑定与兼容键
│   └── seal_binding.py       已应用来源修订与 assembly seal 绑定
├── root 投影
│   ├── root_compilation.py   root 候选、来源 provenance 与 wire role
│   └── source_protocol.py    root/tail 共用的模型可见来源块与正文边界
└── __init__.py               epoch 领域族说明
```

# 可修改内容

- 可以维护 `prefix_epoch.py` 的 epoch/reason 闭集、parent assembly 前缀校验、
  frame serialization 与 compatibility key。
- 可以维护 `root_compilation.py` 的 root 资格路由与 post-user wire role 投影。

# 不可修改内容

- 不得读取或写入 rollout JSONL/SQLite、detail 文件、Provider 请求或工作区文件。
- 不得导入 rollout_context、编排服务、Agent middleware 或资源平台实现。
- 不得为旧 import 路径提供 re-export 垫片或兼容别名。

# 规范

- epoch reason 闭集为 initial、compaction、rewind、toolset_changed、runtime_notice_consumed、provider_changed；Provider 切换只重投影冻结内容，不重编来源/root；notice 消费绑定真实 user acceptance，不伪造 source boundary。
- pending transition 不是 applied epoch；只有首个 assembly 成功 seal 才应用并消费。
- 同一 epoch 内只能尾部追加，父前缀必须逐字节一致；hash 算法固定，不可由调用方覆盖。
