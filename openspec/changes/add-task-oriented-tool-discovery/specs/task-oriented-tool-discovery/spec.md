## Purpose

为模型提供跨已有扩展来源的统一任务级工具发现与精确定义读取能力，使模型在不自动注入全部工具 schema 的情况下获得可靠的调用契约、连接需求和版本信息，并与当前工作区、权限和上下文恢复保持一致。

## ADDED Requirements

### Requirement: 通用工具发现支持任务查询与精确定义读取

系统 SHALL 提供只读 `search_tools`，使用互斥的 `query` 或非空 `tool_ids` 选择器，支持有界条目数、响应字节数和分页。任务查询 MUST 返回相关工具的稳定 ID、用途、匹配理由、实际调用入口、完整 input schema 或其明确可读取引用、schema hash、目录 revision、有效策略和连接 readiness；精确模式 MUST 按请求顺序返回逐 ID 结果。首期 SHALL 支持已有 MCP、本地扩展和本地连接控制目标，不创建 MCP 专用搜索或独立 schema 获取工具。

#### Scenario: 按用户任务发现跨来源候选

- **WHEN** 模型搜索“查找失败 CI 并获取日志”，冻结目录存在相关 MCP 和本地扩展工具
- **THEN** 结果返回相关的有限候选、匹配事实、完整定义与信封调用信息，不执行候选或声称任务完成

#### Scenario: 按 ID 获取完整参数定义

- **WHEN** 模型提交多个精确工具 ID，其中一个不存在或不可发现
- **THEN** 结果按请求顺序提供定义或逐项明确错误，不返回伪造的空 schema

#### Scenario: 选择器非法

- **WHEN** 请求同时提供 query 与 tool_ids，或两者都为空
- **THEN** 工具明确返回输入错误，不静默选择一种解释

### Requirement: 搜索结果有界且不得截断参数契约

系统 MUST 以冻结目录、策略和查询身份约束结果及 cursor；同一输入和快照的排序及分页 MUST 确定。每份可调用 schema MUST 完整保留必填、组合、枚举、附加字段规则和本地引用定义。超过响应上限时 SHALL 减少候选并返回续读信息；单个定义无法内联时 MUST 复用现有 ToolOutputStore／会话 artifact 保存完整内容，并返回有界 schema_ref、read_path、content_sha256、体积、schema hash 和冻结 provenance，以 schema_state=needs_read 区分于 inline_complete。引用读取前 MUST 不把预览视为完整定义。schema 方言不支持、外部引用无法解析、引用也超出预算或物化失败时 MUST 明确报错，不输出截断或宽松替代定义。最终模型结果 MUST 同时遵守现有工具策略和输出中间件的字节／行数约束，不建立新的 schema 存储或专用读取入口。

#### Scenario: 完整 schema 大于单次响应上限

- **WHEN** 单一工具的完整定义超过允许响应字节数
- **THEN** 返回 needs_read、完整内容的不可变引用及摘要和体积，模型可用既有文件读取工具取得完整定义，不把部分 properties 冒充完整 schema

#### Scenario: 搜索输出经过真实工具结果裁剪

- **WHEN** 搜索结果经过 ToolOutputMiddleware，候选定义超过最终字节或行数预算
- **THEN** 模型收到完整内联定义或明确 needs_read 的可读取引用，不能收到被标为 inline_complete 的裁剪预览；完整内容和冻结 schema 身份可核验

#### Scenario: 完整定义无法物化

- **WHEN** 大定义需要物化而工具结果存储写入失败，或连引用都无法放入预算
- **THEN** 返回明确存储或预算错误，不伪造可读取引用、不静默降为截断定义

#### Scenario: 分页期间目录或策略改变

- **WHEN** 模型携带旧目录或策略绑定的 cursor 请求下一页
- **THEN** 系统明确返回快照冲突或重新搜索指引，不拼接不同版本结果

### Requirement: 显式发现与自动注入和执行控制分离

`model_visible` SHALL 继续控制自动注入；显式搜索 SHALL 能按需返回 execution_enabled=true、model_visible=false 且未命中硬隐藏的目标定义，不修改用户选择。任务搜索、精确查询、导航及压缩后恢复 MUST 复用 ToolPolicyResolver 的完整结果，服从 restrictions.model_hidden／model_visibility_locked，不重新投递硬隐藏目标定义；历史原件保持不变，硬隐藏不被扩大为执行禁用。执行被硬策略禁用的目标 MUST 不通过搜索返回完整调用定义或恢复执行能力。目标未授权但有已验证或显式可信的已知目录时 SHALL 返回连接需求并标明不可执行；未知目录 MUST 明确要求先连接，不虚构工具。

冻结目录 MUST 保留已登记目标的成员身份与完整定义事实，不能因执行策略禁用就将已知目标变成不存在。精确查询已知禁用或硬隐藏目标时 SHALL 分别返回不含定义的禁用或隐藏错误；目录成员存在 MUST 不被视为执行授权，真实执行仍独立检查最新权限。旧冻结目录缺少的目标 MUST 不从当前目录补齐。

#### Scenario: 首次发布前已经禁用

- **WHEN** 本地扩展或 MCP 目标在首次目录发布前已被执行策略禁用，随后目录被封存并冷读取
- **THEN** 精确查询仍返回已知禁用错误，普通搜索、导航和自动注入不投递其定义；信封调用得到配对的策略拒绝且远端零执行

#### Scenario: 隐藏自动注入后显式搜索

- **WHEN** 工具允许执行、仅关闭自动注入且未命中 model_hidden，模型明确搜索相关任务
- **THEN** 工具结果可以提供完整定义，同时自动注入选择和执行策略保持原值

#### Scenario: 静态硬隐藏覆盖显式请求和恢复

- **WHEN** 目标命中 restrictions.model_hidden，模型按任务或精确 ID 查询，或压缩后尝试恢复此前发现的定义
- **THEN** 发现和恢复均不重新投递该目标定义或通过导航泄露定义，硬隐藏不能被关闭自动注入的规则绕过，已有历史原件不被改写

#### Scenario: 已知未授权与未知目录

- **WHEN** 一个未授权 Server 有已验证目录，另一个 Server 从未连接也无可信目录
- **THEN** 前者返回带不可执行标记的已知候选与连接步骤，后者只返回先连接才能发现的事实

### Requirement: 定义发现与恢复遵循冻结上下文边界

搜索 MUST 消费发起调用的冻结目录；新目录及定义变更 MUST 在现有允许的激活边界生效，不因搜索或授权完成突破 turn 冻结。自动导航 SHALL 有界，用户显式开启的自动完整定义注入 SHALL 保留。发现定义的来源和版本 MUST 随工具结果保存，压缩后可恢复需保留的活动定义；原定义仍在有效上下文中时 MUST 不重复注入。更新 MUST 提供完整替换定义或删除事实，不让模型从不完整 diff 猜测契约。

完整定义 MUST 随父目录以同一 ResourceRegistry 的 definition facet、typed members 和 lineage 一致发布，并由既有 activation／Saver 正文存储冻结；不得只保存 schema hash 或在首次搜索后才保存定义。搜索与恢复 MUST 读取所属冻结正文，不从 live 同名工具补齐，也不把全部 definition facet 自动注入模型上下文。

#### Scenario: 首次搜索前已有冻结完整定义

- **WHEN** execution 已冻结目录但尚未调用 search_tools，随后 live Server 修改了 schema
- **THEN** 首次搜索仍从该 execution 的冻结正文读取原完整定义与 hash，缺失正文时明确失败，不读取当前同名工具

#### Scenario: 重启后读取原冻结定义

- **WHEN** 进程重启，精确查询按原 execution 的合法恢复合同继续读取定义
- **THEN** 从既有 Saver 恢复相同定义和 provenance；缺失历史正文时明确失败，不用新连接目录重建历史

#### Scenario: 发现后契约发生变化

- **WHEN** 模型已读取旧定义，随后目录更新，而当前 Turn 采用 turn 激活策略
- **THEN** 当前冻结目录保持不变，旧调用只使用原绑定或明确失败；下一允许边界同时提供新定义与新绑定

#### Scenario: 压缩后继续使用已发现工具

- **WHEN** 压缩移除了原发现结果的正文，而后续仍保留该活动工具定义
- **THEN** 系统从原冻结资源恢复完整定义及 provenance，不从当前同名工具重建历史，也不建立第二份权威目录

### Requirement: 动态发现保持 Provider 工具面稳定

系统 MUST 维持固定的 search_tools 与 invoke_extension_tool 定义；动态目录、连接列表和目标 schema MUST 不嵌入其 Provider description 或参数集合。首次新增 search_tools SHALL 按既有 ToolSet 变更处理；其后仅内层目录变化 MUST 不自动造成 ToolSet epoch 变化。缓存收益 MUST 以真实请求及 cached tokens 观测，不保证命中。

#### Scenario: 加入新的 MCP 工具

- **WHEN** 已安装 search_tools 的运行时发布新目标并在允许边界激活
- **THEN** 两个固定入口的 Provider schema 保持一致，模型通过正常结果／上下文更新获得新定义，目录变化本身不制造 ToolSet epoch
