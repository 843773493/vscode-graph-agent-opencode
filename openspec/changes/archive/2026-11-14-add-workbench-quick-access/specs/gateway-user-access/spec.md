## ADDED Requirements

### Requirement: 本机自动化搜索复用同一联邦处理链

Gateway SHALL 为本机 Agent 与 Python 脚本提供具名只读入口 `/api/gateway/search/agent`，使用既有本机私有凭据认证，不依赖浏览器、用户视图或访问租约，不自动创建游客或接管用户。该入口与浏览器搜索 SHALL 使用相同请求、事件、范围解析、Provider、调度、预算及输出编码实现，不建立第二套搜索或跨目标队列。普通浏览器 `/api/gateway/search` SHALL 使用同一既有本机或联邦凭据认证，不要求用户访问租约；本机入口的凭据授权 MUST NOT 被扩大为豁免其它工作区 API 的访问校验。

#### Scenario: 关闭浏览器后执行工具搜索

- **WHEN** 已授权后台 Agent 或持有本机私有凭据的脚本在没有浏览器 Cookie 时查询明确工作区集合
- **THEN** 通过具名本机入口返回同一 typed 搜索流及独立续点，只查询选择的目标，不创建用户访问会话

#### Scenario: 普通浏览器与本机入口校验既有凭据

- **WHEN** 普通搜索或本机入口缺少有效访问凭据
- **THEN** 明确拒绝，不创建游客或访问租约，原目标和联邦权限检查保持
