# 目录用途

`hash/` 是 v2 ContextRequestPlan 规范哈希投影的领域 owner：plan hash、request hash
与其共用的哈希范围投影。它只做确定性计算，不负责生命周期、I/O 或持久化。

# 可修改内容

- 可以维护 plan/request 的哈希范围、canonical preimage 与 digest 计算。
- 可以维护 `hash_projection.py` 中供两个 hash 共用的唯一范围投影。

# 不可修改内容

- 不得读取或写入 rollout JSONL/SQLite、detail 文件、Provider 请求或工作区文件。
- 不得导入 rollout_context、编排服务、Agent middleware 或资源平台实现。
- 不得为旧 import 路径提供 re-export 垫片或兼容别名。

# 规范

- 所有 hash 使用 RFC 8785 JCS v1（`sha256_jcs`）；非法 JSON value 必须直接抛出。
- 两个 hash 的覆盖范围必须严格分离：plan hash 只覆盖 plan 自身，request hash 额外覆盖 provider 与 wire request。
- 共用逻辑只能定义在 `hash_projection.py` 一处，其余模块必须 import，禁止复制。
