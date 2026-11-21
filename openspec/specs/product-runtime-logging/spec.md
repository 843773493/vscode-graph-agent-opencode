# product-runtime-logging Specification

## Purpose
统一本地产品各语言运行进程的诊断日志配置、来源标识、路径与文件所有权，使运行中的等级调整可被真实确认，并保持命令原始输出及会话业务数据的既有归属与读取行为。
## Requirements
### Requirement: 复用既有监督体系管理诊断日志

系统 SHALL 复用 Launcher/Gateway 的进程监督职责管理日志输出，不新增独立日志服务进程或逐条日志的远程采集依赖。诊断输出 MUST 不改变既有服务存活和重启交接语义。

#### Scenario: 完整产品启动
- **WHEN** 用户通过统一 Launcher 启动产品
- **THEN** 日志功能不新增 daemon、监听端口或额外启动依赖，已受管进程按统一合同输出

### Requirement: 本地产品诊断日志使用统一合同

系统 SHALL 为 Launcher、Gateway、Backend、Terminal 和 Browser 服务端提供一致的日志等级与文本格式语义。产品记录 MUST 包含 UTC 时间、等级、服务、PID、运行实例标识和消息，存在真实请求或业务上下文时附带对应关联标识，不得伪造缺失身份。异常 MUST 保留可定位的堆栈。第三方原始输出 MUST 保留原文，不得伪称已规范化。

#### Scenario: Python 与 Node 输出同等级诊断
- **WHEN** Backend 与 Terminal 服务使用相同 level 和 pretty 设置产生诊断
- **THEN** 二者遵循相同过滤规则、字段命名和格式语义，并可区分实际服务与运行实例

#### Scenario: 无工作区上下文的共享 Backend 日志
- **WHEN** 挂载多个工作区的 Backend 输出进程级诊断
- **THEN** 日志不随意选取任一 workspace_id，也不为每个工作区复制同一记录

### Requirement: 产品进程日志由唯一启动 owner 分配路径

默认产品诊断日志 MUST 位于 `${BOXTEAM_HOME}/state/logs/`，由启动 owner 按服务角色和端口分配文件；业务服务 MUST NOT 自行拼接诊断文件路径。同一路径 MUST 只有一个落盘与轮转 owner，前台 tee 或 detached 捕获不得重复落盘。已有显式 Launcher 日志路径覆盖 SHALL 保持有效并展示来源。远端 Gateway SHALL 在远端本地保存诊断日志。

#### Scenario: Gateway 托管 Python 和 Node 服务
- **WHEN** Gateway 启动 Backend、Terminal 和 Browser
- **THEN** 三者获得按统一合同分配的日志去向，结构化诊断可查日志入口 ID、文件身份、服务、运行实例与 owner；原生路径可作为明确目标机器的诊断信息，不成为资源关联的第二权威定位关系；凭据与秘密保护保持，业务进程无需读取文件布局配置

#### Scenario: detached 输出已经被捕获
- **WHEN** 后续启动环节处理已由上游捕获的输出
- **THEN** 同一条输出在目标文件中仅写入一次

#### Scenario: 用户指定 Launcher 日志文件
- **WHEN** 用户显式设置已有 Launcher 日志路径覆盖
- **THEN** Launcher 使用该路径，诊断显示其覆盖来源，其余进程仍由各自启动 owner 分配

### Requirement: 日志热更新按真实运行实例确认

用户修改有效 `logging.runtime.level` 或 `logging.runtime.pretty` 后，系统 SHALL 在不重启服务、不中断业务的情况下协调全部本机受管产品进程应用。系统 MUST 按进程实例去重并核实 revision 与实际设置，全部必要确认成功后才发布配置成功。失败 MUST 保留旧 active 并补偿已应用部分；无法恢复时报告 `recovery_required` 和逐进程实际状态。日志管理入口 MUST 使用既有本机认证边界。

#### Scenario: 多工作区共用 Backend 时调整等级
- **WHEN** 用户从 INFO 调到 DEBUG，再调回 WARNING
- **THEN** 共享 Backend 每次只应用一次，Launcher、Gateway 和两个 Node 服务确认对应设置，后续日志遵循新阈值，进程及活动业务资源保持

#### Scenario: 一项应用失败或最终 promotion 冲突
- **WHEN** 某个进程拒绝候选，或所有确认后最终配置 CAS 失败
- **THEN** 系统不报告全产品已生效，恢复已修改的进程；补偿失败时暴露实际差异和恢复状态

#### Scenario: 进程在应用期间被替换
- **WHEN** 原目标进程退出且新实例使用相同端口启动
- **THEN** 旧实例迟到确认不能计为新实例确认，新实例在 ready 前取得当前有效设置

### Requirement: 原始输出与业务日志保持独立

日志等级和格式设置 MUST 仅作用于产品诊断。系统 MUST NOT 根据产品日志等级过滤或重写 PTY 命令输出、网页 console 或调试目标输出，MUST NOT 将会话 Trace、LLM 请求正文、检查点和工具结果搬到全局日志目录，MUST NOT 因开启 DEBUG 自动记录密钥或认证材料。

#### Scenario: WARNING 等级运行打印 DEBUG 字样的命令
- **WHEN** 产品日志等级为 WARNING，Shell 命令输出普通文本、ANSI、中文和包含 DEBUG 的行
- **THEN** 模型与用户仍读取到原始命令输出及真实退出码，产品过滤不改变内容

#### Scenario: 会话产生专用业务记录
- **WHEN** Agent 执行产生 Trace、LLM 请求和工具结果
- **THEN** 它们继续写入所属工作区与会话的权威存储，产品诊断仅附带必要关联标识

### Requirement: 文件轮转与故障透明

日志文件打开时达到 64 MiB SHALL 按统一规则轮转并最多保留三份归档；此规则 MUST NOT 被描述为运行中文件大小硬上限。活动写入者未排空时 MUST NOT 重开轮转同一路径；Gateway 交接复用存活子进程时 MUST 保留其有效日志去向。打开、轮转或写入失败 MUST 通过 stderr 或控制响应显式暴露，不得静默丢弃或递归写入失败 sink。

#### Scenario: 新实例打开已达阈值的旧文件
- **WHEN** 旧写入者已退出且日志文件达到轮转阈值
- **THEN** owner 按一致的归档顺序轮转再打开，Python/JS 行为一致

#### Scenario: Gateway 交接而 Backend 存活
- **WHEN** 新 Gateway 接管仍在运行的 Backend
- **THEN** Backend 可继续输出至原去向，新 owner 不轮转其活动文件，也不要求为日志重启 Backend

#### Scenario: 磁盘写入失败
- **WHEN** 日志路径不可写或输出失败
- **THEN** 对应启动或运行操作明确报告文件和阶段，不能显示日志已正常持久化

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

工作区搜索 SHALL 提供独立 `trace/content` 类别，由原 TraceEventStore 在 catalog 中发现 Session，复用其唯一 SQLite 事件正文、分页、解析和 Job／Thread 归属索引；一次读取在原数据库事务中取得一致快照。查询不读取 Gateway 自有运行日志，不触发执行恢复，不新增日志副本或持久搜索索引。匹配采用区分大小写的非重叠字面量，正文是原事件按现有 schema 字段顺序、两空格缩进且保留 Unicode 的 JSON；位置使用从零开始、右端不包含的 Unicode 码点区间。每个 Session 从最新完整事件向前读取，单事件内按正文位置正序返回。

每页 SHALL 最多检查 64 个目录节点、128 条事件、读取 8 MiB 正文窗口，正文调度预算为一秒，并服从请求剩余总期限；沿用原 Trace 页的 512 KiB 单窗口／单事件读取上限。达到预算 SHALL 披露扫描量、原因和适用续点；无法跨过的单事件超限不返回循环 cursor。阻塞读取在取消后 SHALL 先结算，再释放 Session 读准入。

续页 SHALL 绑定 Gateway、workspace、原查询、catalog revision、当前 Session 的 Trace 存储身份、提交 revision 及事件／正文位置。事件追加、删除、改写或存储替换导致版本变化时明确拒绝；同一事件内继续时额外核对该条完整内容版本。存储 revision 由原数据库维护，不使用文件 mtime／size 或整份历史摘要代替。各 Session 使用其实际读取时的快照，已完成 Session 的新增事件需重新查询。

命中 SHALL 使用原 Session 资源 URI，独立携带 Session、实际 Thread、event ID、原记录的逻辑字节区间、完整记录内容版本及正文码点位置；逻辑区间由原 Trace owner 发布，不是 SQLite 文件物理偏移。实际 Thread 来自原已提交 Job 事实，Goal 沿原合同归 main，不从裸 ID 或展示路径猜测。精确定位 SHALL 在原 Session 读准入和数据库读取事务内只读目标记录，核验完整内容版本、事件、Thread 及位置，返回原 Trace 详情及临时搜索原文。删除、裁剪、改写、错误归属或越界 SHALL 明确拒绝，不改投最新事件或会话顶部；该定位不要求其它事件保持未追加。

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

