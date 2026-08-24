## Why

本变更最初描述了独立的 Turn 投影和旧 Trace 迁移路径。当前实现已经确定由单个 rollout 的 `rollout.jsonl` 与 `index.sqlite` 直接提供会话历史，因此需要把本变更收敛为 rollout-backed 历史读取与 Web 默认渐进加载契约，避免与 `refactor-rollout-checkpoint-storage` 重复或冲突。

## What Changes

- 将 `/bootstrap` 与 `/history` 的主时间线数据源固定为会话 rollout 的 `rollout.jsonl` 和 `index.sqlite`；上下文边界由 SQLite view 表达，不创建物理 segment 或 chunk。
- 以完整 Turn 为边界支持从头、从尾、游标前、游标后和游标中心加载，并按 include 策略返回用户消息、工具摘要、工具详情和最终响应。
- 将默认 Web 加载固定为无锚点时最新 1 个 Turn；恢复已有位置时使用 `around(anchor)` 一次加载前后各 4 个 Turn，默认只返回用户消息和最终响应，并通过 before/after 游标继续双向加载。
- 将 `rewind`、`continue/resume` 和 `replay` 的职责分开；`replay` 是 `rewind + 可选编辑 + continue`，SQLite context view 只由上下文边界创建。
- 为当前 Turn 提供立即重载的工具详情开关；默认工具详情折叠，不改变其它 Turn 或 Gateway 默认策略。
- 使用 `asset/custom_tool_test_workspace` 的确定性 mock 会话和预生成真实 128 Turn 会话验证历史加载；测试运行时只复制整个工作区，不引入真实模型长 E2E 或多会话摘要基准。
- 将历史投影细分为 `assistant_text`、`thinking`、`tool_summary`、`tool_call`、`tool_result` 和 `final_response`；`thinking` 使用可读 `reasoning`、安全 `summary` 或不携带正文的 `encrypted` 块，最新 1 个 Turn 默认增加可展示 thinking，但不加载 Codex encrypted reasoning 原文。
- 以 `turn_finalize`/`final_message_sequence` 作为最终响应权威定位，禁止通过 role 或完整消息 heuristic 作为主路径。
- 为 128 Turn 混合 reasoning/tool fixture 增加真实 8011 链路的 p50/p95 性能验收，不增加 20+ 会话摘要基准。
- 真实模型会话与大型工具 mock 会话使用不同 session ID；生成 mock fixture 不得覆盖真实会话。

## Capabilities

### Modified Capabilities

- `session-turn-history`: 改为 rollout-backed 的有界历史读取、游标和内容投影。
- `progressive-turn-rendering`: 改为最新 Turn、around(anchor)、before/after 双向加载和每 Turn 活动统计折叠行。

## Impact

- 后端历史读取只通过 `RolloutHistoryReader`、rollout JSONL 和 SQLite 索引完成；Trace 与旧 turn projection 不再作为主时间线回退来源。
- Gateway 负责解析所属 Gateway 的嵌套历史加载配置，代理 Gateway 只透传所属 Gateway 的结果。
- Web 只实现默认加载体验，保留前端后续扩展其它加载策略的接口。
- 测试使用 `asset/custom_tool_test_workspace/` 作为只读模板，由正式 fixture 复制到 `out/tests/.../workspace`；后端真实 128 Turn 测试使用 `ses_8128...`，浏览器大型工具投影测试使用确定性 `ses_9f4e...`。不在测试运行时调用真实模型，不修改资产目录。
