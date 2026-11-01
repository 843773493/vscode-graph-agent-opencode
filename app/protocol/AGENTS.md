# 目录用途

`app/protocol/` 存放 Python 进程边界使用的公开协议绑定、codec 和 adapter。

## 可修改内容

- 可以维护 Python 协议生成入口、JSON/SSE/WS codec 和边界适配器。
- `schema.py` 从 descriptor 核对公开 JSON 的字段、嵌套结构和类型；异形消息仅由具名 adapter 投影或明确接管。业务联合的各分支合计必须完整覆盖协议字段，只有具备 presence 的协议字段可因分支而省略。业务取值与字段间约束仍属于内部 Pydantic。
- Session SSE 的唯一生成投影从 wire descriptor 推导信封、展平 header、事件标签和 payload；状态联合保留既有业务 adapter，不能将这项具名接管扩为跳过其它结构。
- `openapi.py` 只登记已迁齐的 HTTP 公开模型；导出与协议生成均在发布产物前递归校验真实 OpenAPI，并可消费本次 Buf 编译的 descriptor。
- 工作区信息、上下文和索引响应共用该检查；动态元数据仍按原 Struct 合同传递，其位置字段的序列化约束继续归 Pydantic 与原 owner。
- Agent 目录、详情和默认 Agent／Provider 设置共用该检查，嵌套 Provider 由 descriptor 递归核对；默认值和配置可用性仍归 Pydantic 与原 owner。
- Workspace 与 Gateway 的 HTTP 根复用同一个递归检查入口；Gateway 目录浏览、原生目录绑定及解析的结构从其 descriptor 核对，原生输入许可、目录 URI 和授权仍由既有 Pydantic／owner 负责。
- Gateway 受管工作区的创建请求与列表响应共用该检查，列表成员从 descriptor 递归核对；原生目录输入、默认创建行为、名称与额外字段约束仍归 Pydantic，授权及生命周期仍归原 owner。
- Gateway SSH 连接选项列表与远端连接请求共用该检查，选项成员由 descriptor 递归核对；连接来源互斥、端口和原生凭据路径输入仍由 Pydantic 与既有连接 owner 校验。
- Gateway 端口转发的创建、改标签、改本机端口请求及完整列表共用该检查，列表成员由 descriptor 递归核对；端口范围、协议、标签及 loopback 等业务约束仍归 Pydantic 与原 owner。
- Gateway 用户列表、创建及访问租约的请求和响应共用该检查，用户及租约成员由 descriptor 递归核对；名称、跟踪值、租约互斥和授权仍归 Pydantic 与原 owner。
- Gateway 健康、诊断及开发重启回执共用该检查，诊断工作区、日志、进程和日志文件身份由 descriptor 递归核对；进程与计数范围、状态、管理授权及原始日志入口例外仍归 Pydantic 与原 owner。
- Gateway 工作区列表内的服务状态共用该检查，状态、健康路径、端口与错误字段由 descriptor 核对；服务状态取值及生命周期仍归 Pydantic 与原 owner。
- Gateway 启停、重启回执中的阻塞资源共用该检查，工作区、会话、资源及状态字段由 descriptor 核对；阻塞类型和生命周期仍归 Pydantic 与原 owner。
- Gateway 联邦工作区、入站访问范围、资源目录与会话搜索共用该检查，工作区、peer、目录条目及搜索错误由 descriptor 递归核对；后端身份、服务集合、查询状态和管理授权仍归 Pydantic 与原 owner。资源条目的 Struct 快照边界沿用原合同，内部资源结构由工作区协议负责。
- Gateway 运行组请求的 `desired_workspace_ids` 仅在结构检查时由 HTTP 可空数组投影到 proto 的具名 presence 包装消息，根及包装成员仍由同一 descriptor 检查；HTTP 未指定、清空和非空集合的原语义、完整组前置条件及去重约束仍归 Pydantic 与生命周期 owner。
- Gateway配置来源、重载状态、事件及pending证明／丢弃请求共用该检查，来源与事件成员递归推导；policy manifest保留原Struct边界，缺失来源摘要用Value保留显式null。URI、摘要值域、修订及证明约束仍由Pydantic和配置owner负责，不从descriptor替代业务校验。
- 联邦manifest、完整工作区列表及启停／重启回执共用该检查，远端连接摘要与配置游标递归核对；原服务快照Struct边界、联邦深度、配置状态及生命周期前置条件继续归Pydantic和原owner。
- Gateway 工作区注册、更新、排序、激活及导航树与面包屑共用该检查，嵌套导航节点由 descriptor 递归核对；原生路径输入、显式空值、节点类型配对与放置约束仍归 Pydantic，导航和生命周期仍归原 owner。
- 文件 HTTP 的请求、目录、内容、操作回执和快捷目录共用该检查；正文修改的命令、版本字典、回执及前后件快照也从 proto 递归核对，不另列字段映射。原生路径输入、传输封套的固定 URI、版本和命令互斥校验继续由对应业务模型负责。
- Terminal／Browser 的资源创建、管理、传输授权和会话资源 HTTP 共用该检查；嵌套 Thread 身份、资源引用和列表成员由 descriptor 递归核对，版本与业务动作约束仍属于 Pydantic 和资源 owner。
- Job 详情、步骤、控制、派发快照及 Session Goal 共用该检查；原时间字段、控制目标、队列计数和预算约束继续归 Pydantic，不将任意 JSON 正文当作具名 proto 消息。
- Fork 的来源点、创建及跨 Gateway 传输共用该检查；冻结 capture、资源 owner、发布结果和分块成员从 descriptor 递归核对，锚点、资源选择、授权角色及分块边界仍由原 Pydantic 和业务 owner 校验。
- Session 创建、更新、查询与信息快照共用该检查；时间、来源、资源汇总及错误成员由 descriptor 递归核对，URI、fork 来源和展示长度约束仍由原 Pydantic 与 owner 负责。
- Session bootstrap 与 main／精确 Thread 历史共用该检查；Turn、响应部件、canonical 来源及 internal execution 展示成员由 descriptor 递归核对，历史选择器互斥、分页预算与 canonical／streaming 身份仍归原 Pydantic 与 owner。
- 会话文件变更的列表、详情和审查入口共用该检查；汇总、文件条目及审查回执由 descriptor 递归核对，文件 URI、变更类别和审查状态仍由原 Pydantic 与 owner 负责。
- 上下文读取、搜索及 Skill 停止跟踪共用该检查；嵌套资源来源、搜索坐标、部分错误和回执从 descriptor 递归核对，身份、URI、内容预算及 Skill 状态约束仍由原 Pydantic 与 owner 负责。
- MCP 连接管理、授权、工具目录、开关与测试记录共用该检查；工具成员、选择变更、测试样本与 provider 结果从 descriptor 递归核对，操作状态、敏感字段及重复工具等业务约束仍归原 Pydantic 和 owner。
- 持久任务的定义、控制、通知、运行详情和生成器导入共用该检查；动作联合、运行输入消费与嵌套组件由 descriptor 递归核对，时间计划、组件配对、Thread 隔离和版本约束仍归原 Pydantic 与 owner。
- 会话生成器的命名预览与定义校验共用该检查，嵌套策略、挂载及上下文来源从 descriptor 递归核对；命名长度、目标配对及 provider 可用性与原因的关系仍归原 Pydantic 和 owner。
- 运行时状态、排空结果与快照回执共用该检查，阻塞资源成员由 descriptor 递归核对；生命周期及阻塞类型仍归 Pydantic，HTML快照请求保留原入口职责。
- Node Debug 的配置、状态与请求共用同一检查；既有 `action + params` 仅在此投影到 proto oneof，成员和参数结构从 descriptor 推导，不另维护动作映射。

## 不可修改内容

- 不要把 Agent 业务规则或 Workspace 内部事件总线实现放到这里。
- 不要手工修改 `generated/` 下的生成文件。

## 规范

- codec 负责线格式转换，adapter 负责连接业务模型与协议模型。
- 解析失败必须抛出带字段和边界信息的明确错误。
- 能从 descriptor 推导的字段与枚举映射不逐项复制；具名业务别名和不同外形的 adapter 明确保留业务职责。
