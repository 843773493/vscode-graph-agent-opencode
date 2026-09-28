## Why

用户裁定：彻底清理项目中带 agent memory 的部分。

memory 相关实现全部未接入生产，属"未接线占位 / 虚假声明"，违反 AGENTS.md「彻底根除双轨」与「程序绝不能默默失败」：

- 生产链 `create_runtime_deep_agent_for_session` → `app/runtime/agent_runtime.py:120` 全程不传 `memory`；`app/container.py` 零 memory 装配，故 `app/agents/deep_agent_stack.py:202-208` 的 `if memory:` 分支恒不触发。
- `app/services/infrastructure/resource_platform/adapters/memory_state.py` 的 `MemoryStateAdapter`/`MemoryStateReader` 无任何生产实现；`bootstrap.py:152` 判空、`container.py:476` 未传，`platform.memory_states` 生产恒 `None` 且全仓零消费方。
- `configs/workspace_inline.jsonc:427-433` 的 `agent.memory` 6 键与 `agent.knowledge.retrieval` 4 键全仓零代码读取。
- `app/agents/graph_binding.py:366` 的 `"StructuredMemoryMiddleware"` slot 是**虚假声明**：生产从不装配该 middleware，slot 却让 `graph_schema_hash` 声称图族含它。

## What Changes

- **移除 agents 层 memory 链**：删除 `app/agents/structured_memory_middleware.py`、`MEMORY_SYSTEM_PROMPT`、`deep_agent_stack.py` 的 `memory` 形参与 `if memory:` 分支，以及 `agent_factory.py` / `agent_runtime.py` 链路上的 `memory` 参数。
- **移除 resource_platform memory state 适配链**：删除 `adapters/memory_state.py`、`adapters/__init__.py` 的 re-export，以及 `bootstrap.py` 的 `memory_state_reader`/`memory_state_keys` 注入点与 `memory_states` 字段。
- **移除 `memory_state` source kind**：`observed_source.py` 的 `_SOURCE_KINDS` 去掉该元素，并同步修正只提 gateway 的措辞。
- **移除灰色登记位**：`instruction_producers.py` 的 `agent_memory` policy key 与 `app/prompting/registry.py` 的 `agent_memory` prompt tag；并处置因此成为死值的 `PromptTrustLevel.untrusted_reference`。
- **删除 graph slot 并 bump revision**（**BREAKING**）：删除 `"StructuredMemoryMiddleware"` slot，并把 `DEEP_AGENT_GRAPH_REVISION` 由 1 bump 到 2。历史 workspace 的 persisted binding 一次性 fail-closed 失效，这是本仓库「graph 定义变更即 fail-closed、绝不回退到最新图」设计下的预期行为，登记为显式迁移义务。**真实缺口必须一并登记（H2）**：当前仓库**不存在任何自动重绑入口**——`save_graph_binding` 对同 owner 写不同 selector 抛 `graph-binding-store-conflict`，`resolve_or_persist_graph_binding` 不重建；历史 owner（含 live 用户会话）在无显式迁移时永久阻断。因此本 change **必须**登记一次性、幂等、显式、可审计的重绑/清理任务（不得静默覆盖），覆盖 live 工作区与 `out/` 下既有 binding。
- **删除配置键与 schema 定义**（**BREAKING**）：删除 `agent.memory`（含 `workspace_schema.jsonc` 的 `$defs.agentMemory`）与测试基线中的 memory 块。schema 逐层 `additionalProperties:false`，旧用户残留键会在配置验证时显式报「配置验证失败」，属预期的 fail-closed 收紧。
- **同步修正既有 change 文档矛盾**：`add-unified-virtual-resource-addressing` 的 `spec.md:211`/`proposal.md:19`/`tasks.md:7` 称描述符闭集「含 memory」，而代码 `32bc6256` 已删除；作为跨 change 同步项登记。
- 明确**不实现** `knowledge`/`safety` 接线；明确**不动** B/E 类无关同名（含 `docs/memory/`、`ZeroMemory`、in-memory、`enable_workspace_memory` 待裁定项等）。

## Capabilities

### New Capabilities

- `agent-memory`：以**负向边界 requirement** 固化「任何生产运行路径都不得装配、读取或寻址 agent memory」，归档后写入 main spec 并持久保留，防止后续重新引入双轨。

### Modified Capabilities

无。经检索，`openspec/specs/` 下 32 份 main spec **零 memory requirement**（`rg -i memory openspec/specs` 零命中）；被移除的 memory 能力此前只存在于未接线的代码、配置与尚未归档 change 的 delta 描述中，从未进入任何 main spec。因此不存在可 MODIFIED 的既有 requirement。

**spec delta 形式（owner 裁定 D-1：ADDED-only）**：delta 只保留 `## ADDED Requirements` 承载「memory 必须不存在」的负向边界；对尚不存在于 main specs 的 capability 使用 `## REMOVED Requirements` 属 warned no-op，且归档时会被丢弃、与 REMOVED 语义不符，故不使用 REMOVED 段。「移除了哪 7 条能力」的完整记录留在本 `proposal.md` 与 `design.md`（见下）。

### 本 change 移除的 7 条具名能力（记录，不再进 delta）

1. **Agent memory middleware 装配链**：`app/agents/structured_memory_middleware.py`（`StructuredMemoryMiddleware`）与 `app/agents/middleware_prompts.py` 的 `MEMORY_SYSTEM_PROMPT`，及 `deep_agent_stack.py:202-208` 的 `if memory:` 分支；生产链全程不传 `memory`。**迁移**：无需迁移，因该 middleware 从未被任何生产路径装配（`memory` 恒为空、分支恒不触发），删除不留存量数据。
2. **MemoryStateAdapter 注入链**：`app/services/infrastructure/resource_platform/adapters/memory_state.py` 的 `MemoryStateAdapter`/`MemoryStateReader`，及 `bootstrap.py` 的 `memory_state_reader`/`memory_state_keys` 注入点与 `memory_states` 字段。**迁移**：无需迁移，因 `platform.memory_states` 生产恒为 `None` 且全仓零消费方，无持久化状态。
3. **`memory_state` observed source kind**：`app/services/infrastructure/resource_platform/sources/observed_source.py:28` 的 `_SOURCE_KINDS` 成员。**迁移**：无需迁移；允许集合收窄属 fail-closed 收紧，生产只构造 `file`，删除后既有数据不受影响。
4. **`agent.memory` 配置键**：`configs/workspace_inline.jsonc:427-433` 与 `configs/workspace_schema.jsonc` 的 `$defs.agentMemory`。**迁移**：需处置旧用户残留键——schema 逐层 `additionalProperties:false`，残留 `agent.memory.*` 会在 `configs/runtime.py` 配置验证时显式报「配置验证失败」（fail-closed，不静默剔除、不自动迁移），用户在启动报错中看到字段位置后手工删除该键。
5. **`agent_memory` 条件化 instruction policy key**：`app/agents/instruction_producers.py:43` 的 `ConditionalPolicyKey` 成员与 `:319` 的 `_POLICY_KEYS` 成员。**迁移**：无需迁移，因该 key 从不被 `build_toolset_policy_keys` 产出，无运行时状态。
6. **`agent_memory` system prompt tag**：`app/prompting/registry.py:227-233` 的 `PromptTagSpec`（及其唯一消费者被删 middleware）。**迁移**：无需迁移，因该 tag 只被被删 middleware 消费，无持久化引用。
7. **`StructuredMemoryMiddleware` graph slot**：`app/agents/graph_binding.py:366` 的 `DEEP_AGENT_MIDDLEWARE_STACK` 成员（虚假声明）。**迁移**：**需迁移**——删 slot 改变 `graph_schema_hash` 且 revision 1→2，历史 persisted GraphBinding 一次性 fail-closed，必须一次性显式重绑/清理（详见下方 Impact 与 design D2、tasks 7.3.1–7.3.3）。

> 另登记（M2，本 change 不改代码）：`instruction_producers.py:35` 的 `InstructionProducerId` 与 `:311` 的 `_KNOWN_PRODUCER_IDS` 仍含 `"R09"`（第 5 条能力的 producer 身份编号），为待删除项。

## Impact

- **代码**：`app/agents/{structured_memory_middleware.py,middleware_prompts.py,deep_agent_stack.py,agent_factory.py,instruction_producers.py,graph_binding.py}`、`app/runtime/agent_runtime.py`、`app/services/infrastructure/resource_platform/{adapters/memory_state.py,adapters/__init__.py,bootstrap.py,sources/observed_source.py}`、`app/prompting/registry.py`。
- **配置**：`configs/workspace_inline.jsonc`、`configs/workspace_schema.jsonc`、`configs/tests/workspace/default.jsonc`。
- **测试**：`tests/unit/agents/test_middleware_prompts.py`、`tests/unit/agents/policy/test_tool_policy.py`、`tests/unit/agents/test_instruction_producers.py`、`tests/unit/services/infrastructure/resource_platform/test_bootstrap.py`、`tests/unit/services/infrastructure/test_source_reconciler.py`、`tests/unit/agents/test_graph_binding.py`（验证）。
- **依赖**：不变。`deepagents` 的其它能力仍在大量使用，`pyproject.toml` 的 `deepagents==0.6.2` 保持不变。
- **迁移**：graph binding revision bump 导致历史 persisted binding fail-closed，并需一次性显式重绑/清理（当前无自动重绑入口，见上）；配置 schema 收紧导致旧用户残留 memory 键 fail-closed。
- **前端**：`src/` 零 agent memory 触点，不在范围。
- **与既有 change 的关系**：本 change 是独立切片，不并入 `add-context-injection-lifecycle` / `add-unified-virtual-resource-addressing` / `migrate-session-context-uri-to-vrn` / `add-itemized-rollout-context` / `add-multi-workspace-backend-mounting`；仅以登记形式提出跨 change 文档同步项。
