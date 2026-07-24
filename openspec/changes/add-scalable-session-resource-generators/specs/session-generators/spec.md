## ADDED Requirements

### Requirement: 可信生成器类型注册
系统 SHALL 通过稳定 `type_id + version` 注册 Session Generator Provider，并 MUST 使用显式配置 Schema 校验实例配置，禁止从持久化 JSON 动态执行任意代码。

#### Scenario: 目标工作区缺少 Provider
- **WHEN** 生成器目标工作区未声明所需类型和版本
- **THEN** Gateway 将生成器标记为 blocked 并显示缺失能力，不得换用其他版本

### Requirement: 生成器关系独立建模
GeneratorDefinition SHALL 分别保存 trigger、placement、execution、context source、created from、naming layout、session strategy 和 policies；跨工作区会话引用 MUST 包含 workspace ID 与 session ID。

#### Scenario: 从 Gateway 控制面挂载到另一工作区
- **WHEN** 用户在当前工作区 A 的界面创建生成器，将其挂到工作区 B 的会话并读取工作区 B 的上下文
- **THEN** Gateway 保留完整 locator，并路由到工作区 B 执行

#### Scenario: 拒绝尚未支持的执行环境分离
- **WHEN** 生成器要求会话存储、live session 上下文和 Agent 执行分别位于不同工作区
- **THEN** Gateway 返回明确 4xx 能力错误，不得把其它工作区 locator 当作目标工作区的本地 ID

### Requirement: 自定义命名路径安全可预览
生成器 SHALL 分别使用受限目录路径段模板与会话名称模板渲染物理路径，MUST 拒绝路径穿越、绝对路径、非法分隔符和保留名称，并 SHALL 强制最终会话目录包含稳定 ID。稳定 ID 尚未分配时预览 MUST 明确返回物理路径模板且与落盘共用同一规范化规则；运行结果 MUST 返回实际物理相对路径，不得追加未展示的隐式层级。

#### Scenario: 预览日期命名路径
- **WHEN** 用户预览包含生成器名称、年月日、时分秒和会话标题的模板
- **THEN** Gateway 返回完整相对路径模板、规范化名称、稳定 ID 后缀占位语义和所有校验错误且不创建会话

#### Scenario: 在挂载节点下物理创建输出
- **WHEN** `new_per_run` 或 `fork_new_and_report_back` 生成器挂载到工作区、会话或会话文件夹并触发
- **THEN** 工作区和文件夹挂载直接以对应目录为基准；会话挂载以锚点会话的保留 `children/` 边界为基准，新会话的 `parent_session_id` 指向该锚点会话，新会话本体创建在渲染目录中，目录预览、breadcrumb 与实际相对路径一致

### Requirement: 支持三种会话策略
生成器 SHALL 支持 `new_per_run`、`continue_existing` 和 `fork_new_and_report_back`，并 MUST 将 UI 激活策略与后端会话创建分离。

#### Scenario: 每次创建新会话
- **WHEN** `new_per_run` 生成器触发两次
- **THEN** 两次运行在渲染后的物理目录中产生不同稳定 ID 的会话并分别记录来源

#### Scenario: 继续指定会话
- **WHEN** `continue_existing` 生成器触发且目标会话已有活动 Job
- **THEN** 第一版按显式 `queue` 策略串行排队，不得交叉写入消息；其它并发策略在实现前不得写入公开配置

#### Scenario: 新会话完成后旧会话继续
- **WHEN** `fork_new_and_report_back` 运行完成且回报模式为 `continue_agent`
- **THEN** 新会话保留完整过程，旧会话按队列收到结构化回报并继续新 Job，Web 保持原会话除非 ui policy 要求切换

### Requirement: 运行账本与幂等
每次触发 SHALL 创建 GenerationRun，保存稳定 idempotency key、状态、错误和零个或多个输出 locator；相同幂等键重试 MUST 返回同一运行而不得重复创建会话。工作区执行器 MUST 在分派前持久化稳定的输出 session ID、完整消息意图、message ID 与保留的 job ID，并 SHALL 在重启后从最后一个持久阶段继续。Gateway MUST 以工作区生成运行状态为权威终态来源，不能只依赖进程内监控任务或单个 Job 快照。

#### Scenario: Gateway 重启后重试计划运行
- **WHEN** Gateway 在目标工作区已创建输出但写回完成状态前重启
- **THEN** 重试通过幂等键恢复既有输出并完成同一 GenerationRun

#### Scenario: 工作区在消息持久化后重启
- **WHEN** 工作区在生成消息已经持久化、但子 Job 尚未确认分派时重启
- **THEN** 执行器复用同一输出 session ID、message ID 与预留 job ID 分派，不创建第二条用户消息或第二个输出会话

#### Scenario: 无回报模式仍持久化终态
- **WHEN** `new_per_run` 或回报模式为 `none` 的生成 Job 完成或失败
- **THEN** 工作区生成账本持久化 `completed` 或 `failed`，Gateway 轮询该账本并同步同一终态

### Requirement: 定义保存前验证稳定定位器
Gateway SHALL 在把生成器定义保存为 ready 前，通过目标工作区目录 API 验证 placement、session strategy target 与 live session context locator 的存在性和末节点类型；同一 locator 的重复语义校验 SHOULD 共用一次目录探测。

#### Scenario: 会话定位器指向文件夹
- **WHEN** placement、策略目标或 live context 声明为会话，但稳定节点 ID 实际指向文件夹
- **THEN** Gateway 返回包含 expected/actual kind 的明确 4xx，不得保存一个必然失败的 ready 定义

### Requirement: 挂载失效和离线可观察
挂载会话删除、工作区移除、工作区离线或目录丢失 SHALL 产生明确 paused、blocked 或 failed 状态，MUST NOT 自动改绑或伪装成功。

#### Scenario: 挂载会话被删除
- **WHEN** 生成器挂载的会话不存在
- **THEN** 生成器暂停并提供重新挂载或删除入口，不自动提升到工作区根
