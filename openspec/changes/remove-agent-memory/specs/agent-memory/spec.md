## Purpose

定义 agent memory 被彻底移除后的边界：任何生产运行路径都不得装配 memory middleware、读取 agent memory 配置键、把 memory 作为上下文 source 或 VRN scope，也不得在 deep agent graph 的声明式骨架中重新登记 memory slot。本 capability 用于固化移除结果并防止后续重新引入双轨。

> 说明（工具行为，如实登记）：`openspec/specs/` 下 32 份 main spec 对 `memory` 零命中，被移除的 memory 能力从未进入任何 main spec，此前只存在于未接线的代码、配置与尚未归档 change 的 delta 描述中。因此本 delta 的 `## REMOVED Requirements` 为**具名登记被移除的能力**（在归档时对尚不存在于 main specs 的 capability 属 warned no-op，见 design.md D9）；负向边界由 `## ADDED Requirements` 承载，使归档后重建的 spec 具有有效 requirement 并通过 strict 校验。

## REMOVED Requirements

### Requirement: Agent memory middleware 必须经由显式 memory 来源装配

**Reason**: `StructuredMemoryMiddleware` 及其 `MEMORY_SYSTEM_PROMPT` 模板未接入生产；生产链 `create_runtime_deep_agent_for_session` → `app/runtime/agent_runtime.py:120` 全程不传 `memory`，`app/agents/deep_agent_stack.py:202-208` 的 `if memory:` 分支恒不触发。用户裁定彻底移除 memory。

### Requirement: MemoryStateAdapter 必须由 bootstrap 显式注入并暴露 memory_states

**Reason**: `app/services/infrastructure/resource_platform/adapters/memory_state.py` 的 `MemoryStateAdapter`/`MemoryStateReader` 无任何生产实现；`bootstrap.py:152` 判空、`container.py:476` 未传，`platform.memory_states` 生产恒 `None` 且全仓零消费方。

### Requirement: memory_state 必须是合法的 observed source kind

**Reason**: `app/services/infrastructure/resource_platform/sources/observed_source.py:28` 的 `_SOURCE_KINDS` 含 `"memory_state"`，但生产只构造 `"file"`，该 kind 仅测试构造。移除后 `_SOURCE_KINDS` 允许集合更严（fail-closed 更紧）。

### Requirement: agent.memory 必须是合法的 agent 配置键

**Reason**: `configs/workspace_inline.jsonc:427-433` 的 `agent.memory` 6 键与 `configs/workspace_schema.jsonc` 的 `$defs.agentMemory` 全仓零代码读取，属未接入占位。

### Requirement: agent_memory 必须是登记的条件化 instruction policy key

**Reason**: `app/agents/instruction_producers.py:43/:319` 的 `agent_memory` policy key 从不被 `build_toolset_policy_keys`（`:186-206`）产出，是「未接线能力 R09」在类型层的占位。

### Requirement: agent_memory 必须是登记的 system prompt tag

**Reason**: `app/prompting/registry.py:227-233` 的 `agent_memory` tag 唯一消费者是被删除的 `StructuredMemoryMiddleware`。

### Requirement: StructuredMemoryMiddleware 必须是 deep agent graph 的 middleware slot

**Reason**: `app/agents/graph_binding.py:366` 的 `"StructuredMemoryMiddleware"` slot 是虚假声明——生产从不装配该 middleware，slot 却让 `graph_schema_hash` 声称图族含它。

## ADDED Requirements

### Requirement: Agent memory 不得存在于任何生产运行路径

系统 SHALL NOT 在任何生产运行路径装配 memory middleware、读取 `agent.memory` 配置键、把 memory 作为上下文 source，或把 memory 作为 VRN scope。`DEEP_AGENT_MIDDLEWARE_STACK` MUST NOT 再登记 memory 相关 slot。历史持久化数据引用已移除的 memory 能力时 MUST fail-closed 显式拒绝。

#### Scenario: 旧用户配置残留 memory 键

- **WHEN** 用户级或工作区级 `workspace.jsonc` 残留 `agent.memory.*` 键，或残留键违反删除后的 schema
- **THEN** `configs/runtime.py` 的配置验证失败并报告文件路径、字段位置与错误信息，不静默忽略、不自动剔除

#### Scenario: 历史 workspace 引用已移除的 memory middleware slot

- **WHEN** 历史 workspace 的 persisted GraphBinding 的 `graph_schema_hash` 与 bump 后的 `DEEP_AGENT_GRAPH_REVISION` 不匹配
- **THEN** `resolve_or_persist_graph_binding` 以 `GraphBindingUnavailableError` fail-closed 拒绝，不回退到最新图、不静默重绑

#### Scenario: 请求期出现 memory 上下文来源

- **WHEN** 组装模型请求时出现来源为 memory 的 system/developer 控制内容
- **THEN** 系统报告未登记来源并阻止 dispatch，不把 memory 作为 instruction/file source 或 runtime event source 接纳

#### Scenario: 构造 memory_state observed source

- **WHEN** 任何调用方尝试以 `source_kind="memory_state"` 构造 `ObservedSourceDescriptor`
- **THEN** `__post_init__` 以未登记 kind 显式拒绝，不回退到其它 kind

#### Scenario: 注册 memory 相关 prompt tag 或 policy key

- **WHEN** 任何代码尝试注册 `agent_memory` prompt tag 或 `agent_memory` 条件化 policy key
- **THEN** 因该标识已从登记表物理移除而失败，不得以别名或第二套实现重新引入
