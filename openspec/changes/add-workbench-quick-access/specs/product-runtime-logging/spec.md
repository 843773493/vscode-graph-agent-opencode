## ADDED Requirements

### Requirement: 运行日志查询沿原 Gateway 诊断来源

运行日志正文查询与精确定位 SHALL 复用原 Gateway diagnostics 的受认证管理入口、受管进程路径与来源发现，不读取工作区 `.boxteam/`，不新增正文副本、索引账本或日志服务。工作区范围 SHALL 仅选择与明确工作区关联的真实受管日志文件；共享 Backend 文件按实际来源去重，并披露关联的 `workspace_ids`，不得伪造逐工作区日志或声称其中每条正文只属于所选工作区。Gateway 自有来源 SHALL 单独明确选择，不因工作区查询、无命中或“日志”类别自动混入。未托管、未落盘、缺失与查询受限 SHALL 如实披露。

原始日志 SHALL 按物理行正序进行区分大小写的非重叠单行字面量匹配，保留第三方原始文本；去除行尾后的文本采用 UTF-8 替换解码，位置是零基、右端不含的 Unicode 码点区间。每页至多读取 64 个来源、512 行、8 MiB 正文，正文调度预算为一秒并服从请求剩余期限；单行硬上限为 256 KiB。达到限制 SHALL 返回真实扫描量及原因；单行超限不得提供无法推进的 cursor。只查询当前原日志文件，不扫描轮转归档；来源枚举沿原 diagnostics 上限并披露覆盖。

续点 SHALL 绑定 Gateway、明确来源范围、原查询、来源集合、当前文件观察版本与行内位置。文件追加、裁剪、轮转、改写或范围变化 SHALL 明确拒绝旧续点；文件观察元数据不冒充整份文件内容摘要。命中 SHALL 携带原来源 ID、文件身份、原行字节区间、完整原行版本和独立码点位置。精确读取 SHALL 仅核对目标区间及必要边界，不重复扫描或全量计算摘要；追加后的未改完整行仍可定位，轮转、删除、行改写或边界变化明确失效，不回退到 tail 或另一来源。取消等待同步读取结算，不遗留后台读取任务。

查询与定位 SHALL 保持原本机私有凭据及远端联邦管理权限；运行日志命中只用于原诊断视图的具名导航动作，不作为普通文件资源、聊天历史或上下文引用发布。此边界引用统一寻址 owner 的“Gateway 原始诊断日志管理入口保留原文”，不新增 URI kind 或日志资源 parser。

#### Scenario: 明确工作区和 Gateway 来源分别查询

- **WHEN** 用户只选择工作区 A 的受管来源，或单独选择 Gateway 自有来源
- **THEN** 前者只枚举与 A 关联的真实进程文件并标明共享归属，后者只查询原 Gateway 来源；A、B 共用的 Backend 只读取一份，不自动加入另一类来源

#### Scenario: 分页与精确行定位具有不同版本边界

- **WHEN** 查询后文件追加、轮转，或原行被同长度改写
- **THEN** 追加使旧续点失效但未改完整行仍可精确定位；轮转或原行改写使旧命中明确失效，不读取整份文件重新猜测位置

### Requirement: Trace 正文查询复用原事件存储

工作区搜索 SHALL 提供独立 `trace/content` 类别，由原 TraceEventStore 在 catalog 中发现 Session，复用原 JSONL 分页、事件解析、文件锁及 Job／Thread 归属索引。查询不读取 Gateway 自有运行日志，不触发执行恢复，不新增日志副本或持久搜索索引。匹配采用区分大小写的非重叠字面量，正文是原事件按现有 schema 字段顺序、两空格缩进且保留 Unicode 的 JSON；位置使用从零开始、右端不包含的 Unicode 码点区间。每个 Session 从最新完整事件向前读取，单事件内按正文位置正序返回。

每页 SHALL 最多检查 64 个目录节点、128 条事件、读取 8 MiB 正文窗口，正文调度预算为一秒，并服从请求剩余总期限；沿用原 Trace 页的 512 KiB 单窗口／单事件读取上限。达到预算 SHALL 披露扫描量、原因和适用续点；无法跨过的单事件超限不返回循环 cursor。阻塞读取在取消后 SHALL 先结算，再释放 Session 读准入。

续页 SHALL 绑定 Gateway、workspace、原查询、catalog revision、当前 Session 的文件观察版本及事件／正文位置。文件追加、删除或观察版本变化时明确拒绝；同一事件内继续时额外核对该行完整内容版本。文件观察版本不是整份历史的内容摘要，不承诺检测人为保留文件元数据的任意修改。各 Session 使用其实际读取时的快照，已完成 Session 的新增事件需重新查询。

命中 SHALL 使用原 Session 资源 URI，独立携带 Session、实际 Thread、event ID、原行字节区间、完整行内容版本及正文码点位置。实际 Thread 来自原已提交 Job 事实，Goal 沿原合同归 main，不从裸 ID 或展示路径猜测。精确定位 SHALL 在原 Session 读准入和文件锁内只读该行，核验完整内容版本、事件、Thread 及位置，返回原 Trace 详情及临时搜索原文。删除、裁剪、改写、错误归属或越界 SHALL 明确拒绝，不改投最新事件或会话顶部；该定位不要求整份日志保持未追加。

续点错误 SHALL 保留 `owner=trace_event_store`、`code`、`reason` 和原详细消息：输入错误使用 HTTP 400 `invalid_trace_search_cursor`，版本变化使用 HTTP 409 `stale_trace_search_cursor`，旧命中引用使用 HTTP 409 `stale_trace_reference`。Gateway 沿原分支保留原 HTTP 状态、输入续点及本次 request_id，不解析提示文字推断原因。

#### Scenario: 未打开 Thread 的原事件命中

- **WHEN** 用户明确选择 Trace 类别，查询尚未打开 Thread 的原事件正文
- **THEN** 原 owner 返回精确事件与实际 Thread，打开时只读取目标记录并按码点高亮；未选日志来源不参与查询

#### Scenario: 搜索续页与精确定位采用不同版本边界

- **WHEN** 搜索后原日志追加了事件，或者目标行被同长度改写
- **THEN** 追加使旧搜索续点明确失效，但未变化的目标行仍可精确打开；改写使旧目标引用失效，不用首尾采样摘要冒充完整内容版本

### Requirement: LLM 请求日志有界搜索与原记录定位

工作区搜索 SHALL 由原请求日志 owner 按 catalog 中的 Session 有界查询已保留的 request、response、upstream 正文。请求记录保持现有 Session 归属，不推断 Thread，不作为聊天消息重复返回。正文采用固定字段顺序、两空格缩进、保留 Unicode 的 JSON 文本，匹配为区分大小写的非重叠字面量；位置采用从零开始、右端不包含的 Unicode 码点区间。搜索不新增正文副本或持久索引，不改变原日志留存规则。

每页 SHALL 至多检查 64 个目录节点、128 条记录、读取 8 MiB 正文，正文调度预算为一秒，并服从请求剩余总期限。单条记录硬上限为 1 MiB；单个日志目录至多枚举 512 个 JSON 文件。达到预算 SHALL 披露扫描量、受限原因和可用续点；无法推进的单项或目录超限 SHALL 不提供循环 cursor。一次同步读取不能被强行中断，取消后必须等待 worker 结算再释放 Session 读准入。

续页 SHALL 绑定 Gateway、工作区、原查询、catalog revision、当前 Session 日志目录版本及正文位置，目录增删或当前记录变更时明确拒绝。各 Session 是其实际读取时的快照，已完成 Session 新增日志需重新查询。

命中 SHALL 使用原 Session 资源 URI，并独立携带 Session ID、日志文件名、正文版本和位置。精确读取 SHALL 在原 Session 读准入内只读取目标记录，核验归属、版本及位置后返回原详情与搜索文本；删除、留存裁剪或改写 SHALL 返回命中失效，不退回最新记录或 Session 顶部。客户端沿原请求详情展示临时原文并高亮，关闭或切换定位即释放，不拉全量日志或写入聊天历史。

#### Scenario: 请求独有正文命中

- **WHEN** 用户明确选择 LLM 请求类别，查询未打开 Session 中只存在于模型请求正文的词
- **THEN** 原 owner 返回精确 Session／记录／位置，打开后只加载该记录，展示原请求详情及命中原文，不查询未选类别

#### Scenario: 旧记录和续点失效

- **WHEN** 原记录已被改写、删除或裁剪，或续页的目录版本已变化
- **THEN** owner 明确拒绝旧引用或 cursor，不改投另一条记录；普通原请求日志列表继续沿同一解码实现读取

### Requirement: 请求日志查询拒绝保留机器原因

原日志 owner SHALL 统一发布续点与记录引用错误，携带 `owner=llm_request_log`、`code`、`reason` 和原详细消息。格式／结构错误、归属／查询不符及越界续点 SHALL 返回 HTTP 400 `invalid_request_log_cursor`；目录或正文版本变化 SHALL 返回 HTTP 409 `stale_request_log_cursor`。精确定位已删除、变化、越界或超限的原记录 SHALL 保留 HTTP 409 `stale_request_log_reference`。原读取权限、取消和内部错误不改标为续点失效。

原因 SHALL 由实际校验点产生，区分 `malformed`、`owner_mismatch`、`query_mismatch`、`position_out_of_bounds`、`catalog_changed`、`directory_deleted`、`directory_changed`、`record_deleted`、`record_changed` 和 `record_size_limit` 的适用情况。Gateway SHALL 沿原分支传递这些字段、真实 HTTP 状态及原输入 cursor，零页交付时不推进续点，不解析消息文字推断原因。

#### Scenario: 无效输入与过期版本可区分

- **WHEN** 同一请求日志搜索分别使用损坏续点、另一查询的续点或已变化日志的续点
- **THEN** 原 API 与联邦分支返回对应机器代码和原因，保留本次 request_id、原详细消息及输入续点，不报告完整零匹配
