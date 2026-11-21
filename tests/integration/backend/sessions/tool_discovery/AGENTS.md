# 目录用途

验证工具发现从真实 MCP 发布、目录冻结、结果物化到 Saver 封存和冷恢复的跨模块链路。

```text
tool_discovery/
├── conftest.py                # 本目录共用的发布、封存、存储与执行准入 fixture
├── test_search.py             # 冻结目录、策略、排名、分页、物化与错误配对
├── test_context_recovery.py   # 发现结果经压缩、策略变化、冷读与 rewind 的恢复
└── test_definition_batch.py   # 同批定义的归属、引用、摘要和批量读取
```

# 可修改内容

- 发现、封存和恢复的集成场景及故障注入。
- 测试本身使用的局部历史准备 fixture。

# 不可修改内容

- 不放单纯的输入 schema 或关联身份计算单测；分别归 `tests/unit/agents/tools/` 和 `tests/unit/services/infrastructure/mcp/`。
- 不复制 MCP 发布、上下文封存或持久化的生产逻辑。

# 规范

- 由本目录 `conftest.py` 从 `tests/support/mcp/` 和 `tests/support/rollout/context_registry.py` 登记共享前提，不从其他测试文件借用 fixture。
- 使用当前测试路径的 `TestRunContext`；历史产物保留原位置。
- 沿用集成独占闸门与进程外超时、内存保护；`integration-extension-discovery` 包含本目录，不冒充真实 Provider 或完整 Web 验收。
