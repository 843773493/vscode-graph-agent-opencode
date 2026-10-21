# 目录用途

`assembly/` 负责 v2 ContextRequestPlan 与 ContextAssemblySnapshot 的 seal、manifest 和恢复适配。

# 可修改内容

- 可以实现 assembly 生命周期、selection manifest 和 terminal outcome 的持久化适配。
- 可以调用 domain 计划/引用值对象与 storage port。

# 不可修改内容

- 不得读取 v1 artifact，不得生成 LangChain message 或 Provider tools。
- 不得成为 canonical item payload 的第二事实源。

# 规范

- `content_store.py`只编码封存内容DAG；canonical正文复用精确历史item，不读取当前Provider配置或源文件。冷读先完整重建再校验原hash，fork保留历史owner与全部依赖。
- selection 只在 assembly scope 产生，seal 前不得 dispatch；manifest 不一致必须 fail-closed。
