# session-turn-history Specification

## Purpose
TBD - created by archiving change optimize-long-session-turn-loading. Update Purpose after archive.
## Requirements
### Requirement: Rollout 是聊天历史的唯一权威来源

系统 SHALL 直接从会话 rollout 的 `index.sqlite` 和 `rollout.jsonl` 读取聊天历史。上下文边界由 SQLite context view 表达，不创建物理 segment。Trace、日志和旧 turn projection MUST NOT 作为 `/bootstrap` 或 `/history` 的数据源，也不得在 rollout 缺失时静默回退。

#### Scenario: 旧 Trace 不能伪造历史

- **WHEN** 会话只有旧 Trace 文件而没有有效 rollout
- **THEN** 历史 API 返回可诊断的 rollout 缺失或损坏错误，不返回旧投影中的 Turn

### Requirement: 历史支持方向和游标

系统 SHALL 支持从头、从尾、游标之前、游标之后和游标中心加载完整 Turn。游标 MUST 是不透明值，并绑定 rollout、branch、projection epoch 和锚点。

#### Scenario: 游标前加载

- **WHEN** 客户端请求游标之前的 5 个 Turn
- **THEN** 返回最多 5 个不重复且完整的 Turn，并提供继续向前的游标

#### Scenario: 游标中心加载

- **WHEN** 客户端请求 `around(anchor)` 且未指定窗口覆盖数
- **THEN** 按所属 Gateway 配置返回锚点前后各 4 个完整 Turn，并同时提供 `before_cursor` 和 `after_cursor`

#### Scenario: 双向游标继续加载

- **WHEN** 用户向上滚动提交 `before_cursor` 或向下滚动提交 `after_cursor`
- **THEN** 服务只读取对应一侧的下一个固定窗口，不重复锚点 Turn，并返回更新后的同方向 cursor

### Requirement: Turn 是默认分页边界

系统 SHALL 将用户消息、合并 steering、工具活动和模型最终状态组织为完整 Turn；内部消息和工具记录不得把 Turn 拆成独立分页项。

#### Scenario: 工具活动保持完整

- **WHEN** 一个 Turn 包含 assistant tool_call、多个 tool_result 和最终 assistant 响应
- **THEN** 任意历史方向的结果都不会拆分这个 Turn

### Requirement: include 策略控制内容投影

系统 SHALL 支持用户消息、最终响应、工具摘要、工具调用、工具结果、内部消息和 metadata 的独立选择。默认工具内容 MUST 只返回工具名与状态；完整参数和结果必须显式请求并受服务端限制。

#### Scenario: 默认工具摘要

- **WHEN** 请求用户消息、`tool_summary` 和最终响应
- **THEN** 返回用户消息、工具名称/状态摘要和最终响应，不返回工具参数或完整结果

#### Scenario: 显式工具详情

- **WHEN** 请求用户消息、`tool_call`、`tool_result` 和最终响应
- **THEN** 返回对应工具调用、结果和最终响应，并返回有界详情

### Requirement: Gateway 应用默认渐进加载

系统 SHALL 按会话所属 Gateway 的嵌套配置执行历史加载。无已保存视图锚点时默认读取最新 1 个 Turn；存在锚点时使用 `around(anchor)` 返回前后固定窗口，默认两侧各 4 个 Turn，默认只包含 `user + final_response`。后续向上使用 `before_cursor`，向下使用 `after_cursor`，不得通过滚动次数隐式切换批次。

#### Scenario: 首次进入会话

- **WHEN** Web 首次打开会话
- **THEN** 只返回最新 1 个 Turn，工具摘要默认折叠

#### Scenario: 恢复已保存锚点

- **WHEN** 用户重新进入一个已保存历史位置的会话
- **THEN** 客户端先请求 `around(anchor)`，恢复锚点前后窗口并保持该 Turn 的视觉位置

#### Scenario: 代理不覆盖所属 Gateway 策略

- **WHEN** Gateway A 代理读取 Gateway B 上的会话
- **THEN** B 的历史配置决定批次和投影，A 只透传结果和游标

### Requirement: 普通追加保持游标，破坏性操作明确失效

普通追加、`continue/resume`、Turn revision 更新和 checkpoint 提交 MUST NOT 使游标失效。`rewind`、replay 中的 rewind、删除、branch 切换或无法保持身份的重建 SHALL 推进 projection epoch，并对旧游标返回明确 stale-cursor 错误。

#### Scenario: rewind 后使用旧游标

- **WHEN** 客户端在 rewind 后提交旧游标
- **THEN** API 返回可识别的 stale-cursor 错误，不静默返回另一条 branch

### Requirement: 历史读取有服务端硬上限

系统 SHALL 限制 Turn 数、record 数、详情批次、响应字节、总字符和单项字符。客户端传入更大的值不得绕过限制；结果超过预算时返回有界结果、继续游标和明确的截断状态。

#### Scenario: 请求超过响应预算

- **WHEN** 请求范围超过服务端 Turn 或字节预算
- **THEN** 返回已完成的有界结果、继续游标和明确的截断状态，不构造无限大的响应

### Requirement: 历史失败必须透明

系统 SHALL 对 rollout 损坏、索引不一致、恢复失败和无效 cursor 返回包含原因的错误。系统不得以空历史、旧投影或虚假的默认值掩盖失败。

#### Scenario: rollout 索引损坏

- **WHEN** SQLite 索引或已提交 JSONL 记录无法恢复
- **THEN** API 返回包含会话和失败原因的错误，不返回空历史或旧 projection

### Requirement: 历史投影区分 assistant text、reasoning 和 final response

系统 SHALL 独立支持 `assistant_text`、`thinking`、`tool_summary`、`tool_call`、`tool_result` 和 `final_response`。`thinking` 投影由 `thinking_blocks` 表达，块类型为可读 `reasoning`、provider 生成的 `summary` 或不携带正文的 `encrypted` 标记。`final_response` MUST 使用 `turn_finalize.final_message_sequence`，而不是把最后一个 assistant role 作为唯一依据；无 finalization 的旧 fixture 才允许 heuristic fallback。

#### Scenario: 混合 AIMessage

- **WHEN** 一个 AIMessage 同时包含 text content block、reasoning content block 和 tool_calls
- **THEN** canonical checkpoint 保持一条 AIMessage，历史投影可拆出 assistant text/tool summary，但 LangGraph 恢复不会得到多条伪造的 assistant 消息

#### Scenario: 思考块来源保持可区分

- **WHEN** provider 返回可展示 reasoning、provider summary 和/或 encrypted reasoning
- **THEN** Web 分别返回 `reasoning`、`summary` 和无正文的 `encrypted` 块，encrypted payload 只能用于 provider 恢复，不得出现在 API 响应

### Requirement: 历史摘要不 materialize 完整消息

`tool_summary`、`thinking`、final pointer 和受界限的最终 `visible_text` SHALL 从 SQLite 轻量 projection index 读取。中间完整 assistant 文本、tool_call 参数和 tool_result 只在显式 include 时按 offset 读取目标 `rollout.jsonl` 记录；普通分页不得扫描整个 rollout 文件或恢复完整 checkpoint。`visible_text`、`reasoning` 和 `summary` 均不得包含 encrypted reasoning 或工具 payload。

#### Scenario: 默认摘要只读取轻量投影

- **WHEN** 前端请求默认的最新 Turn 摘要
- **THEN** 服务从 SQLite 投影和目标 JSONL offset 读取 user、thinking blocks、tool summary 与 final response，不 materialize 完整 LangGraph checkpoint，也不读取 tool_result 正文

#### Scenario: 显式详情读取目标记录

- **WHEN** 用户只为当前 Turn 请求 tool_call 和 tool_result
- **THEN** 服务只定位并读取该 Turn 命中的 `rollout.jsonl` 记录，不扫描整个 rollout 文件或其它 Turn

### Requirement: 页面级读取延迟可验收

系统 SHALL 使用资产工作区中预生成的 128 Turn 混合 fixture 验证 bootstrap、around、before/after 双向加载和当前 Turn 详情重载。后端 SQLite/reader 热路径的优化目标为 p95 100ms 内，硬验收上限为 200ms；浏览器 prepend/append 与锚点恢复硬验收上限为 200ms。后端真实模型会话和浏览器确定性大型工具 mock 会话必须使用不同 session ID，测试运行时只读取复制后的工作区，不重新生成或修改资产。测试必须记录 p50/p95 和 snapshot、SQLite query、materialize 计数。

#### Scenario: 128 Turn 锚点双向加载

- **WHEN** 浏览器完成 bootstrap、around(anchor)、before/after 加载并点击一个 Turn 的统计折叠行
- **THEN** 每次请求都使用一个 read snapshot 和页面级批量读取，记录 p50/p95；后端 p95 不超过 200ms 并持续向 100ms 优化，浏览器 prepend/append 与锚点恢复不超过 200ms

### Requirement: Turn 活动统计与按需中间消息

每个历史 Turn SHALL 始终显示活动统计折叠行。统计至少包含耗时、隐藏消息数量、中间 assistant 数量、tool_call 数量和 tool_result 数量。折叠状态箭头指向右侧，展开状态箭头指向下方。用户展开时，客户端 MUST 通过现有 SQLite-backed history detail 接口只加载该 Turn 的中间消息；不得绕过 reader 直接打开 JSONL。

#### Scenario: 统计行默认折叠

- **WHEN** around 或 before/after 返回一个历史 Turn
- **THEN** Turn 显示统计行和右箭头，但不读取或渲染中间 assistant、tool_call 和 tool_result 正文

#### Scenario: 展开统计行

- **WHEN** 用户点击某个 Turn 的统计行右箭头
- **THEN** 客户端请求该 Turn 的 `thinking`、`tool_summary`、`tool_call` 和 `tool_result` 受限投影，成功后箭头向下并显示结果；其它 Turn 不被详情化

