## Context

动机见 proposal.md。本次是错误交付链路整理，不是错误文案工程。

已核对的源码事实：

| 已有机制 | 现状与缺口 |
|---|---|
| `proto/boxteam/common/v1/error.proto` | 已有 `Error(code, message, details)` 与 `RequestContext`，不应再设计一个平行的万能 Failure 协议 |
| `app/core/failures.py` | 已有错误来源标记、HTTP 来源上下文、公开消息、执行错误文本和诊断记录；部分路径把校验／Provider 原因压成类型，提取和记录存在耦合 |
| `app/core/trace_middleware.py`、`app/api/errors.py` | 已有 HTTP 异常边界、状态映射、request_id 和流开始后故障处理，不能再叠一个 middleware 改写原状态 |
| `execution_step/failures.py` 与 Job 收尾 | 已向 message stream、Trace 和执行终态报告，但不能据此认定历史已保存同一原因 |
| `workspace_runtime_lifecycle.py` 启动恢复 | 先处理队列 Job／工具及 Trace，再恢复未闭合消息流；只改后者仍可能给已提交结果追加相矛盾的 execution_lost Trace |
| `app/schemas/internal_v2/turn.py`、`history_turns/` | summary/detail 有状态及正文，没有执行失败原因的统一投影字段 |
| `conversationFromTurn`、`historicalBoundaryStatus` | 历史只取得状态，失败分支固定使用“后端没有提供可用的失败详情”；现有测试还断言这段兜底 |
| 插件运行／安装、配置候选、MCP 连接操作、资源 owner | 已有各自 error、状态、事件及日志；不应搬迁其存储或重做状态机 |
| Python／Node SDK | 已有命令、日志、HTTP、维护、生命周期公共入口；进程内工具不能反向依赖 `app/` |
| 客户端 `HttpRequestError`、`errorMessage` | 已独立持有 detail／context／path／requestId，但 `.message` 同时拼入诊断后缀，通用提取再直接交给 UI；8 KiB 载荷上限不能约束默认视觉长度 |
| `HttpRequestError` 的来源解析和缺失原因 | 来源响应头损坏的说明仅保存在拼接的 message 中，context=null 同时表示缺失或损坏；`httpErrorDetailMessage(detail, path)` 无可读原因时返回接口路径。拆分显示需保留损坏诊断，不能继续以路径冒充原因 |
| 设置、插件管理和搜索入口 | 存在局部 formatter 再追加 request_id、插件日志读取失败同时行内展示及通知、Quick Access 详情堆叠原诊断的实际调用方；须迁齐后删除重复拼装 |
| `ComposerDiagnosticsContext`、`workspaceOutput.ts` | 已接窗口日志和原生 OutputService；`boxteam.output.openLog` 可选真实目标／通道，但仅打开最近日志并不等于定位到了原错误 |
| `pluginEditor.ts`、`servicePluginManagement.ts` 的实例日志 | 两处各建 pre 并直接读取，失败同时更新正文及通知；输出又反向导入插件编辑器的 `readPluginInstanceLog`，该 helper 把“空日志／已截断”提示混入返回正文。迁齐不能只处理设置页 viewer |
| `WorkspaceOutput.readSelected`／日志发现 | 尾读失败只弹通知，原模型内容保留但缺刷新失败状态；发现请求内部 catch 提示后仍由命令 await 同一 rejected Promise，可能重复报告，尾读事件的 void 调用又不能证明命令已加载成功 |
| `GatewayDiagnosticLogQueryResultDTO`、`GatewayDiagnosticLogLocation` | 原日志查询按字面量返回命中、`complete／limited_reason／cursor`，有界扫描不保证一次找到；原 locate 持有版本化记录引用，目前在设置页展示，迁入输出时须迁齐旧调用方而非保留两套 viewer |
| `diagnostic_log_transport.py` 与插件日志 | query／locate 当前按稳定 Gateway 要求唯一远端连接，尚未接通尾读支持的明确连接选择；工作区插件日志通过 `runtime_log_id` 注册为 Gateway 文件来源，不能仅用插件 binding.workspace_id 推导查询 source |
| `ModelConnectionProbe`、`failureMessage` | 模型探测既可能抛错，也可能正常返回带 error 的失败结果；通用 Node 提取当前对非 Error 只用 String，不能直接把已解析的公共 Error 对象交进去 |
| Composer 附件、配置编辑／密钥操作 | 已有 allSettled 的部分成功和修改失败后重读失败的 AggregateError；统一呈现不能只留外层标题、覆盖成功项或为每项再弹通知 |
| `server/realtime.py`、`src/shared/realtime.js` | 单订阅异常当前只下发类型名并使用 raw logger.exception；客户端再把 request_id 拼入普通 Error。连接级 fail 将同一 Error 交给全部 pending 订阅，不能在各订阅消费时给该对象回填业务身份 |
| HTTP／DAP 来源与操作目标 | HTTP target 当前只携带已知目标 ID，并不要求 component；DAP 适配器的 source 是 workbench，target 为远端 Gateway／工作区／Thread。来源、操作 owner 与目标角色不能直接互相比等，否则会把合法故障误报为身份冲突 |
| 校验路径与日志坐标 | SDK 校验路径会脱敏、裁剪或替换为 `<extra>`，不能据展示路径保证字段精确定位；日志引用的 line_start／line_end 实为文件字节区间，而 start／end_codepoint 是去行尾后单行文本的码点位置，迁入 Monaco 时须区分单位 |
| 配置编辑器与剪贴板入口 | 配置文本模型已有 getVersionId，字段映射和诊断不能仅凭同一 URI／path 应用到后续草稿；React 既有 clipboard fallback 临时 focus textarea，清理和焦点恢复须由该入口负责，不由各错误组件另写复制流程 |
| HTTP 正文读取／解包 | 非 2xx 的正文读取失败被转成 detail 文本，外层超时又可能抹掉已收到的 status／request_id；2xx 非 JSON 分支将 path／正文片段拼进普通 Error，未保留响应关联。应在原 HTTP adapter 区分已收到响应、原因缺失及解包故障，不交各页面重新判断 |

参考 VS Code：`base/common/errors.ts` 的序列化及异常出口、`errorMessage.ts` 的通用消息提取，以及聊天结果 `errorDetails` 随模型保存、恢复和展示。借鉴其“公共原语 + 领域结果”，不引入 VS Code 扩展宿主或为本项目模拟 Pydantic 以外的校验框架。

## Goals / Non-Goals

**Goals:**

- 未预先登记的异常也有真实原因、类型和已有诊断关联；新增错误不要求修改 UI 或增加异常分支。
- 原 owner 的状态、查询和事件对同一次失败解释一致，刷新后仍可定位；前端不充当故障归因 owner。
- 默认 UI 简短且不重复；完整公开原因可按需取得，技术诊断主要沿底部面板“输出”查看，不以长红字或长 Tooltip 代替诊断导航。
- 改动沿业务链路纵向推进，最终覆盖下表中的边界；每条链迁齐写入、传输、展示和测试后删除重复实现。

**Non-Goals:**

- 不枚举所有 Python／JS 异常，不逐字段编写文案，不要求所有模块继承新异常基类、配置错误注册表或装饰所有函数。
- 不新增错误数据库、错误总线、报告 daemon、重试器、全局通知中心、日志扫描恢复或自动修复配置。
- 不调整模型回退、MCP 重连、维护周期、终端生命周期、清理资格、授权与审批，也不改变 `python_exec`、条件脚本和 ffmpeg 的执行方式。
- 不把本次错误交付扩大成整个日志系统的重写，也不把任意第三方 stderr 无限制复制进 API。

## Decisions

### 0. 最小实现与文案边界

实现主线固定为：原 owner 提供失败事实 → 原 adapter 解释协议 → 原展示入口派生摘要 → 原 Workbench 动作按需打开诊断。下表的职责是已有模块分工，不要求增加对应服务层。

- 文案只取三类来源：原异常／协议原因、少量共享通用提示、原领域已有状态。操作名称复用入口已有标签；通用提取不按插件名、模型名、工具名、字段名或异常类名建立专用文案分支。字段原因来自校验器，不逐字段补中文。
- 新增可选字段只交给实际使用者：普通错误不要求 issues／path_exact、模型版本或日志记录引用。path_exact 只由校验提取方说明路径保真，文档时效与控件映射只在需要字段定位的原编辑器处理。
- `specs/` 定义行为与验收，以下设计规定实现归属，tasks 只跟踪交付；场景不是逐项建立类、helper、状态或测试文件的清单。一组通用机制可以覆盖多个场景，但关键行为仍须验证。
- 每条业务链迁齐消费者后删除重复解析／格式化和旧日志 viewer。新类型、包装器或状态仅在现有承载确实缺少必要事实时增加，不为统一名字重写已有正确入口。

### 1. 公共的是信息提取和关联，业务结果仍归各 owner

复用现有 `Error` 的语义：`code` 是原领域已有控制分类，`message` 是可展示原因，`details` 只承载实际可取得的来源、阶段、异常类型、字段问题或进程／HTTP 元数据。缺少领域 code 时用一个通用未知错误分类，不为每种 Python 异常发明业务 code；异常类名不是重试或权限判断依据。

request_id 优先保留现有响应／事件 envelope 的位置；不存在该位置的结果才在错误上下文中补入实际关联，不能同时维护两套不同值。工作区、Thread、插件实例、配置候选和维护 task ID 沿原宿主对象携带，不在每条错误里复制一套必填身份。没有 HTTP 请求的维护任务、进程退出和客户端本地故障不伪造 request_id，使用原实例／任务／操作标识。

统一语义不等于所有 API 都改为 `{status, failure}`。已有 FastAPI `detail`、MCP JSON-RPC／`isError`、DAP response、插件命令及 message stream 信封保持原职责。已有可表达这些事实的字段直接复用，缺失结构字段才沿原协议扩展；同一对象不同时保留两份可变错误文字。

最小字段合同固定如下。只对本项目构造的公共错误及协议扩展采用该合同；标准协议字段由原边界映射一次，前端不遍历多个候选键猜测。未列出的领域结果字段继续归原领域，不借此复制任意异常对象或完整上游 data。

| 字段 | 类型与语义 |
|---|---|
| `code`、`message` | 复用公共 Error；保留已有领域 code。新产生且无分类的异常使用 `unknown_error`，不据此推导重试；原历史只有文本时不补造分类 |
| `details.error_type`、`details.stage` | 可选非空字符串，分别是来源提供的异常／协议错误类型及已知失败阶段；不新增 `type`／`phase` 等同义公共键 |
| `details.source`、`details.target` | 可选对象，沿已有上下文。source 表示错误实际产生位置，提供时保留原 component；target 仅包含该边界已知的目标 ID，不要求 component，不推断目标已执行或已故障。各领域原 Session／Thread 等关联仍沿原宿主，不复制一套身份 |
| `details.request_id` | 仅原宿主无 request_id 位置且该错误确有原请求关联时出现；已有 envelope 中的 request_id 不搬迁、不另存一份 |
| `details.issues` | 校验问题数组，每项含 `{path: (string 或 integer)[], code: string, message: string}`，可附 `path_exact: boolean`；path 为安全展示用字段段，提取方仅在完整保留原校验路径时声明 path_exact=true，发生脱敏、裁剪或占位替换时为 false。该事实只说明路径保真，不保证能映射控件；不包含 input／context，不把含点号的字段名拆成路径 |
| `details.status_code`、`details.exit_code`、`details.signal` | 可选原 HTTP 整数状态码、进程整数退出码、signal 字符串；沿原协议已有位置传递时不再复制 |
| `details.cause`、`details.errors` | 可选单个原因或子错误数组，成员使用同一公共 Error 形状；只提供有界诊断，不由 cause 推翻外层业务终态 |
| `details.truncated` | 仅有截断时为 true，表示整个错误载荷有省略；不能因截断丢失原失败身份 |

缺失的可选字段直接省略，不填 null、空对象、当前激活目标或虚假默认身份。原宿主的状态／code／关联为权威，新增上下文只能补充实际事实。原 adapter 只核对同一角色、同一作用域下确应一致的稳定身份；source、操作 owner 和 target 角色不同，不能因其不同就报告冲突。例如 Workbench 到远端工作区的连接失败，source=workbench 与 target=远端工作区合法并存，不证明远端已经执行或发生业务失败。确认属于同一角色的身份确有冲突时保留原错误及关联，并由原 adapter 交付协议／归属诊断，不覆盖事实或静默择一，也不据冲突元数据跳转其它目标。既有协议已经使用其它字段名时，映射固定在该原 adapter 中，不增加通用“依次尝试多个键”的兼容层。该合同随原 proto／schema 生成及类型检查维护，不新建运行期 schema 注册中心。

各环节职责落在已有模块，不以“统一入口”为由把业务判断都移入公共 helper：

| 责任 | 现有归属与输入／输出 | 不承担的职责 |
|---|---|---|
| 原因与业务结果 | Backend 的原执行／配置／资源 owner，或 Gateway 的插件／路由 owner；确定原 code、实际状态、可恢复性和已有日志来源 | 不决定客户端是否弹通知，不为 UI 省略改持久事实 |
| 通用异常提取 | Python／Node SDK failures；异常及当次已持有秘密 → 有界安全原因／诊断 | 不识别 HttpRequestError、工作区状态或 Workbench 服务，不调用日志导航或查询 |
| 协议解析和身份核验 | Web 原 HTTP／领域 API adapter、共享 realtime 协议层、DAP adapter 各自解释自己的信封；只把本协议已知字段交给后续消费者 | 不让 SDK 或纯展示模块解析所有协议，不由 realtime 通道判断 Turn／插件业务终态 |
| 展示与操作归属 | 原 React／Workbench 操作入口持有领域结果、目标快照和可用动作，决定所属区域还是原通知；共享纯展示逻辑只派生摘要／安全诊断文本 | 纯逻辑不导入 Web API、VS Code、React 或 DOM，不推导重试、批量结果或日志来源；ReportedError 只表达原客户端操作已报告，不写回领域状态／协议 |
| 通知、窗口日志和输出导航 | 原 Workbench 命令／日志桥执行副作用；React 使用已有回调，局部状态仍归原组件 | 不另解析协议、推导业务结果或保存全局错误，不要求各组件直接依赖 OutputService |
| 日志读取与定位 | Gateway 原 diagnostics owner 提供原 descriptor、query／locate 和版本核验；workspaceOutput 消费其能力及原失败绑定 | 不重建业务失败，不从 Error.source 的 component 猜日志 descriptor 的 source，不替下游业务选择恢复策略 |

这里是职责划分，不是新增六层服务。沿已有调用点交付已解析事实，删除重复解析／格式化和反向依赖；仅需共享的纯投影放客户端 shared/core，该目录不反向导入 web/api 或 Workbench。工作区、Thread、字段控件及操作资格由原消费者已持有的领域信息提供，不在纯函数内按错误文本推断。浏览器内纯提取不会仅因“已经统一”就替业务改变状态；跨进程不传递客户端已报告标记。

### 2. 默认使用异常已有原因，不依赖人工文案

公共提取是纯数据处理，与记录日志分开；上层转发已生成的错误时不再格式化为第二个原因。按语言维护一份实际可复用的基础实现，Python 公共部分放 Python SDK，Node 公共部分放 Node SDK；产品特有异常和 HTTP 映射留在 `app/core/failures.py`／原 adapter，客户端纯展示留在现有共享 core，禁止 SDK 导入产品模块。不预建插件注册式 formatter 系统。

| 输入 | 采用的信息 | 不做的事 |
|---|---|---|
| 普通未知异常 | 异常类型、经统一敏感信息处理的 `str(error)`／JS `Error.message`；消息为空时明确“未提供错误文本” | 不要求类型白名单才展示原因，不一律压成“内部错误” |
| Pydantic／JSON schema 校验 | 校验器已有字段路径、错误 code 和原因；Pydantic 排除 input/context/URL，通用翻译可选 | 不逐字段写检查，不为显示中文阻断未知校验 code，动态路径及 validator 消息也须脱敏 |
| HTTP／Provider | 原状态码、原错误 message/code、已知目标和失败阶段 | 不把网络超时、鉴权拒绝和模型参数错误互相改写，不下发请求头／完整请求正文 |
| 子进程 | 原退出码／signal、原 supervisor 已捕获且有界的 stderr 摘要、原日志入口 | 不为交付错误再读完整日志，不将正常终端输出当故障 |
| MCP／DAP／命令 | 原协议错误 code/message/data 中必要原因，保留 transport 与业务调用的区别 | 不把 MCP 工具 `isError` 误作连接断开，不因提示修改调用重放策略 |
| ExceptionGroup／cause | 保留主失败及有界子错误／因果说明，原诊断日志按下述独立预算保留堆栈和结构 | 不 flatten 成最后一个清理异常，不递归复制整个异常对象或局部变量 |

阶段和来源来自已知边界，例如“插件启动”“配置应用”，通常只需边界提供一次；不从异常文本猜测来源，不要求每条 raiser 补中文句子。显式业务说明仍可复用现有机制，但不是通用覆盖的先决条件。

拒绝仅用 Pydantic 覆盖整个项目：它只解决结构校验，不覆盖进程、网络或生命周期错误。拒绝建立穷尽异常映射表：无法覆盖新增异常，且会持续扩大分支。

### 3. 敏感信息与体积控制只在现有出口处理

先复用已有脱敏原语；存在缺口时在公共出口补一份针对实际错误载荷的轻量处理。认证头、Cookie、URL userinfo／敏感查询参数、结构化 secret 字段，以及本操作已持有的凭据值均不得进入公开错误或本链路新生成的诊断正文。普通未知异常仍保留可展示原因，不以“未知类型”作为整段丢弃原因的理由。不得为每条错误枚举环境变量、读取完整密钥库或启动异步密钥查询。

脱敏有两个实际落点：公开错误构造前处理 message／details；原报告出口写日志前处理有界格式化的诊断正文。Python 在报告出口不采集 locals，沿选定的有界原因节点复用标准库单段堆栈提取／格式化；Node 对原 stack／cause 采用相同预算。各文本片段脱敏后累计到正文，随后沿现有 SDK logger 记录安全文本，不再同时传入未经处理的 `exc_info`／原 Error 让 formatter 输出第二份原文。不能只清洗 message 却保留 raw traceback，也不能先把整条异常链格式化完再截断。

秘密值来源仅限已有凭据使用边界：Provider／MCP 客户端创建或调用处、插件私有输入消费处等已实际持有本次凭据的位置，在原有捕获／报告步骤把它们作为短期参数交给纯脱敏函数，生成安全消息和诊断文本。原异常继续用于内部因果传播；下游只交付安全事实，不重复记录其原始堆栈。不把秘密塞进 error details、日志 extra、异常属性、ContextVar、全局集合或新 SDK 注册表，不延长凭据生命周期。没有实际凭据值的边界仍做结构字段和常见认证语法处理，不承诺识别任意未知文本中的随机秘密，也不能为此丢弃全部原因。

诊断比公开错误载荷多保留堆栈和因果结构，但同样有界：沿原更严格限制；否则本链路单条诊断正文 UTF-8 至多 64 KiB（含 message、stack／cause 和截断标记），已选异常的堆栈帧合计至多 64 帧，原因后代／深度复用 8 个／4 层上限。优先保留主原因、真实关联和靠近抛错点的帧；超限停止遍历和格式化，明确省略，不先递归构造全部 traceback／ExceptionGroup 或渲染巨大校验正文再裁剪。单段消息仍沿已有通用提取与脱敏原语处理，不开发另一套异常格式化框架或为自定义 `__str__` 新建超时线程／沙箱。日志轮转约束落盘保留，不能替代上述构造预算。

该调整只迁齐本 change 涉及的报告链路，不保存明文凭据、locals、请求正文或整份环境，不改写全部 logger，也不拦截第三方库自行输出；已有第三方原始日志仍受原访问边界和保留规则约束，公开 UI 摘要须脱敏，本次不扫描、改写既有日志。恶意自定义异常任意编码秘密不是本次建立复杂净化器的理由。

大小预算适用于本项目构造／持久化的公共 Error，以及从原协议映射出的公开提取载荷，不作为所有下游 wire 错误正文的新限制，也不是默认 UI 摘要的视觉长度。沿原更严格预算；没有约束时，单个上述错误连同其 details／issues／cause／子错误的 JSON UTF-8 总量至多 16 KiB，其中 message 至多 8 KiB；issues 至多 32 项，cause／子错误合计至多 8 个后代、嵌套至多 4 层。这些是公共常量，不增加用户配置开关。单个问题、路径段、子错误及上游 data 不能绕过总预算；不把异常对象或第三方 data 全量转成 dict 后才截断。

构造时采用有界读取／遍历及累积字节预算，输出前只对已受限的候选做序列化核对。先保留原 code／类型、已有宿主关联和截断标记，再保留有界主原因，剩余预算按原顺序留校验问题及次生原因；不足时裁去次要详情、缩短文本并标记 `details.truncated`，不修改状态或生成新错误替代原故障。凭据识别不能先裁断一个已知凭据再让残片泄露，敏感正文须沿现有有界载荷在裁剪前处理。

HTTP／MCP／DAP 的原错误信封、工具正文和 data 沿各原协议、鉴权、脱敏及传输大小规则处理；Gateway 不为满足 16 KiB 公开错误预算而新增正文缓冲、JSON 解析、截断或重编码，也不替下游净化整个响应。错误 owner 在原构造出口处理本层秘密，客户端或原协议消费 adapter 只映射有界必要字段；默认 UI 摘要再从这些事实纯派生，省略不写回原协议对象。无法解析的非 JSON 正文沿现有内容类型和 transport 规则处理，不增加猜字段、全量文本读取或第二个错误信封；原传输超限仍沿原传输失败语义。Gateway 自己生成的错误受公共 Error 预算约束。

较详细的有界诊断仍沿原日志入口和有界读取规则按需查看，不能因不受 16 KiB 公开错误预算而宣称无限完整；不发明新全文下载或新的资源 URI kind。UI 用纯文本或已有安全渲染器显示，不执行异常带来的 HTML、命令或任意链接。

### 4. 在真实边界捕获，不扩大正常调用链

| 边界／原 owner | 错误交付及持有位置 | UI 和操作语义 |
|---|---|---|
| HTTP／Gateway 代理 | 原异常 handler、TraceMiddleware、代理响应；已收到下游错误保持原状态／正文含义／request_id；Gateway 自身路由、连接故障标注 Gateway 和已知目标 | 原页面行内结果或通知；不由 502 猜成插件业务失败 |
| Agent／工具／模型执行 | 原 execution 收尾及 message stream／rollout owner | 对应 Turn／tool；用户取消、超时、结果未知保持原分类 |
| 插件安装与运行 | 原 install operation、实例状态和 supervisor 输出 | 扩展详情／实例错误／原输出；安装失败不伪称运行实例启动失败 |
| MCP 连接与调用 | 原 connection operation／tool result；标准协议原地交付 | 管理页或对应工具；错误不新增重连、审批、重放 |
| 配置解析与应用 | 原候选、active/pending、应用事件和诊断 | 设置页显示来源／字段／应用阶段；保存成功不等于已生效，不改 revision 和 CAS |
| 主题及客户端操作 | 原加载 Promise／命令入口及 Workbench 通知／日志；涉及后端请求保留原 HTTP 错误 | 用户此次切换失败不能宣称成功；保留原生预览取消、主题移除、启动恢复和默认主题恢复策略，展示实际生效状态，不弹重复通知 |
| 终端／浏览器／调试 | 原 resource owner、插件实例或专用协议的错误 | 所属面板／资源详情；已退出历史、未核实和操作失败分别表达，不能把未核实写成进程必然不存在 |
| 后台维护 | SDK 已有任务报告入口和原 owner 最近状态（如本来存在） | 默认原日志；不要求为每次周期错误新增持久记录或 toast，下一原周期行为不变 |
| 释放／关闭 | 原 LifetimeScope／插件 shutdown／资源 owner 的失败聚合 | 主操作失败和次生释放失败都能定位；释放失败保留未释放事实，不伪造成功或自动重试 |
| 客户端本地 JS 异常 | 已有命令／React 错误边界／日志 | 保留浏览器来源；不归因 Backend，不伪造后端请求身份 |

普通业务函数直接抛错；已有局部捕获若承担补偿、资源释放或协议语义则保留，禁止为了减缩进删除必要边界。取消在 Python 中继续传播 `CancelledError`，不是统一捕获 `BaseException`；Node Abort 继续采用原取消语义。

原失败最靠近的拥有者记录上述有界诊断，上层传播结构化事实并保留关联，不在每一层重复堆栈。日志或失败记录写入本身失败时，保留原故障并暴露次生故障；沿原监督／关闭规则处理，不返回伪造成功，不递归调用同一失效报告入口，不新建备用账本。

### 5. 聊天终态沿一份权威事实恢复

```text
异常 → 原执行失败收尾 → rollout convergence 原事务提交终态与 failure
                              ├─ 派生原 message stream 终态事件／快照
                              └─ 原 Turn summary／detail → ConversationView → Turn UI
```

**唯一来源固定为原 rollout convergence 的终态提交。** 扩展 `RolloutExecutionsMixin.converge_execution` 及原 checkpoint facade，在原 `TERMINAL_CONVERGENCE` 提交的 `storage_commits.metadata_json.failure` 保存安全 failure；execution／Turn 状态、failure 和 history revision 在同一原事务提交。failure 不另建表；既有索引／快照投影只能引用或派生这一份 metadata，不另写可变 error_message。历史 reader 沿原实际 view／revision 及 execution-terminal 提交关联取得它，不读 message stream 文件作为历史来源，也不扫描全部 commits 猜最后一个错误。

原 message stream 的 `stream.failed`／snapshot 是上述终态的传输镜像，保留现有 code/message 和取消／可恢复性字段。原失败收尾先原子提交 rollout，再从已提交结果构造终态事件；不能先构造两个独立可变对象分别更新。复用原执行准入、终态幂等键和 commit 身份；重入读取已提交原因，不因重新格式化、生成新时间或错误 ID 改写已提交事实。非终态 `model.failed`／tool failure 仍沿原事件路径处理，不要求先结束整个 Turn。

启动恢复须在现有 `reconcile_stale_executions` 的 Job 处理入口先读取精确 admission／execution 的已提交终态，再决定恢复、丢失、Trace 和消息流收尾。已提交 completed／completed_empty、failed、cancelled／interrupted 的执行不恢复为活动 Job、不重放工具、不追加固定 execution_lost Trace；原队列／claim 沿原 owner 收尾，缺失的消息流终态按已提交结果映射。终态写入端口返回 True 只表示原操作已被吸收，不能据此推断本次写入了 execution_lost。只有无已提交终态且原 checkpoint 恢复不成立时才沿原丢失分支记录。复用同一终态读取端口供 Job 和消息流恢复消费，不新增恢复协调器、终态缓存或逐轮历史扫描。

两处持久化不宣称跨文件原子；崩溃边界固定如下：

| 中断位置 | 恢复规则 |
|---|---|
| rollout 终态事务未提交 | 不存在已确认原失败原因；继续原执行恢复／execution_lost 判定，不能用零散 model.failed、异常日志或当前配置补造业务失败 |
| rollout 已提交、stream 终态未写入 | Job 恢复先识别已提交结果；`reconcile_unfinished_streams` 使用同一原 convergence 终态读取，按精确 session/thread/turn/execution 补齐对应 completed／interrupted／failed 镜像。失败带原 failure，成功不制造 failure，取消沿原控制映射；不恢复 Job 或追加 execution_lost Trace |
| stream 终态已写入、客户端未收到 | 原 cursor 回放或 snapshot 返回已提交镜像，不产生新业务执行 |

补齐镜像沿原启动恢复／writer 幂等入口，使用原 execution＋terminal commit 派生的稳定事件身份；同一提交重复恢复零新增终态事件，不另建 outbox、恢复队列、轮询或协调服务。仍处于原 resumed job 集合的执行保持原跳过规则。没有流的已接受 Turn 仍可从历史取得 failure，若沿原生命周期创建消息流则用原准入身份而非虚构 Item。普通历史／snapshot 查询只读，不负责补写。

镜像一致性必须比较同一 terminal commit：同一提交内容不同才是完整性故障。原 owner 已允许 execution_lost 后产生 cancelled／interrupted 的后续控制修正，不能把该合法新提交当作历史损坏；保留旧提交和旧快照，经原 writer／控制事件路径交付当前修正。后续提交使用自己的事件身份，不复用旧 event_id 写入不同内容、不改写旧事件，不重复业务执行或首次完成通知，不改变 first-end 分页／订阅游标顺序。启动恢复原集合内需要交付的合法控制修正与未闭合流共用同一读取及幂等路径，不因此在普通查询或每次启动新增全历史扫描。失败落盘本身失败时不宣称终态已提交，保留原故障及写入故障，沿原监督／恢复规则处理。

初始化失败可能发生在首个模型 Item 之前，不能把 failure 依附于“必须有 model/tool Item”这个条件；已接受 Turn 应保留失败原因，Item 数仍为 0。无 Turn 的 internal execution 同样把原因交原 execution convergence metadata，继续使用其原 envelope，不硬造 Turn。

实时、snapshot、history 使用同一已提交原因。历史 summary 与 detail 都带原 failure，不要求展开 Item；failure 是终态 metadata，不计 Item，不创建虚构 assistant 消息。字段复用原 stream failure 的 code/message 语义，必要元数据沿原公共 Error 语义传递，不同时保存一个新 failure 和旧 error_message 作为两个可变来源。存储有现成错误文本但缺少结构元数据时只能披露已有事实，不能编造 code／身份。

**attempt 失败不等于 Turn 失败。** 模型 A 失败后回退 B 成功，或工具 `isError` 后 Agent 继续并成功，错误只留在其原 model/tool attempt 明细；Turn 成功终态不带 failure，也不显示整轮失败标志。只有原 convergence 已确认的 Turn 失败才投影到 Turn.failure。显式恢复产生新 execution 时，旧执行的 failure 留在旧终态提交，新活动／成功投影不得继承它；按当前实际历史快照所选的 terminal execution 读取，不从“最近一条错误事件”选原因。原并行模型、重试和回退决策不变。

保留既有历史状态映射；仅有 failed／unknown 状态不能证明某个工具或模型失败。旧记录没有原因时明确“该记录未保存失败详情”，不冒充后端当前没有返回，更不按相邻 Trace、当前配置或最新模型猜原因。读取期间不修复数据、不扫日志补写。用户中断和失败的竞争仍由原终态 owner 决定，本变更不重做竞态状态机。

### 6. 前端统一消费，而非统一弹窗

复用现有 HTTP 解析、React 结果区域、Workbench 通知／输出及剪贴板。VS Code 的通知会直接消费传入 message，不会替业务分离诊断；因此在原展示入口派生摘要，而不改公共提取和业务事实。具体行为及边界场景见 `specs/failure-reporting/spec.md`，这里仅确定落点。

#### 6.1 原因、摘要与领域专属处理

| 内容 | 归属与实现 |
|---|---|
| 原公开原因 | SDK／原 adapter 保留原预算内 message、issues 及关联，继续供协议、日志和状态消费；不全局缩短 failureMessage／errorMessage |
| 默认摘要 | 客户端 shared/core 的纯投影消费已解析事实和入口已有标签；紧凑提示单行、独立空态至多两行。缺少原因如实说明，不拿 path／status 当原因，不按异常类型编文案 |
| 按需诊断 | 保留完整有界公开原因及实际来源、路径、阶段、状态和关联；主要经原输出查看，复制只用已持有安全字段，不依赖读取原日志 |
| HTTP 故障 | 原 requestJson／readHttpErrorDetail 及 HTTP adapter 一次解析，保留 detail／context／path／requestId；来源头缺失与损坏分别表达，损坏诊断不含原头／解析原文，不覆盖业务原因 |
| 正文读取／解包 | 已收到非 2xx 的正文故障不抹掉原状态及关联、不充当业务原因；要求 JSON 的 2xx 解包失败保留实际响应关联，正文片段脱敏后只入诊断。保持无响应、主动取消、允许空正文端点及令牌刷新原语义，不为展示重读／clone 正文 |
| 字段定位 | SDK／校验 adapter 在原 issue 交付 path_exact；原编辑器解释字段段、body／query 等前缀、语法偏移和分支，并核对原资源与模型版本／草稿快照。脱敏／裁剪／占位路径、未声明精确或草稿已变化时不提供精确跳转，原因仍保留 |
| 业务及批量结果 | 原领域入口交付类型化结果、主次关系和真实计数。正常响应中的 error 也可展示，不强行 throw、不 String 为对象文本；保留部分成功、取消／未执行及逐项关联，不推断根因或重放成功项 |

HttpRequestError 的混合 message 后缀和各 UI 局部追加字段应在消费者迁齐后删除，不靠正则反解析。展示状态保留原错误及导航关联，视觉裁剪只生成投影，不写回历史、缓存／消息流的 failure 或另存摘要。必要业务文件名／字段名可留摘要，技术关联不塞进原生 title；字段诊断、主动展开的工具结果和日志不套紧凑行限。多个 issues 只显示实际已知数量，截断时明确省略。

字段版本复用编辑器已有 getVersionId／草稿状态，结果返回及点击定位时核对；数组重排、继续编辑、重载后使旧标记／跳转失效，不覆盖草稿或自动重验。SDK 不识别控件，公共展示不保存文档副本。历史刷新直接消费原 summary／detail 的 failure，即使旧 JS Error 或原日志不可用也可取得已保存原因，不展开 Item 或补读 Trace。

#### 6.2 一个主要提示出口，业务状态由原 owner 决定

| 场景 | 原出口 |
|---|---|
| 历史读取／定位失败 | 历史区，不复制到 Composer；保留可读消息 |
| 校验／配置／模型探测 | 原字段诊断或提交／测试结果，保持 saved／active／pending、结果未知和草稿事实 |
| Turn／工具／插件／MCP／终端／浏览器／调试 | 对应操作或资源区域；attempt、连接状态和整轮终态分开 |
| 联邦搜索部分失败 | 原失败分支和不完整标记，保留成功结果与用户所选范围 |
| 无所属区域的全局命令／主题 | 原生通知；保留主题预览取消、默认恢复及实际生效主题 |
| 后台同步／维护 | 原日志；影响可用性时保留所属区域紧凑状态，不逐次弹通知 |

原发起入口持有目标／连接／资源快照并交付一次反馈，沿现有 ReportedError 传播，不改变失败返回。区域已关闭但操作仍有效且需要反馈时才使用原生通知；写日志／更新隐藏状态不算用户已收到提示。已显示后关闭区域不补发通知，原生命周期取消／过期仍按原规则处理。多视图保留状态镜像，不各弹通知或变异同一共享 Error；聚合中已报告成员不吞掉其它失败，由原聚合出口交必要汇总。

回调、日志和复制均用发起快照，不从当前编辑器／模型重新取归属。恢复／新同类操作成功沿原生命周期替换相应显示；关闭提示、打开日志或网络恢复不清除权威业务失败。取消以原上下文判断，不仅凭 AbortError 名称；已保存但应用失败、写入结果未知由原 owner 确认，不凭 reject、当前资源或 revision 变化猜结果。恢复动作只复用原资格。

共享 realtime 的原捕获出口复用有界脱敏报告，原 error 帧保留安全原因及已有 subscription_id／request_id；src/shared/realtime.js 和订阅 adapter 保留字段与订阅 path，不将 ID 拼进 message，不补造 500／握手 ID。单订阅故障只收尾该订阅，连接故障归原统一管理入口；订阅者不回写共享 Error 的业务身份、不重连各自业务或将传输失败落成领域终态。订阅 ID 只在原连接生命周期使用。

#### 6.3 输出负责日志读取／定位，剪贴板负责复制副作用

所有日志查看复用 boxteam.output.openLog、OutputService、原窗口日志桥及 Gateway diagnostics；日志 owner 提供能力，workspaceOutput 消费，不从 Error.source 猜日志 descriptor.source。

| 事项 | 唯一落点和必要行为 |
|---|---|
| 迁齐入口 | 设置页 GatewayDiagnosticLogLocation 及 pluginEditor／servicePluginManagement 的实例日志查看迁入输出，删除旧 viewer／路由、局部 pre／读取及旧 readPluginInstanceLog UI 导出；输出直接用既有 getGatewayDiagnostics，不反向依赖插件编辑器。query／locate API、诊断配置和进程概览保留 |
| 来源与连接 | 点击时绑定原目标、工作区、log_id／实例及真实关联，不激活导航工作区、不换同名实例。日志 source／范围取原 descriptor，插件工作区 binding 不代替日志归属；query／locate 沿用尾读的可选 gateway_connection_id，由原注册表核验稳定 Gateway，未指定仅用唯一连接，失效不换凭据；联邦入站权限不扩展，窗口日志始终本地 |
| 按需导航 | 有版本化引用则有界 locate；只有关联则先查已加载通道，未找到再一次有界 query。能力只支持尾读／仅知通道时只承诺打开输出；无真实来源不显示虚假查看日志动作，已持有安全诊断仍可复制 |
| 查询与版本 | 消费原单行字面量、complete／limited_reason／cursor；未完成零命中不表示没有记录，续页由用户发起。候选核对来源及原 log_id／实例，多条或 ID 子串命中不任取根因。业务与诊断请求 ID 分开，引用沿原 device／inode／content_version 核验，失效不转到新文件同一偏移 |
| 输出状态 | 区分打开、读取、定位，不把 showChannel／void 读取当加载成功；失败由本次原操作边界反馈一次，不递归查日志。同源刷新失败可留最后成功正文但说明未刷新，首次失败不当空日志，换源不混旧正文；空／截断／不可用等反馈不写成日志行或加入正文复制 |
| 生命周期 | 关闭／换通道／新导航取消或废弃旧结果，不抢焦点；定位记录不被尾读立即覆盖，也不插入实时尾部冒充新日志。沿原模型生命周期处理，不另存命中副本 |
| 坐标 | Gateway 保持 line_start／line_end 为物理行 UTF-8 字节半开区间，start／end_codepoint 为去行尾并 UTF-8 替换解码后的 locate.text 码点区间；Workbench 在已核验文本内转换 Monaco UTF-16，不能拿 preview／字节作原行坐标。schema／标签明确单位，不改引用或另存坐标 |
| 复制 | Workbench 复用 IClipboardService，React 复用 utils/clipboard；只格式化已持有的安全公开原因与关联，不序列化整个 Error／Response／请求／配置、不附加原日志。临时节点清理、失败传播及有效焦点／选区还原归原剪贴板入口；成功完成才反馈成功，不改草稿、不抢回旧编辑器焦点，失败不覆盖业务原因或自动重试 |

原 owner 记录一次安全诊断，上层不重复堆栈。查看第三方原日志仍沿原访问边界，不声称已净化。本节沿已有有界请求和展示态实现，不增加索引、全文件扫描、跨目标搜索、自动追页、后台预取、轮询、缓存或备用错误账本。次要动作放原生菜单，键盘可达，不靠悬停取得详情、不因日志刷新反复播报。

#### 6.4 工作台不可用的故障页

局部渲染边界保持原隔离粒度，短摘要及窗口日志；整个 Workbench 无法打开输出时，原故障页才提供本地展开／复制安全诊断及重载，不依赖后端或日志写入成功。普通业务组件不另建详情系统。

本节新增展示合同尚未验收：tasks 6.3 重新打开，旧勾选和下方历史证据不代表新增要求已完成。

### 7. 实施切片、性能和证据

先公共信息提取与协议消费，再按“聊天 → 插件／MCP／资源 → 配置／主题／客户端 → 维护／关闭”迁齐；这是实现依赖关系，不要求一次提交跨越所有域。每条链全仓检索直接／间接消费者，保留必要状态映射，物理删除其重复 formatter 和错误文字重建；不批量重构无关异常。

错误格式化只访问当次异常／协议载荷和已持有上下文；正常暖读不执行错误扫描、自检、额外远程查询或全库完整性校验。权威 owner、触发边界和失效条件分别沿原领域版本，不新建错误缓存。沿 `interactive-latency-budgets`：普通 ≤512 KiB 暖读 p95≤500ms、单次≤1500ms；三步导航 p95≤1000ms、单次≤2000ms；可操作 Composer／文件可见≤2000ms；原历史更严格预算优先。外网、模型及进程启动等待单列；快速本地失败计时从真实操作到失败结果可见，不能用提前出现 loading 作为终点。

验证以少量稳定的真实入口场景覆盖跨层信息保真；不为易变摘要文字、像素布局或所有异常类型新增测试。真实后端校验、插件退出和协议失败必须由入口触发，不能手工向 helper 塞 metadata 代替。原有错误测试预期须据合同改变，不能把“无详情”断言机械删掉。具体场景和证据要求见 tasks.md。

## Risks / Trade-offs

- [异常正文可能含凭据] → 在原出口脱敏，覆盖真实消息、URL、校验器和 stderr 样本；不依赖逐类型许可，也不声称任意编码秘密都能识别。
- [通用原始消息不够友好] → 先保留真实原因和原操作上下文；通用文案覆盖未知类别，人工业务解释只用于确有价值的少量场景。
- [多协议被强改成同一信封] → 统一语义及必要字段，不替换标准协议或已有控制 code；生成消费者同批更新。
- [原失败被清理错误覆盖／重复上报] → 原 owner 保留主故障和次生故障，传播层不重复完整堆栈，不增加失败队列。
- [旧历史原因无法恢复] → 允许如实表达缺失，不重建、不推测、不阻断其它有完整数据的历史。
- [并发任务正在修改资源／插件代码] → 实施时重新核对契约和工作树，逐 owner 集成，不覆盖其它任务改动；本设计不冻结并发源码版本。

## Migration Plan

1. 实施前记录各受影响 owner 的现行结构／wire 版本、真实写入方和消费者，以及是否存在可核验 `R_current`／`R_previous`；当前开发残留不构成迁移来源。按版本升级与迁移约定处理缺口，不据本文创建猜测迁移。
2. 优先利用既有终态载荷和错误字段。确有持久结构变化时由原 owner 的有序迁移／schema 版本承接，复用事务、备份及原支持范围；纯 DTO 或投影增加字段不自动意味着修改 SQLite schema。
3. 协议从原 proto／schema 生成并贯通 Python、Node、Web；生成 Python 协议仍只有 SDK 一份。不保留新旧两套读取／写入，不删除真实支持基线所需的历史迁移。
4. 真实用户 Home／数据不因本变更自动清理、重建或修复。历史原因缺失不是迁移失败，也不能扫描日志回填。开发验证使用隔离 Home／工作区。
5. 回滚代码时先核对原 owner 的格式兼容与已有回退流程；有持久结构变化则按原备份／离线维护回退，不将旧程序直接指向未知新结构。不承诺通用自动降级或双写窗口。

## 实施核对与验收证据（2026-11-22）

本批在共享根 worktree 实施；记录起点为 `8005338f795abe98299947677353ad2c72a8046f`，工作树还有并发任务改动，因此不能只凭 HEAD 复现。各批实际源码摘要保存在临时产物的 `chat/source-sha256.txt`、`primitives-final-source.json`，收尾再记录统一指纹。未提交、同步或重启固定 dev，未修改用户 Home、真实会话或连接。所有测试使用进程外 `timeout` 和 `ulimit -d 4194304`，不运行 reference_repo 测试。

### 实际入口与保留边界

| 边界 | 本批落点和核对结论 |
|---|---|
| 公共提取与诊断 | Python SDK `failures.py`、Node SDK `failures.ts` 分别拥有本语言纯提取原语；Node 子路径可在浏览器运行。`app/core/failures.py` 只保留产品分类、原关联及日志出口。未知原因不再只留类型；显式公开说明仍优先。调用时已有凭据作为短期参数，无环境扫描、秘密注册表或额外服务 |
| HTTP／Gateway | 原 TraceMiddleware、`app/api/errors.py`、请求校验 codec 和 BaseAPIException handler 共用安全原因；标准 422 仍为 `detail[{loc,type,msg}]`。原 HTTPException 结构及下游正文保留。Gateway `send_upstream_request`／`ProxyStreamingResponse` 不增加正文解析，两跳 422 大 JSON／文本完整透传。HTTP 来源使用原头，`setdefault` 保留更低层身份，取消和已开始响应后抛错不改 |
| 客户端 HTTP | 原 `HttpRequestError` 保存原 detail 与响应头 request_id；摘要只提取已知 message／校验字段，正文 data 不复制到摘要。非 JSON 只消费首段并取消读取，先脱敏再截断；状态文字和关联后缀也计入预算。原 token 失效刷新策略保留。删除按 401／403／404／409 猜原因的展示分支，六个文件／目录调用方统一使用原 `errorMessage` |
| 聊天终态 | 原 convergence 的 `storage_commits.metadata_json.failure` 为唯一持久事实，状态及历史 revision 同事务。新增精确 `execution_terminal_result`，原 Job 启动入口先读已提交终态，原 writer 按 commit 幂等镜像。summary/detail/internal execution 与 Python／Node／Web 生成协议贯通，没有新表、outbox、扫描或恢复协调器 |
| 聊天客户端 | `TurnSummary.failure → ConversationView.turnFailure → ChatTurn`；memo 比较终态和 failure。原同 revision 合并保留 metadata，新 revision 替换不继承旧 failure。整轮边界只读权威 Turn 结果，工具 attempt 的错误仍在工具行，成功 Turn 不因工具错误显示整轮失败 |
| 插件安装／运行 | 原 installation operation、runtime slot、SDK 命令和 supervisor 出口提取安全原因。原退出码／signal、实例身份、运行与诊断应用状态分别保留；没有捕获 stderr 摘要的 owner 只交退出事实及 log_id，不为交付错误读取日志。第三方原 stderr 保持原字节与原访问边界，不声称全部原日志已经净化。没有新增重启或另一份实例账本；Node tarball／Python wheel 走原构建制品入口验证 |
| MCP | 原 catalog／connection operation／tool 协议继续各自归属；operation 补安全 `error_message` 并贯通 Workbench 原状态／详情。`error_code` 仍是控制分类，不用 message 决定重连。OAuth 的既有私密输入合同保留，不放宽为公开任意异常；MCP 工具 isError 不重写连接状态 |
| 终端／浏览器／调试 | 原资源 owner 和专用 HTTP／WebSocket／DAP 原错误合同不套公共摘要信封。浏览器维护日志不再把原 Error 另交 formatter；调试响应校验真实入口、代理关闭／原异常身份用定向测试核对。本批不替换终端协议或修改资源释放条件，并发终端任务的独有改动不归本批 |
| 配置 | 原 instruction input 最近错误、LLM probe、reload watcher／启动对账出口使用同原语。active／pending／revision／CAS、设置原候选和诊断入口保留，不新增预检、自动修复或逐字段提示。设置组件仍按原 lifetime／请求身份处理结果，不引入新的全局错误状态 |
| 主题 | 只通过现有 sourceTransform 修改原 Workbench `ensureLoaded` 失败包装和 reload 日志，保留 cause 与实际原因；原预览取消、主题移除、启动缺失及默认主题恢复逻辑不变。React 背景测试只能证明背景合同，不能充当原生主题切换验收 |
| 维护／关闭 | SDK 原维护、LifetimeScope、进程生命周期、插件 shutdown 与浏览器维护出口保留主错误、次生清理错误和 notes；无 HTTP 请求不造 request_id，不新增周期记录、toast、释放重试或生命周期状态机 |

公共 Error 仍为原 `code/message/details`。确需新增 wire 字段仅为原 Turn/history 的 failure、message stream 的 terminal commit／outcome关联及 MCP connection operation 的 `error_message`；仍从原 proto 生成所有消费者。宿主 SessionThread、execution、实例、operation 和 request_id 不另建第二套身份源。

### 版本与数据

`git for-each-ref refs/boxteam/releases/` 本批未找到可核验的已验收发布记录，未把根 HEAD 或开发残留当 release 基线。聊天 failure 复用原 JSON metadata，不新增数据库结构版本。MCP `error_message` 是原 WorkspaceStateStore 唯一必要 DDL：按既有有序清单从 026 到 027，原 owner 的 `migrations/027.sql` 添加可空列；固定 v26 实验输入验证升级，旧 NULL 只表示没有保存原因，不补造历史事实，也不据此宣称已建立发布兼容合同。Python 生成协议仍只在 SDK 的真实 `boxteam` 包中。

### 已完成的定向验证

以下路径均相对 `out/tests/temp/2026/11/22/unify-failure-reporting/artifacts/`；原始输出和失败轮次保留，后续只对有实际修改的依赖补验。

| 检查／原命令主体 | 结果与证据 |
|---|---|
| `uv run pytest -q` 公共 SDK／core、安装与运行、MCP／027／维护和关闭定向集合 | `152 passed`，28.09s，退出 0；`primitives-final-python-consolidated.log`。预算极端／控制字符／校验输入另有 `primitives-budget-*.log`，Node SDK／browserManager 71 项与原生 Node 6 项通过 |
| SDK 离线制品真实入口 | Python wheel 插件 HTTP／维护／关闭／MCP bridge 2 项通过；Node tarball 锁及篡改检查 2 项通过、2 项未选；`primitives-sdk-offline-{python,node-archive}.log` |
| `uv run pytest -q tests/unit/api/test_request_validation.py tests/unit/services/infrastructure/config/test_reload_logging.py` | 49 项通过，7.53s，退出 0；`http-config-tests.log` |
| 六个 API response-validation 文件 | 第二轮 324 项通过，28 项仍为旧日志／文本或缺 Gateway 测试身份；迁齐后相关 job/node-debug 116 项全部通过，10.79s，退出 0，`response-validation-second.log` 与 `job-debug-validation.log`。未重复跑其余已通过文件 |
| HTTP／两跳代理／DAP／debug代理／Trace／BaseAPIException | 71 项通过，5.60s，退出 0；`http-proxy-boundaries-final.log`。覆盖大于摘要预算的原协议正文、原因与 request_id、取消和流开始后故障 |
| `bun test` HTTP／errorMessage／ChatTurn history与memo／React背景 | 76 项通过，13.53s，退出 0；`frontend-tests-final.log`。最近安全复审后仅重跑 HTTP 文件，38 项通过，0.396s，退出 0；`http-review-tests.log` |
| 聊天后端与恢复 | 原公共／存储集合、消息流／启动 25 项、恢复 6 项及 Provider fallback 2 项通过，详见 `chat/*tests*.log`。恢复测试重建 Saver／Store 后调用真实 `reconcile_stale_executions`，覆盖已提交成功／失败／取消／中断、晚到取消、重复恢复及合法控制修正；不宣称已做操作系统 SIGKILL 实验 |
| `bun test src/clients/web/src/state/tests/messageStream.test.ts` | 80 项通过，退出 0；`chat/ts-stream-tests-2.log` |
| `bun run check:protocol` | 退出 0，`protocol-check.log`；Python／Node／Web 的两种生成代码一致 |
| Workbench／Web 类型与构建 | `bun run --cwd src/clients/workbench typecheck` 包括 213 份真实转换源码、`bun run --cwd src/clients/web tsc --noEmit`、`bun run --cwd src/clients/web build` 均退出 0。原生 Workbench 基线 `fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca`，79 个内置 Web 扩展；`workbench-typecheck-final.log`、`web-typecheck.log`、`web-build-final.log`。最近 HTTP 首段脱敏变更后须收尾补验类型及构建 |
| 定向静态检查 | 主线程实际修改的 Python 文件 `uv run ruff check` 退出 0，`primary-ruff-final.log`；代理批次另留各自 ruff／Node strict 日志。检查整个 config_service 时发现未修改文件已有 25 个 TRY004，不为消除既存告警改异常类型和状态合同 |

### 当前验收限制与待补证据

- 两条正式真实发送用例在 fixture setup 被 `tests/support/ports.py` 全局容量拦截（集成／E2E 文件数 692，当前 92 端口块容量 691），没有执行业务断言。原始失败为 `chat/real-send-tests-final.log`；没有删测试、改共享端口布局或绕过其正式断言。真实浏览器改用原 `E2EProcessLease.acquire_available` 与 `start_gateway_process`，隔离 Home／workspace 和显式独立端口，后续单独记录其观察结果。
- 部分 MCP 原正式 fixture 是 session catalog v11，而当前 owner 要求 v12；没有清理其他任务的工作区。对应隔离制品和新鲜装具只证明其实际覆盖的路径。
- 真实浏览器已经完成普通成功、工具失败后继续成功、首个 Item 前配置失败及刷新历史；见下方补验记录。原生主题和安装菜单通知发现缺口，修复后仍须从真实 UI 复验。只有 Chromium headless，保留截图与原始结果，不声称保留了可见桌面窗口。
- 未做真实远端 SSH／Electron 原生运行和全部异常类型穷举；本批不以局部通过宣称所有错误已覆盖。

### 收尾补验与发现的实际缺口

- Provider 捕获处使用本次已持有 API Key 生成安全公共快照，再沿原异常身份传播；不把密钥放入异常或注册表。安全快照预算为 12 KiB，给消费者覆盖有界 code／stage 留余量；后续日志不重新格式化原 Provider cause，连日志 sink 失败的隐式 context 也不重读原密钥。主线程 API 消费同一公开说明。原异常类别、args、cause、取消与 fallback 身份保留。
- Python 诊断只以有界 deque 保留靠近抛错点的 traceback 帧引用，选中后才格式化，不读取源代码行或 locals；尾帧定向检查 9 项通过，Ruff 通过，见 `primitives-tail-frames-*`。遍历原 traceback 以找到尾帧不等于全量构造 StackSummary。
- 聊天存储／读取／恢复集合 276 项通过（49.83s），旧 owner 直接调用迁齐后 68 项通过（16.87s）；删除三个未使用的 close_* 生产接口及全部旧调用，不保留兼容入口。1 个 fork 用例要求正式 matrix runner，未在该短命令运行。Provider 共用集合 124 项、完整 upstream 集合 77 项、裸 Key 来源场景 16 项通过；真实 fallback HTTP／stream 两项通过（42.81s）。见 `chat/provider-*`、`chat/removed-close-*`、`chat/old-owner-*`。
- MCP 真实官方 SDK 内存会话经原 `_McpServerSession`／`McpCatalogOwner` 走 initialize、tools/list、tools/call：首个 isError 原文本、image、structuredContent 保留，下一次同连接成功；Owner 持续 ready、connection_ref／catalog revision 不变，SDK 会话和 tools/list 各一次，服务端 calls 恰为失败／成功两次。整文件 10 项通过（0.51s），见 `chat/mcp-real-business-result-*`；没有真实外部网络依赖。
- 浏览器真实工作区树创建会话并发送，工具读取不存在路径后模型继续成功；canonical tool_result 为 failure，但 Turn completed 且 failure=null，刷新一致。新鲜隔离工作区的工具 factory 返回 str，通过真实发送产生 TypeError、stage=agent_execution；Item=0、response_parts=[]，实时终态、无 Item 状态行和刷新历史保留同一原因。不是手工填 metadata。早期在已运行工作区修改配置未生效而误触发模型连接错误的样本另存，不冒充这一场景通过。
- 普通历史暖读与失败 metadata 历史小页各连续采样 20 次，未筛样本：普通 p95=71.10ms、max=151ms，约 36 KiB；失败 p95=56.10ms、max=59.70ms。页面无 pageerror。详细请求和 request_id 见浏览器产物。
- **快速本地失败 UI 延迟未达标**：两次冷样本 5922ms／5798ms均保留；同一暖实例另一次总 2444ms（接受 878ms、等待终态 1559ms、显示 7ms）。冷 run_step 到 factory 报错约 4.65s，热约 1.14s；错误捕获到终态落盘约 15ms。只读核对显示本批失败快照／终态处理发生在 factory 抛错后；先前 Skill 来源绑定、目录编译、依赖预载、模型准备和默认工具构造均未由本批修改。没有改动前对照或装配阶段精细计时，不能宣称已证明全部为既存开销。保持原 2s UI 门限，不增加前置校验、缓存或放宽预算；更细性能归因仍待验证。
- 实际菜单安装不存在 ZIP 返回原 422、Errno 2 路径和 request_id，但关闭菜单后异步 ActionRunner 通知监听已释放，通知中心确为空。两个管理菜单命令现复用原 `runOperation` 交付一次通知，程序化 command 仍 reject；没有另造通知服务。真实原生主题加载 JSON 返回 500 时，原 picker 仅交全局 onUnexpectedError、恢复旧主题，却没有通知；现由原 picker 调原 `INotificationService`，保留原恢复链路。两项均须使用收尾构建复验，不能用源码修改替代验收。
- 真实子进程 stderr 专项：插件写入 2 MiB（含首尾标记、URL 凭据）后 exit 42；启动及同 slot 状态重读全程禁止读取原 raw 日志，记录 reads=[]。公共状态只含启动／退出事实和 log_id，少于 8 KiB，无 stderr／秘密；独立核对原日志完整保留原字节。复用既有测试 fixture，不加 supervisor 捕获缓存或日志扫描。该文件与 SDK 尾帧共 32 项通过（22.76s），Ruff 通过；`primitives-stderr-and-tail-final.log`、`primitives-stderr-ruff-final.log`。
- 最终最小充分复审找到真实 preitem 链路在 `thread_residency` 后台 builder 回调仍输出 raw exc_info，已改为同一 `report_failure`，保持回调、异常身份和重试资格。既有真实异步 task 用例增加主因 password、cause token 脱敏和不传 exc_info 断言；整文件 18 项通过（1.83s），Ruff／差异通过，见 `chat/thread-runtime-safe-report*`。未扩修未涉及的 LLM 日志轮转逻辑。
- 收尾类型与构建：`web-typecheck-close.log` 退出 0；`workbench-typecheck-theme-fixed.log` 对 214 份实际转换源码检查退出 0；`web-build-theme-fixed.log` 完整构建 79 个内置 Web 扩展退出 0。主题通知首轮误重复引入上游已有 INotificationService，类型检查指出后删除该重复 import，再运行上述最终检查；失败原件 `workbench-typecheck-theme-notification.log` 保留。原安装命令拒绝语义相关 47 项通过（8.99s），见 `command-rejection-close.log`。公共 Python 定向 Ruff 和 OpenSpec strict 也通过，见 `primary-ruff-close.log`／`openspec-validation-close.log`。
- 最终浏览器观察：新构建的插件菜单安装不存在 ZIP，唯一 notification-list-item=1，保留真实失败原因；`representative-plugin.png`。原生 Abyss 主题 JSON 被 route 返回 HTTP 500 后，一条通知显示原 `Internal Server Error`／主题路径，实际主题仍为 Light 2026。测试脚本曾错误要求通知包含数字 500，保留其失败原件并更正证据解释，不据此否定原 HTTP reason 保真，也不为该误断言修改产品。
- 原配置来源 JSONC 编辑器真实键盘输入候选并 Ctrl+S，调用 `workspace_root_mutable_override/preflight` 返回 422：原 JSONC parser 的 ValueError、行列及 request_id 均保留，头体 request_id 一致；原 file_digest、active_revision 和 layer_revision 不变。刷新仍保留原 dirty 草稿，未将拒绝候选当作已生效配置。见 `config-submit-result.json`、`representative-config-submit-failure.png`、`representative-config-refreshed.png`。此前设置页只读截图不再作为“通过 UI 提交”的证据。主题移除在当前只读内置主题／自有插件协议下没有支持的 UI 路径，不为测试恢复旧 Gallery／扩展平台；启动缺失主题仍未实测。
- 额外故障探针复现 LLM 请求日志目录被普通文件占用时，FileExistsError 会替代原 Provider 异常，并在隐式 context 中携带裸 Key。仅修改原 LLMLoggingMiddleware 同步／异步失败记录出口：落盘失败自身的安全 message／type 作为次生事实附 notes 并记录一次，随后原样抛出 Provider；不改变原异常身份／args／cause／context，也不让通用 formatter 再递归读取外层日志异常。不新增异常 wrapper 或安全注册机制。真实文件阻塞回归和该文件全部 15 项通过（1.26s），4 项旧“只有类型、没有原因”预期按本变更合同迁为精确真实原因，未放宽身份、顺序或 dispatch 断言。见 `chat/nested-provider-cause-probe*`、`chat/provider-log-sink-owner-*`。
- 正常主题预览／Esc 独立验收退出 0：Light 2026 → Kimbie Dark（vs-dark）→ Esc 还原 Light 2026，pageErrors=[]。见 `theme-preview-result.json`、`representative-theme-preview.png`、`representative-theme-cancelled.png`。配置 422 提交的截图明确显示原生 JSONC 语法诊断／Problems=1 和 dirty 草稿；通知中心当时为空，未证明后端拒绝原因通知可见，因此 6.4 仍保留未勾选，连同启动缺失主题恢复的未验范围如实交付，不把 HTTP 回执当成 UI 原因展示通过。
- 临时运行环境已清理：`browser-cleanup.json` 记录 owned_remaining／browser_descendants_remaining 均为空，39380／39400 均不监听，租约释放；模型回放控制线程由原 fixture close／join。只有此任务的隔离实例被停止，固定 dev 未动。截图、真实请求结果、失败原件、源码指纹和命令日志保留在同一 artifacts 目录供复核，没有新增独立总结文档。
- 同一 LLM 日志 owner 的轮转 unlink 故障也经真实文件故障复现：原 warning 的 raw exc_info 会沿隐式 context 输出原 Provider Key。该 warning 现只记录有界路径、OSError 自身类型／安全消息，不传原异常给 formatter，保留轮转失败留痕及继续处理语义。同步／异步 mkdir 与 unlink 场景合并后整文件 17 项通过（1.24s），Ruff／差异通过；失败原件 `chat/provider-prune-unlink-before.log` 和最终 `chat/provider-prune-unlink-{verified,ruff-final}.log`／源码指纹均保留。没有继续扩展到其它日志 owner。

### 快速失败延迟的进一步定位（2026-11-22）

用户要求排查延迟，本轮只新增临时诊断装配与产物，没有修改产品代码或固定 dev。证据位于同一 artifacts 下的 `latency-diagnosis/`，汇总原始样本及源码指纹为 `analysis.json`。经真实 Gateway、隔离工作区和 Workbench 创建会话、发送同一无效 factory 场景，对比测试控制时钟／ID与产品默认时钟／ID；两者都能复现首次 SDK 初始化。cProfile 只用于定位调用，不把带 profiler 的 6～12s 样本当产品延迟，也不相加其异步／重入累计时间。

最后一轮使用产品默认时钟／ID，完全关闭 cProfile，仅保留阶段起止计时，并在真实 React ShadowRoot 内用 MutationObserver 记录错误实际可见时间，不先轮询 Job。结果如下，部分阶段互相包含，不能逐行相加：

| 阶段 | 冷请求 | 同进程暖请求 |
|---|---:|---:|
| 点击发送至错误 DOM 可见 | 5378ms | 2067ms |
| 消息 POST 发出至响应 | 812ms | 1049ms |
| Thread runtime acquire 全程 | 4131ms | 664ms |
| 其中 Agent 装配 | 3934ms | 633ms |
| 其中模型准备 | 3824ms | 2ms |
| 其中首次 LiteLLM 导入 | 3629ms | 无首次导入 |
| Skill catalog 构建 | 32ms | 8ms |
| 首个包含该失败的 WS frame 至 DOM 可见 | 9ms | 21ms |

冷请求主因已明确：`preload_agent_dependencies()` 只预载 Agent 工厂和 `openai.resources`；`assemble_agent()` 仍先 `prepare_agent_model()`，在选用 LiteLLM 的 Provider 时通过 `litellm_runtime.__getattr__('ChatLiteLLM')` 首次导入 LiteLLM。导入加载大量 Provider／Pydantic 类型，随后才进入默认工具、自定义工具构建；本样本的无效 factory 在自定义列表末尾，所以一个本地即可确认的错误被迫等待模型 SDK 初始化结束。属于现有装配顺序和懒加载边界，不是新公共错误快照／历史恢复造成的主耗时。

暖请求没有这次 SDK 导入，但失败 runtime 不会作为成功实例缓存，下一次会重新构建工具及 schema。函数采样显示工具函数转 schema、Pydantic 动态类型／subset model 构造占有明确时间；关闭 profiler 的几次暖样本中 Agent 装配约 128～633ms，消息 POST 约 456～1049ms，整体 DOM 可见约 928～2067ms，存在调度与 I/O 波动。这不是 p95 验收，也未进一步定位消息准入内部每次数据库／锁操作，因此不据少量样本宣布全部满足 2s。

原 2444ms 暖样本是在 Job 轮询确认 failed 后才检查 UI，含观察等待，不能视为精确 DOM 延迟；本轮已将真实 DOM 时间与 HTTP／WS 时间分开，保留原样本，不用新样本覆盖旧证据。初轮临时观察器挂在 ShadowRoot 外而超时，及脚本以文件入口运行导致根包不在 sys.path 的失败原件也保留；修正诊断入口后两轮完整退出 0。

后续优化应优先评估把原自定义工具构建／返回值校验移到模型初始化之前，或按当前实际 Provider 在就绪前预载所需 SDK；不新增第二套预检、不缓存绑定 Thread 的工具对象来掩盖失败、不把所有 Provider 都无差别预热。暖路径如继续优化，应先细分现有消息准入和工具 schema 构造。上述是排查结论及建议，本轮尚未实施这类行为／装配顺序变更。

临时实例均按原 fixture 清理，`latency-diagnosis/cleanup.json` 记录没有残留 Gateway／Backend、40780／40800 均未监听、租约释放。新增产物为阶段 JSONL、原始 cProfile／文本视图、浏览器请求／DOM时间、截图、脚本与指纹；没有新增长期总结文档。
