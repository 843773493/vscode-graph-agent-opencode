# 目录用途

`app/agents/summarization/` 是缓存优先上下文压缩链路的垂直子包：原先集中在 `app/agents/cache_preserving_summarization.py` 的单一实体，按“常量/状态 → 事件投影 → 分区与事件构造 → 摘要生成辅助 → 响应解释 → 中间件 → 工具中间件 → 工厂”的单向依赖顺序拆分为多个模块。对外契约仍由原路径 `app/agents/cache_preserving_summarization.py` 这个 facade 逐字重导出，调用方无需改动。

各模块职责：

- `state.py`：策略常量、`SummaryToolCallError`、`CachePreservingSummarizationState`（LangGraph 状态 schema）与 `CachePreservingPartition`。
- `projection.py`：把 `_summarization_event` 投影为模型可见消息与状态边界（`apply_summarization_event` 及两个 cutoff 映射）。
- `planning.py`：缓存前缀切点候选、自动/HTTP/工具入口共用的安全压缩分区与事件构造。
- `retry.py`：摘要指令构造、媒体剥离、大型工具载荷压缩、overflow 重试中段切分、重试标记与摘要文本校验。
- `responses.py`：把摘要 handler 的 `ModelResponse` 解释为摘要正文。
- `middleware.py`：同步 `wrap_model_call` 与异步 `awrap_model_call`/`_awrap_model_call_impl` 两条对等 wrap 链路。
- `preflight.py`：无 durable owner 装配下的压缩 preflight 显式失败端口。
- `tool_middleware.py`：`compact_conversation` 工具的缓存优先压缩中间件。
- `factory.py`：由 `BaseChatModel` + backend + preflight 端口构造中间件。

# 可修改内容

- 可以在保持原路径 facade 导出面不变的前提下，调整各模块内部实现与模块划分。
- 可以按业务子链路继续下沉拆分过大的模块（当前 `middleware.py` 最大）。

# 不可修改内容

- 不得改动 `app/agents/cache_preserving_summarization.py` 的重导出面：它必须逐字保持原模块全部顶层符号（含私有/非 `__all__` 名），这是零回归契约。
- 不得合并同步 `wrap_model_call` 与异步 `awrap_model_call`/`_awrap_model_call_impl`：二者是对等实现，语义差异只登记不合并。
- 不得改变 LangGraph 状态注解（如 `_force_cache_compaction: Annotated[NotRequired[bool], PrivateStateAttr]`）与 `serialized_name`、`state_schema` 等运行期契约。
- 不在本目录直接读写 checkpoint 中的原消息，不做业务规则或会话状态计算。

# 规范

- 保持从 `state` 出发的单向 import 图，禁止引入循环依赖；跨模块引用只在确实需要时引入具体符号。
- 单据改动聚焦一条垂直子链路，自底向上改透并移除旧实现，不做外层包胶水式补丁。
- 与 DeepAgents 私有实现类交互时保留其 `TODO` 说明，并集中在适配点内。
- 修改本目录后运行 `ruff check`，并带进程外保护地跑对应单元测试（如 `tests/unit/agents/test_cache_preserving_summarization.py`）。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
