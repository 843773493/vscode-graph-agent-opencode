# responsive-session-composer Specification

## Purpose
TBD - created by archiving change optimize-long-session-turn-loading. Update Purpose after archive.
## Requirements
### Requirement: Composer 不等待会话历史
用户切换到已有会话时，系统 SHALL 保持 Composer 挂载并立即允许编辑；历史 bootstrap、Turn 详情、Trace 和 Markdown 渲染不得作为输入可用性的前置条件。

#### Scenario: 历史接口被延迟
- **WHEN** 用户切换会话且 bootstrap 与详情接口被人为延迟
- **THEN** 用户仍可立即聚焦 Composer、输入和编辑文本

### Requirement: 草稿按会话同步恢复
Composer SHALL 在会话 scope 切换时同步读取对应 workspace/session 草稿，且 MUST NOT 短暂展示上一会话草稿。

#### Scenario: 两个会话具有不同草稿
- **WHEN** 用户从会话 A 切换到会话 B
- **THEN** Composer 第一次可见提交即展示 B 的草稿，不展示 A 的草稿

### Requirement: 时间线更新不触发 Composer 重渲染
Composer SHALL 只订阅输入、附件、发送控制和当前 scope 所需状态；历史分页、Trace、文本流和 Markdown 水合变化 MUST NOT 改变 Composer 的订阅快照。

#### Scenario: Assistant 持续流式输出
- **WHEN** 当前 Turn 连续产生文本 delta
- **THEN** Composer 输入值、选区和输入法组合状态保持不变，且 Composer 不因每个 delta 重渲染

### Requirement: 加载竞态不覆盖用户的新发送
系统 SHALL 以会话选择 generation、请求取消和 Turn revision 合并处理切换竞态；用户在历史加载完成前发送的新 Job MUST 保留。

#### Scenario: 用户在 bootstrap 返回前发送
- **WHEN** 用户切换会话后立即发送消息，随后旧 bootstrap 响应到达
- **THEN** 新 Job Turn 保留且旧响应不得替换或删除它

#### Scenario: 同一 Turn 在详情请求期间连续失效
- **WHEN** bootstrap、SSE 或终态协调在一个详情请求尚未完成时连续报告更高 revision
- **THEN** 客户端取消错误 scope 的请求，并对当前 scope 持续执行 trailing 详情请求，直到满足最后一次失效 generation

### Requirement: 错误不禁用独立输入
历史或 Turn 投影读取失败时，系统 SHALL 清晰展示时间线错误并允许用户重试；若后端仍接受该会话的新 Job，Composer MUST 保持可用。

#### Scenario: 历史投影损坏
- **WHEN** bootstrap 返回可诊断的投影错误
- **THEN** 时间线展示该错误和重试入口，Composer 不伪装历史成功也不无关地丢失草稿

