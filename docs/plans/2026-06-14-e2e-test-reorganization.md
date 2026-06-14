# E2E 测试目录整理计划

## 当前文件分析

| 现有文件 | 核心场景 | 目标文件 |
|---|---|---|
| `test_agent_execution.py` | Agent 初始化、单步执行、session 隔离 | `test_agent_execution.py`（保留并增强） |
| `test_agent_name_alignment.py` | Agent 切换后 system prompt 名称对齐 | `test_agent_capabilities.py` |
| `test_api_endpoints.py` | 完整 session 流、前端响应诊断、同 session 排队、跨 session 并行 | `test_session_lifecycle.py` + `test_job_scheduling.py` |
| `test_deepagent_integration.py` | DeepAgent 工具调用链路、system reminder、事件顺序 | `test_agent_execution.py` |
| `test_full_e2e.py` | 完整 session 流、同 session 排队、跨 session 并行、自动继续 | `test_session_lifecycle.py` + `test_job_scheduling.py` + `test_auto_continue.py` |
| `test_tool_denylist_visibility.py` | 工具 denylist 对模型不可见 | `test_agent_capabilities.py` |

## 目标结构（4 个文件）

1. **`test_session_lifecycle.py`**
   - 创建会话
   - 发送消息并等待完成
   - 验证消息历史
   - 前端响应诊断流
   - 完整 session 流（从 `test_api_endpoints.py` 和 `test_full_e2e.py` 去重合并）

2. **`test_job_scheduling.py`**
   - 同 session 多个 job 串行排队
   - 跨 session job 并行执行
   - job 事件历史

3. **`test_agent_execution.py`**
   - Agent 初始化/单步执行
   - Session 隔离
   - DeepAgent 工具调用链路
   - System reminder 注入与事件顺序

4. **`test_agent_capabilities.py`**
   - Agent 切换后名称/system prompt 对齐
   - 工具 denylist 可见性

5. **`test_auto_continue.py`**（可选，若用户希望单独保留）
   - 会话自动继续 start/stop

## 合并原则

- 删除重复测试：完整 session 流在 `test_api_endpoints.py` 和 `test_full_e2e.py` 中重复，保留一个语义最完整的版本。
- 每个文件使用独立端口和工作区，避免共享状态。
- 保持现有 helper 函数（`wait_for_job_done`、`normalize_text` 等）在 `utils.py` 中。
- 重命名后的文件职责单一，命名清晰。

## 最终文件列表

- `tests/e2e/test_session_lifecycle.py`
- `tests/e2e/test_job_scheduling.py`
- `tests/e2e/test_agent_execution.py`
- `tests/e2e/test_agent_capabilities.py`
- `tests/e2e/test_auto_continue.py`（如果保留自动继续测试）

## 实施步骤

1. 创建 `test_session_lifecycle.py`，合并 `test_api_endpoints.py::test_full_session_flow` 和 `test_full_e2e.py::test_full_session_flow`，保留 `test_api_endpoints.py::test_frontend_response_diagnostic_flow`。
2. 创建 `test_job_scheduling.py`，合并同 session 排队和跨 session 并行测试。
3. 创建 `test_agent_capabilities.py`，合并 agent 名称对齐和工具 denylist 测试。
4. 更新 `test_agent_execution.py`，追加 DeepAgent 工具调用和 system reminder 测试。
5. 保留 `test_auto_continue.py` 或并入 `test_session_lifecycle.py`。
6. 删除旧文件：`test_agent_name_alignment.py`、`test_api_endpoints.py`、`test_full_e2e.py`、`test_tool_denylist_visibility.py`、`test_deepagent_integration.py`。
7. 运行全量 e2e 测试验证。
