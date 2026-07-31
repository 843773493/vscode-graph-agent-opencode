## ADDED Requirements

### Requirement: 最新 Turn 先于旧历史完整呈现
客户端 SHALL 在会话切换时先呈现最新 Turn summary，并立即请求其 full detail；旧历史页和完整 Trace 不得阻塞这一过程。

#### Scenario: 切换到长会话
- **WHEN** bootstrap 返回最新 Turn summary
- **THEN** 最新用户输入和 Job 状态先显示，随后同一 Turn 被 full detail 原位水合

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

### Requirement: 长历史体验具备可重复验收
项目 SHALL 提供隔离工作区 E2E，覆盖大量完整 Turn、大型 Markdown、慢 bootstrap、向前分页、实时更新和上下文压缩，并 SHALL 验证 Composer 先可用、最新 Turn 优先和 cursor 稳定。

#### Scenario: 真实浏览器长会话验收
- **WHEN** E2E 在包含大量 Markdown Turn 的会话中执行切换、输入、分页和实时完成
- **THEN** 测试以可观察 UI 和请求断言证明输入未被阻塞、最新 Turn 先出现、分页完整且旧历史未丢失
