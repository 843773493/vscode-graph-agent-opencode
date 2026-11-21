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

## ADDED Requirements

### Requirement: 插件热应用必须展示实际实例与配置 revision

Gateway 配置诊断 SHALL 区分配置来源已保存、active 配置及每个受管插件实际应用的 revision／plugin_id／instance_id。日志、遥测和统计的应用失败 SHALL 返回原实例、阶段及具体错误；部分成功不得伪报全成功，也不要求跨服务事务回滚或通过重启强迫应用。实例重启后 SHALL 从当前已确认设置初始化，不恢复已关闭的采样定时器。

#### Scenario: 一项诊断配置部分应用失败
- **WHEN** 同一热更新在 Terminal 已应用，在 Browser 明确失败，MCP 尚未就绪
- **THEN** 诊断分别报告三者实际状态，不假报采集已经全部关闭，不创建第二配置事实或把未就绪服务读查询当作启动指令

### Requirement: 插件启用期望与运行回执必须分别报告

插件配置提交 SHALL 返回原来源／候选及 enabled，运行 owner 分别报告精确插件和实例实际状态；启动或停止失败不伪报配置回滚／运行成功。旧配置 revision 的 ready／停止回执不得覆盖新期望，开关和重启串行且原进程退出核实前不重复启动。跨 owner 版本切换 SHALL 先预校验，再沿原 CAS 提交期望、撤新准入、停旧核退出／启新、按固定引用顺序发布；首次失败停止后续推进，保留各 owner 实际版本、阶段和部分结果，不跨 owner 补偿或自动重试。预校验失败保留旧发布，实际登记失败只清本次新增句柄，不重建已释放句柄；残留旧贡献只按旧版本诊断，版本不匹配的新调用不可用。期望提交后不自动回滚，原 active／pending 仍按各域既有 promotion 合同报告，不把期望当已生效或增加汇总账本／新恢复状态机。普通参数热应用不能隐式开启插件；需要重启的参数按原生效分类提示显式插件重启，不重启兄弟插件或 Backend。

#### Scenario: 已保存开启但启动失败
- **WHEN** enabled=true 已提交，插件入口启动失败
- **THEN** 配置保持已开启、实际状态 failed 并显示原原因，其他插件可用；用户显式重启该绑定才重试，普通工具和读取不重试启动

#### Scenario: 切版中已退出旧实例而新实例失败
- **WHEN** 新版本期望已提交且旧实例退出已核实，新实例启动失败
- **THEN** 诊断分别显示已提交期望、原 active／pending 事实和运行失败阶段；不自动恢复旧进程或重写来源，用户显式重启重新应用，选择旧版本须另作显式配置提交
