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
- **THEN** 三者获得按统一合同分配的日志去向，结构化诊断可查日志入口 ID、文件身份、服务、运行实例与 owner；真实路径仅限既有具名原始日志/堆栈管理入口，不进入结构化诊断或持久应用账本，业务进程无需读取文件布局配置

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

