## Purpose

为模型提供跨已有扩展来源的统一任务级工具发现与精确定义读取能力，使模型在不自动注入全部工具 schema 的情况下获得可靠的调用契约、连接需求和版本信息，并与当前工作区、权限和上下文恢复保持一致。

## ADDED Requirements

### Requirement: 通用工具发现支持任务查询与精确定义读取

系统 SHALL 提供只读 `search_tools`，使用互斥的 `query` 或非空 `tool_ids` 选择器，支持有界条目数、响应字节数和分页。任务查询 MUST 返回相关工具的稳定 ID、用途、匹配理由、实际调用入口、完整 input schema、schema hash、目录 revision、有效策略和连接 readiness；精确模式 MUST 按请求顺序返回逐 ID 结果。首期 SHALL 支持已有 MCP、本地扩展和本地连接控制目标，不创建 MCP 专用搜索或独立 schema 获取工具。

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

系统 MUST 以冻结目录、策略和查询身份约束结果及 cursor；同一输入和快照的排序及分页 MUST 确定。每份可调用 schema MUST 完整保留必填、组合、枚举、附加字段规则和本地引用定义。超过响应上限时 SHALL 减少候选并返回续读信息；单个定义超过上限、schema 方言不支持或外部引用无法解析时 MUST 返回明确错误，不输出截断或宽松替代定义。

#### Scenario: 完整 schema 大于单次响应上限

- **WHEN** 单一工具的完整定义超过允许响应字节数
- **THEN** 返回 definition-too-large 与体积事实，不把部分 properties 冒充完整 schema

#### Scenario: 分页期间目录或策略改变

- **WHEN** 模型携带旧目录或策略绑定的 cursor 请求下一页
- **THEN** 系统明确返回快照冲突或重新搜索指引，不拼接不同版本结果

### Requirement: 显式发现与自动注入和执行控制分离

`model_visible` SHALL 继续控制自动注入；显式搜索 SHALL 能按需返回 execution_enabled=true 且 model_visible=false 的目标定义，不修改用户选择。执行被硬策略禁用的目标 MUST 不通过搜索返回完整调用定义或恢复执行能力。目标未授权但有已验证或显式可信的已知目录时 SHALL 返回连接需求并标明不可执行；未知目录 MUST 明确要求先连接，不虚构工具。

#### Scenario: 隐藏自动注入后显式搜索

- **WHEN** 工具允许执行但关闭自动注入，模型明确搜索相关任务
- **THEN** 工具结果可以提供完整定义，同时自动注入选择和执行策略保持原值

#### Scenario: 已知未授权与未知目录

- **WHEN** 一个未授权 Server 有已验证目录，另一个 Server 从未连接也无可信目录
- **THEN** 前者返回带不可执行标记的已知候选与连接步骤，后者只返回先连接才能发现的事实

### Requirement: 定义发现与恢复遵循冻结上下文边界

搜索 MUST 消费发起调用的冻结目录；新目录及定义变更 MUST 在现有允许的激活边界生效，不因搜索或授权完成突破 turn 冻结。自动导航 SHALL 有界，用户显式开启的自动完整定义注入 SHALL 保留。发现定义的来源和版本 MUST 随工具结果保存，压缩后可恢复需保留的活动定义；原定义仍在有效上下文中时 MUST 不重复注入。更新 MUST 提供完整替换定义或删除事实，不让模型从不完整 diff 猜测契约。

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
