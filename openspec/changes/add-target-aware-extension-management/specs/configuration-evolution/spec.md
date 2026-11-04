## MODIFIED Requirements

### Requirement: 配置诊断必须揭示真实来源和应用状态

配置诊断接口 SHALL（必须）同时报告 JSONC 来源摘要、SQLite 记录摘要、当前有效来源、文件/记录 revision 或 digest、active/pending 状态及最近失败原因。来源正文编辑的结果 SHALL 分别报告文件保存、SQLite 层物化和 active／pending／最近失败事实，返回原候选及幂等提交身份；文件写入成功不得被表述为全部资源已生效。诊断接口的重新读取 MUST（必须）只用于观测，不能绕过正常候选提交流程改变运行时配置。

#### Scenario: JSONC 已迁移到 SQLite
- **WHEN** 用户查询 Gateway 或 Workspace 配置诊断
- **THEN** 响应明确显示 SQLite 为当前有效可变来源、JSONC 的最近导入摘要以及是否存在待应用文件变化

#### Scenario: 候选失败后查询状态
- **WHEN** 最近一次配置候选因为 schema 或业务预检失败
- **THEN** 诊断响应保留旧 active revision，并返回失败来源、路径和错误，不返回伪造的成功状态

#### Scenario: 来源文件保存后候选等待重启
- **WHEN** 显式来源保存成功，而有效变化包含原生效策略要求重启的字段
- **THEN** 结果显示来源已保存、原 active 保留及实际 pending／restart required；重读或同幂等请求不得伪造 promotion、重复副作用或另一逻辑提交
