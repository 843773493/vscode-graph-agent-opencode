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
- **删除 graph slot 并 bump revision**（**BREAKING**）：删除 `"StructuredMemoryMiddleware"` slot，并把 `DEEP_AGENT_GRAPH_REVISION` 由 1 bump 到 2。历史 workspace 的 persisted binding 一次性 fail-closed 失效，这是本仓库「graph 定义变更即 fail-closed、绝不回退到最新图」设计下的预期行为，登记为显式迁移义务。
- **删除配置键与 schema 定义**（**BREAKING**）：删除 `agent.memory`（含 `workspace_schema.jsonc` 的 `$defs.agentMemory`）与测试基线中的 memory 块。schema 逐层 `additionalProperties:false`，旧用户残留键会在配置验证时显式报「配置验证失败」，属预期的 fail-closed 收紧。
- **同步修正既有 change 文档矛盾**：`add-unified-virtual-resource-addressing` 的 `spec.md:211`/`proposal.md:19`/`tasks.md:7` 称描述符闭集「含 memory」，而代码 `32bc6256` 已删除；作为跨 change 同步项登记。
- 明确**不实现** `knowledge`/`safety` 接线；明确**不动** B/E 类无关同名（含 `docs/memory/`、`ZeroMemory`、in-memory、`enable_workspace_memory` 待裁定项等）。

## Capabilities

### New Capabilities

- `agent-memory`：登记 agent memory 能力的移除，并以负向边界固化「任何生产运行路径都不得装配、读取或寻址 agent memory」，防止后续重新引入双轨。

### Modified Capabilities

无。经检索，`openspec/specs/` 下 32 份 main spec **零 memory requirement**（`rg -i memory openspec/specs` 零命中）；被移除的 memory 能力此前只存在于未接线的代码、配置与尚未归档 change 的 delta 描述中，从未进入任何 main spec。因此不存在可 MODIFIED 的既有 requirement，移除记录以新 capability 的 REMOVED delta 表达（详见 design.md 的「spec delta 落点」决策）。

## Impact

- **代码**：`app/agents/{structured_memory_middleware.py,middleware_prompts.py,deep_agent_stack.py,agent_factory.py,instruction_producers.py,graph_binding.py}`、`app/runtime/agent_runtime.py`、`app/services/infrastructure/resource_platform/{adapters/memory_state.py,adapters/__init__.py,bootstrap.py,sources/observed_source.py}`、`app/prompting/registry.py`。
- **配置**：`configs/workspace_inline.jsonc`、`configs/workspace_schema.jsonc`、`configs/tests/workspace/default.jsonc`。
- **测试**：`tests/unit/agents/test_middleware_prompts.py`、`tests/unit/agents/policy/test_tool_policy.py`、`tests/unit/agents/test_instruction_producers.py`、`tests/unit/services/infrastructure/resource_platform/test_bootstrap.py`、`tests/unit/services/infrastructure/test_source_reconciler.py`、`tests/unit/agents/test_graph_binding.py`（验证）。
- **依赖**：不变。`deepagents` 的其它能力仍在大量使用，`pyproject.toml` 的 `deepagents==0.6.2` 保持不变。
- **迁移**：graph binding revision bump 导致历史 persisted binding fail-closed；配置 schema 收紧导致旧用户残留 memory 键 fail-closed。
- **前端**：`src/` 零 agent memory 触点，不在范围。
- **与既有 change 的关系**：本 change 是独立切片，不并入 `add-context-injection-lifecycle` / `add-unified-virtual-resource-addressing` / `migrate-session-context-uri-to-vrn` / `add-itemized-rollout-context` / `add-multi-workspace-backend-mounting`；仅以登记形式提出跨 change 文档同步项。
