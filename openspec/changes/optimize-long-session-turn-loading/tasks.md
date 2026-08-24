## 1. Rollout 历史读取

- [x] 1.1 以 rollout `rollout.jsonl` 和 `index.sqlite` 实现有界 bootstrap、Turn 分页和详情读取。
- [x] 1.2 支持 head、tail、before、after、around 方向以及 opaque cursor、projection epoch 和 stale-cursor 错误。
- [x] 1.3 按完整 Turn 组装用户消息、steering、工具活动、内部消息和最终响应，不从旧 Trace projection 回退。
- [x] 1.4 实现 `user`、`tool_summary`、`tool_call`、`tool_result`、`final_response` 等 include 投影及详情硬上限。
- [x] 1.5 为 append-only rollout 使用 SQLite Turn span/byte offset 快速路径；semantic boundary、replace、truncate 和未索引记录走显式恢复路径。

## 2. Gateway 历史策略

- [x] 2.1 增加四层以上的嵌套 `features.session_history.loading.progressive` 配置和 schema 校验。
- [x] 2.2 默认首次加载最新 1 个 Turn，包含用户消息、工具摘要和最终响应。
- [x] 2.3 默认锚点窗口前后各加载 4 个 Turn，使用 `user + final_response`，Gateway 配置不再按滚动次数切换批次。
- [x] 2.4 通过 Gateway 代理透传所属 Gateway 的历史策略，禁止上游覆盖下游会话策略。

## 3. Web 默认体验

- [x] 3.1 实现最新 Turn 优先、`around(anchor)` 恢复以及 before/after 双向滚动锚点保持。
- [x] 3.2 每个历史 Turn 固定显示活动统计折叠行，复用 assistant avatar/codicon 作为工具详情入口。
- [x] 3.3 工具详情入口只重载当前 Turn 的 `tool_call` 与 `tool_result`，不改变其它 Turn 或 Gateway 默认策略。
- [x] 3.4 使用 Turn revision、generation、请求取消和 stale-cursor 校准处理加载竞态。
- [x] 3.5 保持 Composer 与历史时间线状态解耦，历史错误透明展示且不伪造成功。
- [x] 3.6 折叠行点击通过 SQLite-backed `/history` 详情接口加载当前 Turn 的中间消息，并保持右/下箭头语义。

## 4. 测试和验证

- [x] 4.1 使用确定性 stub rollout/SQLite 集成夹具，验证摘要与工具详情两种 include 组合。
- [x] 4.2 验证 `around(anchor)`、两侧 cursor、before/after 双向加载，以及 head、tail 方式。
- [x] 4.3 验证 128 Turn 的有界读取不 materialize 全量消息，并验证内部前导消息不制造空 Turn。
- [x] 4.4 添加前端 API、状态、组件和工具详情菜单测试；不引入真实模型 E2E。
- [x] 4.5 运行 Python 静态分析、focused pytest、Bun 测试、Web build 和 `openspec validate --all --strict`。

## 5. Finalization、reasoning 投影与性能验收（新增审查任务）

- [x] 5.1 更新 API/schema/include，明确 `assistant_text`、`thinking`、`tool_summary`、`tool_call`、`tool_result`、`final_response` 的独立语义和默认投影。
- [x] 5.2 实现无锚点最新 1 Turn 默认投影和有锚点窗口 `user + final_response`；统计行保持折叠并显示隐藏消息计数。
- [x] 5.3 实现历史读取 projection index，禁止为 summary materialize 完整 AIMessage/ToolMessage；完整内容只按目标 offset 读取。
- [x] 5.4 补充普通 reasoning 与 Codex encrypted reasoning stub，验证 encrypted payload 只进入 provider 恢复路径，不进入 Web 响应。
- [x] 5.5 将 Web 历史请求切换到页面级批量 offset 读取的后端契约，并验证一次 snapshot、一次 SQLite records 查询和单个 `rollout.jsonl` 的批量 I/O 策略。
- [x] 5.6 使用 128 Turn 浏览器/集成 fixture 记录 p50/p95；验证 around、before/after 和当前 Turn 详情热路径不超过 200ms，不使用真实模型。
- [x] 5.7 完成三轮 OpenSpec 边界审查、`openspec validate --all --strict`、Python 静态分析、Bun 测试和 Web build。

## 6. Thinking projection 收尾

- [x] 6.1 将公开 Turn DTO/include 从字符串式 `reasoning_summary` 收敛为 `thinking`/`thinking_blocks`，定义 `reasoning`、`summary` 与无正文 `encrypted` 块。
- [x] 6.2 从 SQLite thinking projection 聚合 reasoning、provider summary 和 encrypted 标记；canonical rollout 与 LangGraph materializer 保持混合 `AIMessage` 不拆分。
- [x] 6.3 Web 将 thinking blocks 映射到每个 Turn 的活动统计折叠行，encrypted 块只显示提示，不显示或请求密文。
- [x] 6.4 增加普通 reasoning、provider summary、Codex encrypted reasoning、混合 AIMessage 的 API/前端测试，并重新运行静态分析、Web build 和 OpenSpec 校验。

## 7. 测试资产和工作区复制边界

- [x] 7.1 将真实 128 Turn 会话与确定性 mock 会话登记为不同用途和不相交 session ID，修正 fixture 清单、导航索引和生成器引用。
- [x] 7.2 让正式测试从 `asset/custom_tool_test_workspace` 复制整个工作区到 `out/tests/.../workspace`，禁止测试运行时调用生成器或写回资产目录。
- [x] 7.3 验证资产会话只包含 `rollout.jsonl`、`index.sqlite` 和 manifest，不残留旧 Trace/TurnHistory/segment 文件；重新运行相关后端、Web 和 OpenSpec 验证。
