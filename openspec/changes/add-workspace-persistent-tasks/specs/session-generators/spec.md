## MODIFIED Requirements

### Requirement: 可信生成器类型注册

系统 SHALL 通过稳定 `type_id + version` 注册会话生成动作 Provider，并 MUST 使用显式配置 Schema 校验实例配置，禁止从持久化 JSON 动态执行任意代码。既有定时生成器 SHALL 迁移为“定时触发＋无附加条件判断＋会话动作”的工作区持久任务预设组合，类型和配置由所属工作区验证，不保留独立定时生成任务模型或专用调度运行时；用户可在同一任务开启脚本或模型附加条件。

#### Scenario: 目标工作区缺少 Provider
- **WHEN** 生成器目标工作区未声明所需类型和版本
- **THEN** 所属工作区拒绝创建或明确将既有任务标记为 blocked 并显示缺失能力，Gateway 只透明传输结果，不得换用其他版本

### Requirement: 生成器关系独立建模

生成器任务 SHALL 分别保存 trigger、placement、execution、context source、created from、naming layout、session strategy 和 policies；跨工作区会话引用 MUST 包含 workspace ID 与 session ID。定义、调度与运行协调 MUST 归属于明确工作区，Gateway 不得保存生成器业务状态。既有不支持的执行环境分离 MUST 继续明确拒绝。

#### Scenario: 从 Gateway 路由到另一工作区创建任务
- **WHEN** 用户在工作区 A 的界面明确选择在工作区 B 创建生成器，将其挂到 B 的会话并读取 B 的上下文
- **THEN** Gateway 将请求路由到 B，B 持有完整定位器及任务并负责调度，A 不保存另一份定义

#### Scenario: 拒绝尚未支持的执行环境分离
- **WHEN** 生成器要求会话存储、live session 上下文和 Agent 执行分别位于不同工作区
- **THEN** 系统返回明确 4xx 能力错误，不得把其它工作区 locator 当作目标工作区的本地 ID

### Requirement: 自定义命名路径安全可预览

生成器 SHALL 分别使用受限导航路径段模板与会话名称模板，MUST 拒绝路径穿越、绝对路径、非法分隔符和保留名称。所属工作区 SHALL 提供与创建使用相同规范化规则的预览且不创建会话。物理目录 MUST 沿用当前 catalog 与统一 locator 合同：叶名为稳定 session ID，显示名和导航父链不得参与物理路径推导；预览 MUST 区分导航位置与物理定位，不能恢复旧物理 children 目录模型。

#### Scenario: 预览日期命名路径
- **WHEN** 用户预览包含生成器名称、年月日、时分秒和会话标题的模板
- **THEN** 工作区返回规范化名称、导航路径及校验错误，未分配 session ID 时明确物理定位尚未生成，且不创建会话

#### Scenario: 在挂载节点下创建输出
- **WHEN** `new_per_run` 或 `fork_new_and_report_back` 生成器挂载到工作区、会话或会话文件夹并触发
- **THEN** 输出按预览的导航位置登记，由 catalog 分配并记录稳定物理 locator，导航挂载不会导致物理目录沿父链嵌套

### Requirement: 支持三种会话策略

生成器 SHALL 支持 `new_per_run`、`continue_existing` 和 `fork_new_and_report_back`，并 MUST 将 UI 激活策略与后端会话创建分离。这些策略 SHALL 作为工作区持久任务的动作运行，使用当前会话 catalog 的稳定定位与导航规则，不恢复已被替代的物理目录推导方式。

#### Scenario: 每次创建新会话
- **WHEN** `new_per_run` 生成器触发两次
- **THEN** 两次运行通过当前 catalog 创建不同稳定 ID 的会话，分别记录来源和权威 locator

#### Scenario: 继续指定会话
- **WHEN** `continue_existing` 生成器触发且目标会话已有活动 Job
- **THEN** 按显式 `queue` 策略串行排队，不得交叉写入消息；其它并发策略在实现前不得写入公开配置

#### Scenario: 新会话完成后旧会话继续
- **WHEN** `fork_new_and_report_back` 运行完成且回报模式为 `continue_agent`
- **THEN** 新会话保留完整过程，旧会话按队列收到结构化回报并继续新 Job，Web 保持原会话除非 ui policy 要求切换

### Requirement: 运行账本与幂等

每次触发 SHALL 产生工作区权威的任务运行，保留 GenerationRun 的稳定身份、幂等键、状态、错误和输出 locator；不得建立可独立修改的第二份生成运行账本。相同幂等键重试 MUST 返回同一运行而不得重复创建会话。工作区执行器 MUST 在分派前持久化稳定的输出 session ID、完整消息意图、message ID 与保留的 job ID，并 SHALL 在重启后从最后一个持久阶段继续。Gateway MUST 只代理工作区权威状态，不能依赖自身监控任务保存生成器终态。

#### Scenario: Gateway 重启后重试计划运行
- **WHEN** Gateway 在目标工作区已创建输出而客户端尚未收到完成状态时重启
- **THEN** 重试由工作区按原幂等键返回既有输出和同一任务运行，Gateway 重启不接管或重复任务调度

#### Scenario: 工作区在消息持久化后重启
- **WHEN** 工作区在生成消息已经持久化、但子 Job 尚未确认分派时重启
- **THEN** 执行器复用同一输出 session ID、message ID 与预留 job ID 分派，不创建第二条用户消息或第二个输出会话

#### Scenario: 无回报模式仍持久化终态
- **WHEN** `new_per_run` 或回报模式为 `none` 的生成 Job 完成或失败
- **THEN** 工作区持久化同一任务运行的 completed 或 failed 结果，界面通过 Gateway 读取该权威结果

### Requirement: 定义保存前验证稳定定位器

所属工作区 SHALL 在把生成器定义保存为 ready 前，通过权威 catalog 验证 placement、session strategy target 与 live session context locator 的存在性和末节点类型；同一 locator 的重复语义校验 SHOULD 共用一次目录探测。Gateway MUST 只路由，不直接读取工作区业务目录或维护第二套定位器验证事实。

#### Scenario: 会话定位器指向文件夹
- **WHEN** placement、策略目标或 live context 声明为会话，但稳定节点 ID 实际指向文件夹
- **THEN** 工作区返回包含 expected/actual kind 的明确 4xx，不得保存一个必然失败的 ready 定义

### Requirement: 挂载失效和离线可观察

挂载会话删除、工作区移除、工作区离线或目录丢失 SHALL 产生明确不可用原因及适用的 paused、blocked 或 failed 状态，MUST NOT 自动改绑或伪装成功。所属工作区后端离线时 Gateway SHALL 展示可用性状态，不得假称已修改离线工作区的任务事实；恢复后由工作区按持久进度与补跑策略对账。

#### Scenario: 挂载会话被删除
- **WHEN** 生成器挂载的会话不存在
- **THEN** 生成器暂停并提供重新挂载或删除入口，不自动提升到工作区根

#### Scenario: 工作区后端离线
- **WHEN** 定时任务所属工作区后端停止
- **THEN** 界面显示工作区不可用且任务不在 Gateway 继续执行；后端恢复后由工作区恢复调度，不把离线状态写成已完成

## ADDED Requirements

### Requirement: 定时会话动作组合必须保留计划与回报语义

迁入持久任务后，定时触发与会话动作的组合 MUST 保留 cron/interval、时区、skip/run_latest/catch_up 补跑、既有 overlap、命名/导航、UI 激活和回报配置的有效行为。新运行记录 SHALL 能从任务触发追踪到输出会话及回报执行；不得因通用判断串行化而擅自改变既有生成 Job 的重叠策略。无附加条件判断 MUST 对每个有效计划触发直接通过，不创建判断 thread、检查脚本或额外模型调用；会话动作自身需要的模型执行仍按原策略发生。

#### Scenario: 定时创建会话无需判断模型
- **WHEN** 已迁移的定时创建会话任务到达有效计划点且无附加条件
- **THEN** 系统沿统一链路直接通过判断并执行原会话动作，不额外启动模型来判断时间是否到达

#### Scenario: 定时任务重启后补跑
- **WHEN** 工作区停机错过多个计划点后重启
- **THEN** 系统按该任务原有补跑配置选择计划点，复用原计划身份并保留时区计算，不统一改为立即执行一次
