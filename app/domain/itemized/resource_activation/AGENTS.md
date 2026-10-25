# 目录用途

`resource_activation/` 是 v2 资源激活 snapshot/provenance 冻结领域合同的 owner：
字段闭集、内容 hash 范围、完整冻结 policy 值与 SourceLineageRef 的事实定义。
它不拥有 ResourceRegistry、monitor、loader、配置来源或运行时策略选择，也不做任何 I/O。

## 职责树与数据流

```text
resource_activation/
├── __init__.py         对外发布本包的领域合同
├── shared_contract.py  唯一共享字段、错误码、校验 helper、policy 与 lineage 类型
├── provenance.py       单条资源及成员绑定事实、bindings hash
└── snapshot.py         turn/model-call 冻结快照、provenance hash

导入方向：`snapshot.py` 依赖 `provenance.py` 与 `shared_contract.py`；
`provenance.py` 只依赖 `shared_contract.py`；`__init__.py` 汇总公开符号。
```

# 可修改内容

- 可以维护 snapshot/provenance 的字段清单、闭集、内容 hash 覆盖范围与校验 helper。
- 可以维护 snapshot 持久化所需的纯 policy 值、规范化与 revision/hash 一致性校验。
- 可以维护 `shared_contract.py` 中供 `provenance.py`/`snapshot.py` 共用的唯一共享定义。

# 不可修改内容

- 不得读取或写入 rollout JSONL/SQLite、detail 文件或 Provider 请求。
- 不得导入 rollout_context、编排服务、Agent middleware 或资源平台实现。
- 不得为旧字段提供 reader、别名或 fallback。

# 规范

- 字段名即持久化列名；未知字段、别名和旧路径形态必须显式抛 ResourceActivationContractError。
- 共享符号（字段闭集、错误码、校验 helper、SourceLineageRef）只能定义在 `shared_contract.py` 一处，`provenance.py`/`snapshot.py` 必须 import，禁止复制。
- 两个内容 hash 语义严格分离：`bindings_hash` 只覆盖按 activation ordinal 的语义选择，`activation_provenance_hash` 额外覆盖 policy/parent/lineage。
