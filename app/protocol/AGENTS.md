# 目录用途

`app/protocol/` 存放 Python 进程边界使用的公开协议绑定、codec 和 adapter。

## 可修改内容

- 可以维护 Python 协议生成入口、JSON/SSE/WS codec 和边界适配器。
- `schema.py` 从 descriptor 核对公开 JSON 的字段、嵌套结构和类型；异形消息仅由具名 adapter 投影或明确接管。业务联合的各分支合计必须完整覆盖协议字段，只有具备 presence 的协议字段可因分支而省略。业务取值与字段间约束仍属于内部 Pydantic。
- Session SSE 的唯一生成投影从 wire descriptor 推导信封、展平 header、事件标签和 payload；状态联合保留既有业务 adapter，不能将这项具名接管扩为跳过其它结构。
- `openapi.py` 只登记已迁齐的 HTTP 根模型；导出与协议生成均在发布产物前递归校验真实 OpenAPI，并可消费本次 Buf 编译的 descriptor。
- 文件 HTTP 的请求、目录、内容、操作回执和快捷目录共用该检查；嵌套结构从 proto 递归推导，不另列字段映射。原生路径输入与 URI 输出校验继续由对应业务模型负责。
- Terminal／Browser 的资源创建、管理、传输授权和会话资源 HTTP 共用该检查；嵌套 Thread 身份、资源引用和列表成员由 descriptor 递归核对，版本与业务动作约束仍属于 Pydantic 和资源 owner。
- Node Debug 的配置、状态与请求共用同一检查；既有 `action + params` 仅在此投影到 proto oneof，成员和参数结构从 descriptor 推导，不另维护动作映射。

## 不可修改内容

- 不要把 Agent 业务规则或 Workspace 内部事件总线实现放到这里。
- 不要手工修改 `generated/` 下的生成文件。

## 规范

- codec 负责线格式转换，adapter 负责连接业务模型与协议模型。
- 解析失败必须抛出带字段和边界信息的明确错误。
- 能从 descriptor 推导的字段与枚举映射不逐项复制；具名业务别名和不同外形的 adapter 明确保留业务职责。
