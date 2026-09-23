# 目录用途

`app/domain/itemized/` 是 v2 item、ContextRef、ToolSetRef、selection、hash 和 provenance 合同的领域 owner。

# 可修改内容

- 可以维护 v2 schema、枚举、JCS hash、不可变引用和 selection 校验。
- 可以维护 `hash_projection.py` 中供 plan/request hash 共用的唯一哈希范围投影。
- 可以维护 content part 与 Turn/Execution identity 的纯值对象。

# 不可修改内容

- 不得读取或写入 rollout JSONL/SQLite、detail 文件或 Provider 请求。
- 不得导入 LangChain、Agent middleware、编排服务或 legacy reader。

# 规范

- 所有 hash 使用 RFC 8785 JCS v1；非法 JSON value 必须直接抛出。
- `ref_type`、`payload_kind`、`semantic_kind` 和 status 必须使用闭合集合；未知扩展不能静默降级。
