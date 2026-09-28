# agent-memory Specification

## Purpose
固化 agent memory 被彻底移除后的负向边界：任何生产运行路径都不得装配 memory middleware、读取 agent memory 配置键、把 memory 作为上下文 source 或 VRN scope，也不得在 deep agent graph 的声明式骨架中重新登记 memory slot。本 capability 只承载这条「memory 必须不存在」的有效边界，用于防止后续重新引入双轨。

> 说明（delta 编码，如实登记）：`openspec/specs/` 下 32 份 main spec 对 `memory` 零命中，被移除的 memory 能力从未进入任何 main spec，此前只存在于未接线的代码、配置与尚未归档 change 的 delta 描述中。因此本 delta 采用 **ADDED-only 形式**：负向边界由 `## ADDED Requirements` 承载，归档时写入 main spec 并持久保留。「移除了哪 7 条能力」的完整记录留在本 change 的 `proposal.md` 与 `design.md`（D9/D10），不使用 `## REMOVED Requirements`——对尚不存在于 main specs 的 capability 使用 REMOVED 是 warned no-op，且 REMOVED-only 会在归档时报 `archive_spec_validation_failed`（见 design.md D9 实测）。
## Requirements
### Requirement: Agent memory 不得存在于任何生产运行路径

系统 SHALL NOT 在任何生产运行路径装配 memory middleware、读取 `agent.memory` 配置键、把 memory 作为上下文 source，或把 memory 作为 VRN scope。`DEEP_AGENT_MIDDLEWARE_STACK` MUST NOT 再登记 memory 相关 slot，`InstructionProducerId`/`_KNOWN_PRODUCER_IDS` MUST NOT 再登记 `R09`。历史持久化数据引用已移除的 memory 能力时 MUST fail-closed 显式拒绝，MUST NOT 静默回退、MUST NOT 静默重绑或覆盖。

#### Scenario: 旧用户配置残留 memory 键

- **WHEN** 用户级或工作区级 `workspace.jsonc` 残留 `agent.memory.*` 键（删除 `$defs.agentMemory` 后违反逐层 `additionalProperties:false`）
- **THEN** `configs/runtime.py` 的 `validate_config` 经 `jsonschema.validate` 失败并抛出 `ValueError("配置验证失败: ... location=...")`，报告文件路径与字段位置，不静默忽略、不自动剔除
- **验证手段**：`configs/runtime.py:40 validate_config` / `:53` 的错误包装；`configs/workspace_schema.jsonc` 的 `agentConfig`/根/`agentMemory` 均 `additionalProperties:false`；`configs/layout_migrations.py`/`configs/legacy_migrations.py` 不触碰该键（已实测：残留键抛 `Additional properties are not allowed ('memory' was unexpected)`）

#### Scenario: 历史 workspace 引用已移除的 memory middleware slot

- **WHEN** 历史 workspace 的 persisted GraphBinding 的 `graph_schema_hash` 与 bump 后的 `DEEP_AGENT_GRAPH_REVISION` 不匹配
- **THEN** `resolve_or_persist_graph_binding` 以 `GraphBindingUnavailableError`（`graph_binding_unavailable`）fail-closed 拒绝，不回退到最新图、不静默重绑
- **验证手段**：`app/agents/graph_binding.py:239` 的 `graph_schema_hash` 比对与 `:666-693 resolve_or_persist_graph_binding` 的精确 revision 解析路径（`GRAPH_FACTORY_REGISTRY.resolve(persisted)` 抛 `GraphBindingUnavailableError`）

#### Scenario: 请求期出现 memory 上下文来源

- **WHEN** 组装模型请求时出现来源为 memory 的 system/developer 控制内容
- **THEN** 系统报告未登记来源并阻止 dispatch，不把 memory 作为 instruction/file source 或 runtime event source 接纳
- **性质（如实标注）**：本条**依据现状不存在而成立**——memory 从不是任何已登记 source kind（`_SOURCE_KINDS` 删除 `memory_state` 后更严），生产链不装配 memory middleware、不读取 memory 配置键，故该状态无法自然出现。当前**不存在**「未登记来源即阻止 dispatch」的独立钩子，本条不声称存在该钩子，只声明「若出现即被拒绝」的边界。
- **验证手段**：`app/services/infrastructure/resource_platform/sources/observed_source.py:28 _SOURCE_KINDS` 不含 memory 类 kind（构造即抛 `ValueError`）；`rg -n 'memory' app/agents app/runtime` 零 A 类命中

#### Scenario: 构造 memory_state observed source

- **WHEN** 任何调用方尝试以 `source_kind="memory_state"` 构造 `ObservedSourceDescriptor`
- **THEN** `__post_init__` 以未登记 kind 显式拒绝，不回退到其它 kind
- **验证手段**：`app/services/infrastructure/resource_platform/sources/observed_source.py:70` 的 `if self.source_kind not in _SOURCE_KINDS: raise ValueError`（移除后 `memory_state` 不再在闭集内）

#### Scenario: 注册 agent_memory 条件化 policy key

- **WHEN** 任何代码尝试把 `agent_memory` 作为条件化 instruction policy key 注册或用于绑定
- **THEN** 因该标识已从 `ConditionalPolicyKey` 与 `_POLICY_KEYS` 物理移除而失败，不得以别名或第二套实现重新引入
- **验证手段**：`app/agents/instruction_producers.py:43 ConditionalPolicyKey` 与 `:319 _POLICY_KEYS` 不含 `agent_memory`；`InstructionProducerSpec.__post_init__` 与 `assert_toolset_policy_binding`（`:152-166`，未知 key 抛 `instruction-producer-policy-unknown`）拒绝未登记 key；`build_toolset_policy_keys`（`:178`，体 `:187-197`）从不产出该 key

#### Scenario: 重新引入 memory prompt tag

- **WHEN** 任何代码尝试重新注册 `agent_memory` prompt tag
- **THEN** 该 tag 已从 `TAG_SPECS` 物理移除、全仓零引用，重新引入即重新构成双轨，本 change 的边界不允许
- **性质（如实标注，M4）**：本条**依据现状不存在而成立**——`TAG_SPECS` 是无闭合集拒绝机制的静态元组，`StructuredPromptRegistry._validate_registration`（`app/prompting/registry.py:87-148`）只校验名称格式 `^[a-z][a-z0-9_]*$`（`agent_memory` 是合法名）与父子/放置/codec 约束，**不会拒绝重新注册同名 tag**；`registry.tag(name)`（`:150`）对未注册名才抛 `ValueError`。因此本 change 不为 prompt tag 引入闭合集拒绝机制，验证只能依据「登记表物理移除 + 零引用」。
- **验证手段**：`app/prompting/registry.py:227-233` 的 `PromptTagSpec("agent_memory", ...)` 已删除；`rg -n 'agent_memory' app tests` 零命中

