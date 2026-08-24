## ADDED Requirements

### Requirement: 最新 Turn 先于旧历史完整呈现
客户端 SHALL 在会话切换时先呈现最新 Turn summary；完整中间消息只在用户点击该 Turn 的活动统计行后请求，旧历史页和完整 Trace 不得阻塞这一过程。

#### Scenario: 切换到长会话
- **WHEN** bootstrap 返回最新 Turn summary
- **THEN** 最新用户输入、最终响应和活动统计先显示，完整中间消息保持未加载，直到用户明确展开该 Turn

### Requirement: Markdown 渐进且非阻塞渲染
客户端 SHALL 先提交轻量文本或稳定骨架，超过固定阈值的 Turn detail JSON SHALL 在主线程外解码和解析，再以低优先级增强 Markdown；大型详情处理和 Markdown 解析不得阻塞 Composer 输入。

#### Scenario: 最新回答包含大型 Markdown
- **WHEN** 最新 Turn full detail 包含大量表格、代码块和列表
- **THEN** Composer 在 Markdown 增强期间保持响应，先展示有界格式化预览，并在用户明确展开后展示完整格式化内容

### Requirement: 仅水合可视范围详情
客户端 SHALL 使用虚拟列表，并只为可视 Turn 和受限 overscan 请求 full detail；折叠的大型 reasoning 与工具输出在展开前 MUST NOT 执行完整 Markdown 解析。

#### Scenario: 快速滚动长历史
- **WHEN** 用户滚动包含大量 Turn 的会话
- **THEN** DOM 和详情请求数量保持与可视窗口及固定 overscan 相关，而不随总历史线性增长

### Requirement: Turn 分页保持视觉锚点
客户端 SHALL 以完整 Turn 向前分页，并在旧 Turn 前插后保持当前可见 Turn 的位置；分页、SSE 与终态协调 MUST 使用 upsert 而不是替换全部历史。

#### Scenario: 顶部加载旧 Turn
- **WHEN** 用户滚动到顶部并加载前一页
- **THEN** 当前首个可见 Turn 保持视觉位置且新页不包含半个 Turn

#### Scenario: 历史加载后当前 Job 完成
- **WHEN** 客户端已经加载多页旧 Turn 且当前 Job 进入终态
- **THEN** 当前 Turn 原位更新，已加载旧页仍保留

### Requirement: 主时间线不恢复完整 Trace
会话切换时客户端 MUST NOT 为构建聊天时间线读取完整 Trace；调试事件 SHALL 在事件视图中独立按需分页。

#### Scenario: 会话包含大量 Trace
- **WHEN** 用户只打开聊天视图并切换该会话
- **THEN** 网络请求不包含无上限的 Trace 读取，事件视图未打开时不传输完整事件历史

### Requirement: 取消终态与运行失败具有不同语义

客户端 SHALL 将用户主动 `session_interrupted` 显示为中性“已由用户中断”状态，将其他 `job_cancelled` 显示为“任务已取消”；取消事件 MUST NOT 使用运行失败文案或错误样式。只有真实 `error` 与 `job_failed` SHALL 显示为运行失败。

#### Scenario: 用户点击中断生成

- **WHEN** 同一 Turn 依次收到 `session_interrupted` 与伴随的 `job_cancelled`
- **THEN** 时间线只显示一次“已由用户中断”，不显示“运行失败”或重复取消状态

#### Scenario: 非用户触发的任务取消

- **WHEN** Turn 只有 `job_cancelled` 而没有 `session_interrupted`
- **THEN** 时间线显示“任务已取消”，不显示“已由用户中断”或“运行失败”

### Requirement: 历史体验具备可重复的 stub 验收

项目 SHALL 提供隔离 workspace 的 deterministic stub integration/component/API tests，覆盖最新 1 个 Turn、around(anchor)、before/after 双向分页、活动统计折叠、当前 Turn 工具详情重载、慢历史请求和实时更新。测试 SHALL 验证 Composer 可用、最新 Turn 优先、分页完整且旧历史不丢失；本变更不要求真实模型或长时间真实浏览器 E2E。

#### Scenario: stub 历史加载验收

- **WHEN** 测试使用 stub API 执行会话切换、输入、向前分页和当前 Turn 工具详情切换
- **THEN** 测试通过返回的请求参数和可观察状态证明输入未被历史读取阻塞、around/before/after 按配置加载、Turn 完整且旧历史保留

### Requirement: thinking 与工具摘要默认折叠且不加载加密 payload

客户端 SHALL 将每个历史 Turn 的 `thinking_blocks`、tool summary 和活动统计渲染为始终存在的折叠行；折叠行显示耗时和隐藏消息统计，`reasoning` 块显示可读思考，`summary` 块显示安全摘要，`encrypted` 块只显示加密思考提示，不显示 provider payload。默认历史响应不得包含 provider encrypted reasoning。用户展开或请求工具详情时，客户端只能请求对应的受限投影，不得把其它 Turn 的完整 assistant/tool 内容一并解析。

#### Scenario: 128 Turn 混合历史

- **WHEN** 测试在 128 Turn 中混合普通 reasoning、Codex encrypted reasoning、tool_call/tool_result 和 final_response，并从 8011 逐级向前加载
- **THEN** 每个 Turn 的统计行默认折叠，around/before/after 默认只显示 user/final_response，encrypted payload 不出现在网络响应；点击一个 Turn 后只加载该 Turn 的中间详情，加载和锚点恢复达到性能预算。该测试读取复制后的确定性 mock 会话，不在运行时调用真实模型。
