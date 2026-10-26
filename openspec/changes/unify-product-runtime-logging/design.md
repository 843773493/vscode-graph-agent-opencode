## Context

动机见 proposal.md。当前 Backend 从 Workspace 配置读取 `logger`，Gateway 仅使用 Python 日志初始化默认值，Node Terminal/Browser 直接调用 console。`packages/launcher/src/logging/service-log.mjs` 与 `app/gateway/runtime/process_logs.py` 各自实现路径与打开时轮转，后者将托管进程的 stdout/stderr 直接接到文件句柄。一个 Backend 已能挂载多个工作区，日志配置却仍来自各工作区，启动时只能要求它们一致。

现有配置流水线已有 candidate、apply journal、consumer proof、promotion 和补偿机制。本设计复用它们，不为日志另建配置事实源，也不改变 Gateway 不读取工作区业务数据的边界。

## Goals / Non-Goals

**Goals:**

- 一个用户级控制面配置覆盖本机受管产品服务，热调等级与格式不打断 Agent、MCP、PTY 或浏览器实例。
- 每个运行实例的日志去向、当前配置 revision、应用结果都能查明；Python/JS 对相同合同产生一致语义。
- 一个文件只有一个落盘路径和一个轮转 owner，保留现有进程交接与文件描述符生命周期。

**Non-Goals:**

- 不新增日志 daemon、数据库、消息队列、远程日志平台或日志搜索 UI。
- 不过滤/重写 Shell 命令输出、网页 console、调试目标 stdout，不搬迁会话 Trace、LLM 请求或工具结果。
- 不实现 MCP 配置热切换，不替换现有 Job/Thread 调度体系，不声称第三方库的原始文本都具备结构化字段。
- 不在此次改动中加入运行中文件大小硬上限、在线压缩和历史文件清理服务。

## Decisions

### 1. 产品诊断日志配置归 Gateway 域

新配置为 `logging.runtime.level` 和 `logging.runtime.pretty`，默认分别为 `info`、`true`。等级只接受 `debug/info/warning/error/critical`，规范化大小写；`pretty` 是严格布尔值。用户级 Gateway 和 Gateway local 层仍按现有顺序合并。Workspace 配置不再提供 logger，也不允许覆盖该 Gateway 配置。

等级表示日志生产端的阈值；DEBUG 以下曾被过滤的内容不能事后补回。`pretty` 只影响后续产品诊断日志的格式，不改变数据归属、过滤规则和业务输出。

选择 Gateway 域是因为控制对象包含 Launcher、Gateway、共享 Backend 与 Node 辅助进程。继续由各 Workspace 提供配置会造成共享进程互相覆盖；新增第三套 logging.jsonc 则增加来源、watcher 和迁移链，故不采用。

配置版本分别从实施基线的当前版本递增一次，不能用软件版本替代；如果实施时仍为 v1，则均迁为 v2。跨域旧 `logger` 的迁移只在显式安装迁移入口处理，不在在线重载中写其它域。

### 2. 统一合同，保留各语言正常日志工具

Python 继续使用标准 logging，JS 使用一个共享服务端日志入口；实施时优先复用已安装库，无合适依赖时才评估引入成熟库。不得让每个 Node 服务继续自建等级或格式判断。

规范化记录包含 UTC `timestamp`、`level`、`service`、`pid`、`runtime_instance_id`、`message`，以及真实存在时的 `workspace_id/session_id/thread_id/request_id` 和异常堆栈。缺失关联字段不伪造，不枚举多个工作区后随意选择一个。共享 Backend 的进程日志不强行绑定单个工作区。

最终进程日志保持 UTF-8 文本流：pretty 模式采用易读前缀，紧凑模式保留相同身份与时间字段并减少装饰；关联字段用一致命名的文本字段表达。不将现有日志文件改成只接受 JSONL，也不通过猜测 console 文本决定等级。多行异常保持完整内容。Uvicorn 等已有 Python logger 显式接入统一 handler/等级策略并消除重复传播；无法控制的第三方 stdout/stderr 原样保留，并在诊断中标明是 raw output。

API key、认证头和模型请求正文不因 DEBUG 自动加入产品日志；详细业务内容继续使用既有专用存储与访问入口。

### 3. 路径由启动 owner 分配，业务进程不计算诊断文件路径

默认根统一为 `${BOXTEAM_HOME}/state/logs/`。其下按角色和端口使用稳定文件名：`launcher/services.log`、`gateway/gateway.log`、`backend/local-backend-<port>.log`、`terminal/local-terminal-<port>.log`、`browser/local-browser-<port>.log`；SSH 转发及开发入口也使用同一角色命名合同。进程记录携带独立 `runtime_instance_id`，避免把端口当成运行实例身份。

Launcher 决定自身与 Gateway 的去向；Gateway 为本机托管子进程分配去向并传递已打开句柄，Backend/Terminal/Browser 仅向 stdout/stderr 输出。正常前台输出允许 tee 到终端，但每个日志文件只落盘一次。detached 入口已经接管的流必须有显式所有权信息，后续 owner 不得再 tee 回同一文件。

路径公式、默认大小与保留数量只登记一次可供安装包携带的纯合同资源，Python/JS 读取相同资源；各语言只保留必要的文件 API 实现，并用相同样本验证。移除原模块中的重复常量、旧默认目录计算和私有路径拼接。已有 `BOXTEAM_SERVICE_LOG_PATH` 作为显式 Launcher sink 覆盖继续生效，诊断展示覆盖来源和绝对路径，不扩展为各服务自行选目录的能力。

远端 Gateway 在远端本机按其自身 home 落盘；不把远端流回传本地集中存储。显式开发/单服务入口也必须加载同一合同，未启用文件托管时明确报告 stdout-only，不隐式创建另一套文件。

### 4. 不增加日志进程，也不为统一而改变子进程存活合同

维持现有 Launcher 监督 Gateway、Gateway 托管服务的结构。Launcher 管理 Gateway 的管道输出；托管子进程继续使用 Gateway 分配的文件描述符。日志打开与轮转是启动 owner 的职责，不为每条日志引入 HTTP/IPC 请求，也不要求 Backend 等待 Gateway 才能输出一行日志。

轮转保持打开时检查语义：既有文件达到 64 MiB 时，按 `.1` 至 `.3` 顺序轮转后打开，新文件运行期间不保证小于该阈值。打开/轮转同一路径前必须确认没有仍存活的写入者；Gateway 重启后若复用存活子进程，沿用原日志与 owner 记录，不重新轮转其活动文件。对无法证明的所有权冲突明确失败。

统一输出格式不要求所有进程共用一个文件。单个大文件会混淆归属并引入多进程轮转冲突；独立 daemon 则增加启动依赖与故障路径，当前不采用。

### 5. 热更新由 Gateway 按真实进程协调

日志是现有 Gateway candidate 的一个 runtime consumer。Gateway prepare 阶段冻结本机受管进程集合，按进程实例去重；向 Launcher 使用既有监督控制通道，向 Backend/Terminal/Browser 使用受已有本机认证保护的内部管理通道下发 `revision + instance_id + level + pretty`。不得新增公开无认证的日志级别接口。

每个进程校验身份与参数，幂等应用并返回真实 `instance_id/revision/effective settings`；Backend 只应用一次，不按挂载工作区重复应用。准备启动的新进程必须在 readiness 前取得当前有效设置；应用期间的进程退出/新增由同一生命周期 owner 串行结算，不能接受已退出旧 PID 的迟到确认。

全部目标确认后才允许既有 active promotion；单个进程失败、超时或最终 CAS 冲突时，沿原 apply journal 恢复已改变的消费者并核实结果。补偿失败进入 `recovery_required` 并显示逐进程状态，不把一次跨进程更新宣称为同时原子切换。日志故障本身优先通过 stderr 与控制响应暴露，避免向同一失败 sink 无限递归记录错误。

Launcher 初始启动在 Gateway 验证配置前可使用内置 bootstrap 设置，必须标记该阶段；ready 前切到 Gateway 确认的有效设置。此暂态不是第二套可变配置来源。

### 6. 跨域迁移显式处理差异，不静默选值

显式迁移工具读取用户级 Workspace/Workspace local 的旧 logger，并可接收明确指定的工作区集合以检查旧工作区覆盖；这些读取由迁移工具执行，Gateway 运行期不读取工作区配置，不扫盘发现工作区。

所有显式旧日志值一致且新位置缺失时才自动迁入 Gateway 域；新旧显式值一致时移除旧位置；不一致、不同工作区覆盖不同值或旧值非法时，预览列出来源和冲突，要求用户在迁移命令中明确选定统一值。文件多步改写使用现有迁移备份/恢复机制，全部验证成功才完成版本推进。未参与迁移的工作区后来出现旧 logger 时，明确拒绝并提示显式迁移，不静默忽略。

不保留旧 logger accessor、运行时兼容层或双 watcher。历史日志文件不自动移动或删除；旧进程退出前沿用其文件，新进程只走新默认根。回滚软件前恢复迁移备份，不能指望旧版本理解新字段。

## Risks / Trade-offs

- [跨进程应用存在暂时不一致] → 使用现有 apply/proof/补偿链逐项展示结果，不承诺分布式原子切换。
- [Gateway 交接时仍有 Backend 写旧文件] → 日志句柄归属纳入既有进程 owner 记录；只在无活动写入者时轮转，不因控制进程退出丢失子进程日志。
- [DEBUG 导致输出量增加] → 生产端过滤、可立即调回等级；本次保持打开时轮转，不用该阈值声称磁盘占用受硬限制。
- [旧工作区有不同日志偏好] → 显式迁移报告冲突；产品诊断配置统一后不再支持逐工作区争用同一进程 logger。
- [文本第三方输出缺少字段] → 保留原文并按进程文件确定来源，不把解析猜测当成日志合同。
- [两种语言再次偏离] → 共用合同资源与跨语言样本，删除旧常量及路径分支，按输出行为验证。

## Migration Plan

1. 固定合同与跨语言样本，迁移两域 schema、模板、策略及全部旧 logger 消费者。
2. 同批贯通 Launcher、Gateway、Backend 和两个 Node 服务的诊断输出、路径 owner 与热应用确认，不留下双运行链路。
3. 用显式迁移工具处理配置冲突；历史日志只保留，不自动搬迁。所有新服务通过唯一入口启动。
4. 在隔离 home/工作区中完成真实多进程热更新和重启交接验收，确认 Agent/终端/浏览器实例保持，Shell 输出字节不变。
5. 发布说明准确列出跨域配置变更、迁移命令和打开时轮转限制；回滚使用备份恢复配置后再启动旧版本。
