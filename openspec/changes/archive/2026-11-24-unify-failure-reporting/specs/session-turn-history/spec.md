## ADDED Requirements

### Requirement: 历史失败原因来自原执行终态而不是展示猜测

Turn 历史 summary 与 detail SHALL 以原 rollout convergence 的终态提交为唯一失败来源，failure、execution／Turn 状态和 history revision 在同一原事务提交；消息流仅为传输镜像，不作为历史权威来源。读取 SHALL 沿实际 view／revision 与 terminal execution 的原提交关联定位，精确绑定原 SessionThread、Turn 和执行，不扫描全部 commits 猜最后一次错误。已经接受但尚未产生模型／工具 Item 的失败也必须保留原因。失败 metadata MUST NOT 创建虚构消息、改变 Item 数量或依赖展开详情。仅有状态而没有原因的历史 SHALL 明确表示未记录详情，不猜测工具、Provider 或当前配置原因。读取 MUST NOT 扫描 Trace、日志或全历史进行补写／重建，且须服从原 cursor、字节及延迟预算。

默认紧凑摘要 SHALL 仅为原 failure 的显示投影，不写回 summary／detail、前端缓存或消息流中的失败字段。刷新后 SHALL 仍可沿原按需诊断入口读取／复制已经交付的安全原因及关联，不依赖旧 JS Error／cause、展开 Item 或日志尚未轮转；日志不可用只影响日志导航。原公开预算及截断标记保持，不承诺还原未保存文本，不增加失败详情 API、额外存储／迁移或扫描补读。

#### Scenario: Agent 初始化失败且 Item 为零
- **WHEN** 用户经真实发送入口接受 Turn 后，Agent 工具选项校验失败
- **THEN** 实时和刷新后的 summary／detail 均显示原校验原因及真实归属，Item 保持 0，不显示“后端没有提供可用的失败详情”替代已存在原因

#### Scenario: 原历史没有保存原因
- **WHEN** 原权威记录只有失败／未知状态而没有失败原因
- **THEN** 页面如实显示该记录未保存详情，不伪造失败来源，不读相邻事件补齐，不阻断其它可读取 Turn

#### Scenario: 分页和详情展开后错误保持一致
- **WHEN** 用户读取摘要、展开详情、离开后再按同一历史快照定位失败 Turn
- **THEN** 各入口保留同一已记录原因与归属，正文裁剪不删除失败 metadata，也不把另一个执行的错误附到该 Turn

#### Scenario: 刷新后原日志已经不可用
- **WHEN** 已保存失败原因的 Turn 先以紧凑摘要展示，刷新后原日志已轮转或离线
- **THEN** 用户仍可按需读取／复制 summary／detail 中原预算内的安全原因及关联，原 failure 不变，不要求展开 Item、不用旧内存异常或扫描日志重建；日志动作如实表达不可用

### Requirement: attempt 失败不得替代 Turn 的权威结果

模型或工具 attempt 的失败 SHALL 继续保存在原 attempt 明细；只有原 convergence 确认 Turn 失败时才投影 Turn.failure。成功终态 MUST NOT 因较早失败事件而带上整轮 failure。显式恢复产生新 execution 后，旧失败 SHALL 留在原执行终态提交，当前投影按实际历史快照选择 execution，新活动或成功不得继承旧失败。模型回退、工具继续执行及恢复决策 MUST 保持原规则。

#### Scenario: 模型回退成功
- **WHEN** 模型 A 失败，原回退链选择模型 B 且 Turn 最终成功
- **THEN** A 的失败可在 attempt 明细中查看，Turn 保持成功且不带 failure，刷新后仍不显示整轮失败

#### Scenario: 工具错误后继续完成
- **WHEN** 工具返回 isError，Agent 继续执行并成功完成 Turn
- **THEN** 工具调用保留原错误，summary／detail 不把工具错误投影为 Turn.failure

#### Scenario: 失败执行被显式恢复
- **WHEN** 失败后按原规则创建新 execution，并分别读取旧历史快照和新活动／成功快照
- **THEN** 旧快照保留旧 failure，新快照使用实际所选 execution 的结果，不从最近错误事件继承旧 failure
