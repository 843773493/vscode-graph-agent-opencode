## Purpose

让同一模型服务中的多个模型能够共享连接默认值并保存独立覆盖，以稳定身份贯穿配置、会话和实际请求。用户应能解释每个有效值的来源，修改少量参数而不复制整组配置，并获得符合交互总延迟预算的管理和执行体验。

## ADDED Requirements

### Requirement: Provider 和 Model 必须以稳定 ID 为对象键

模型配置 SHALL 使用 `llm.providers.<provider_id>.models.<model_id>` 对象结构，对象键为唯一的不可变身份，不再在值中重复维护 `id`。显示名称、Model 稳定 ID 与实际服务端模型名称 SHALL 分离；显示名修改不改变引用，同一服务端模型可以对应不同稳定 Model ID。数组重排、显示顺序、endpoint 或名称 MUST NOT 被用来推断身份。

#### Scenario: 两个服务包含相同名称的模型
- **WHEN** 两个 Provider 都配置 Model ID 为 general 且显示名称相同的模型
- **THEN** 两者以 Provider＋Model 的组合准确区分，修改一个不会改写另一个，模型选择展示必要的所属服务信息

#### Scenario: 修改别名和复制模型
- **WHEN** 用户修改显示名称，或复制一个模型配置
- **THEN** 重命名保留原身份与引用；副本取得不同稳定 ID，后续配置与选择独立

### Requirement: 配置来源和模型默认必须按两种继承关系解析

系统 SHALL 先按现有 Workspace 配置来源优先级合并对象，再将选定 Provider 的 `defaults` 与 Model 的 `overrides` 合并为该模型选项。对象默认递归合并，标量及数组整体覆盖；`api_mode.supports_reasoning` 作为完整协议线格式声明，在两个合并阶段均以显式块整体替换，缺键才继承，不能混合不同协议的字段。局部来源允许仅提供一个字段，但修改线格式块须提供完整该块，合并后须满足实际协议要求。系统 SHALL 提供最终值、来源层和 Provider／Model 来源，不能由客户端另算权威有效配置。

#### Scenario: 工作区只修改一个模型参数
- **WHEN** 用户在 workspace 来源只覆盖一个 Provider 下一个 Model 的请求参数
- **THEN** 下层其他 Provider、其他 Model 及该模型未覆盖字段保持，诊断准确标识本层字段与继承字段，不要求复制整个模型列表

#### Scenario: 高层服务默认和低层模型覆盖同时存在
- **WHEN** workspace 修改 Provider 默认参数，而 user 来源已为其中一个 Model 显式覆盖同一参数
- **THEN** 先完成来源合并后，该 Model 的明确覆盖仍优先于 Provider 默认；界面展示这个覆盖的实际 user 来源，不错误宣称 workspace 默认压过全部模型覆盖

#### Scenario: 来源覆盖片段不能独立满足完整模型 schema
- **WHEN** 合法覆盖片段只写一个参数，而下层提供其连接与模型必需信息
- **THEN** 按合并后的完整模型校验；若最终确实缺少信息则定位具体 Provider／Model／字段，不伪造默认值

#### Scenario: 模型覆盖服务的协议及线格式
- **WHEN** Provider 默认使用 Responses 并声明 reasoning_items，一个 Model 明确改用 Anthropic 并提供完整 thinking_blocks 线格式块及合法能力声明
- **THEN** 该 Model 的有效线格式仅采用其完整块，不混入 reasoning_items；连接、streaming 等未覆盖通用字段继续继承，其他 Model 保持 Provider 默认

#### Scenario: 高层来源切换已有模型的协议
- **WHEN** user 来源已为 Model 声明 Responses 线格式，workspace 来源将该 Model 改用 Anthropic 并提供完整目标线格式块
- **THEN** 来源合并先整体替换该块，之后再完成模型继承；未知或错误协议字段仍须拒绝，不靠删掉未知字段或跳过校验通过

### Requirement: 恢复继承必须保留显式覆盖意图

恢复继承 SHALL 只移除选定来源的选定字段，原子线格式声明须移除整个本层块，结果由下层 Model 覆盖或 Provider 默认决定；MUST NOT 将有效值复制回该层。显式设值即使等于当前继承值也 SHALL 保留覆盖意图。`supports_reasoning` 的显式空对象 SHALL 作为完整空块替换下层，仍须与能力声明一致，不等于恢复继承；其他对象的空对象不清空下层。移除本层 Provider／Model 子树 SHALL 说明下层同身份定义可能重新可见，不引入隐藏删除标记或级联修改其他来源。

#### Scenario: 默认值变化前保存相同的显式值
- **WHEN** 模型显式参数与当前 Provider 默认相等，随后 Provider 默认改变
- **THEN** 模型仍保留显式参数；只有用户执行恢复继承后才跟随默认值变化

#### Scenario: 删除本层覆盖后露出下层模型
- **WHEN** 用户移除 workspace 中某个 Model 的全部本层覆盖，而 user 来源仍定义该模型
- **THEN** 模型继续可用且展示 user 来源，不显示为已从所有配置中删除

### Requirement: 请求参数必须具有一致优先级及明确省略语义

请求参数 SHALL 从低到高采用 Provider 默认、Model 覆盖、Agent 显式参数、本次调用已支持的显式参数。请求选项统一使用 `request_options.parameters`，MUST NOT 同时保留原 `request_options.overrides` 的另一套优先级。参数 schema 支持省略的字段 SHALL 区分缺键继承、显式值、null 不发送；零和 false 不能被当作未设置。更高优先级明确设值可以覆盖较低层的省略选择。

未配置采样、reasoning 或输出限制时 SHALL 使用模型服务默认，不自动填值或从上下文容量推导输出上限。容量与能力声明不得被误当作请求参数。协议确实必需的参数沿已有已验证合同处理。高级参数 SHALL 保留实际协议边界，不覆盖模型身份、认证、消息／工具及既有业务期限；自定义正文中的 null 依其协议解释，不递归删掉所有 null。

Agent 参数别名 SHALL 沿当前 adapter 映射后参加统一优先级合并，同一优先级重复声明同一协议字段必须拒绝。null 省略标记 SHALL 保留到所有优先级合并完成，再于 SDK 调用前移除，不能让已省略的低层值恢复。`extra_body` 展开字段 MUST NOT 与当前 adapter 的正式参数或受保护字段重复，即使值相同；冲突在原预检／调用选项边界定位并拒绝，不依赖 SDK 最后覆盖。不新增全厂商参数注册表，未知扩展中合法 null 保持原协议语义。

#### Scenario: 多层参数最终进入模型服务
- **WHEN** Provider、Model、Agent 与现有调用接口对同一支持的参数分别设值
- **THEN** 实际发送请求包含最高优先级的显式值，所有支持的协议遵守同一规则，界面有效值与发出的请求一致

#### Scenario: 继承省略与显式零
- **WHEN** Model 将一个可省略参数设为 null，或将另一个允许零的参数设为0
- **THEN** 前者在没有更高显式值时不发送，后者真实发送0；恢复继承和选择不发送是不同操作

#### Scenario: 最小配置不增加额外请求参数
- **WHEN** 用户只提供协议必需配置，模型还声明了输入容量
- **THEN** 实际请求不因此出现 temperature、top_p、reasoning 或输出上限，容量仍只作模型元数据

#### Scenario: 高级正文不能绕过 Agent 参数
- **WHEN** Provider 的 extra_body 声明 temperature，而当前 adapter 已提供该参数的正式入口，Agent 还显式设置 temperature
- **THEN** 网络前拒绝 extra_body 重复字段并定位修正位置，不因 SDK 展开顺序发送 Provider 的值，也不静默删除用户字段

#### Scenario: 省略标记在最终合并后处理
- **WHEN** Provider 有一个请求参数，Model 将它设为 null，Agent 与调用均未显式覆盖，另一个未知扩展含合法 JSON null
- **THEN** 上游不收到被省略参数，扩展中的 null 保留；Agent 后来明确设值时才恢复发送，不复活 Provider 低层值

### Requirement: 模型能力与协议覆盖必须明确且可验证

Provider SHALL 支持协议与连接默认，Model SHALL 能显式覆盖现有协议允许的选项。能力、reasoning 线格式与请求参数 SHALL 分别展示；控件和枚举只开放真实支持项。未知能力不得凭名称或同服务其他模型猜测，冲突协议／能力组合须明确拒绝。系统 SHALL 展示按实际请求规则生成的脱敏目录／调用地址与协议，避免用户误解是否需要 `/v1`；客户端不能另行拼出不同地址。

协议切换 SHALL 在同一来源草稿中明确给出目标协议的完整 `supports_reasoning` 块，不自动转换线格式或改写能力声明；继承结果与目标协议冲突时定位所需修正。该原子块规则 SHALL 由原配置 owner 在两种继承中复用，其他字段保持原合并合同，不新增通用策略注册框架。

`api_mode.streaming` SHALL 是可继承、可按模型覆盖的布尔传输选项，最终缺省为 true；false 必须使实际上游请求非流式，而非服务端收集流式后伪装为非流式。正式执行和测试 SHALL 使用同一解析规则，保持非流式结果的消息、reasoning、工具调用和 usage 语义；页面事件流不因上游非流式而关闭。普通请求参数 MUST NOT 再提供一套可覆盖此选择的 `stream` 设置，失败不自动切模式或换协议。

正式 Job SHALL 将同一冻结传输选择经原 Saver profile／wire 参数传入统一 finalizer、封存 request_hash preimage 及实际 adapter 发送，完整正文中的 stream／实际 stream_options 保持一致。MUST NOT 为接入非流式而剔除实际正文字段、绕过 sealed binding 或放宽 source-mismatch 校验；必要投影语义变更沿原 projector 更新版本，历史封存不重写。

#### Scenario: 同服务中的模型采用不同协议
- **WHEN** 一个 Model 继承服务协议，另一个明确覆盖为另一已支持协议并给出合法模型选项
- **THEN** 两者分别向其明确目标发送对应协议请求，认证与参数保持各自解析结果，不以 Provider 厂商名称取代协议选择

#### Scenario: 模型没有支持某个推理强度
- **WHEN** 用户查看模型的快捷配置或提交不支持的枚举值
- **THEN** 菜单只显示真实允许项及说明；非法提交在原预检／执行校验边界明确拒绝，不静默换成其他值

#### Scenario: 同服务有仅支持非流式的模型
- **WHEN** Provider 默认流式，而一个 Model 明确覆盖 `api_mode.streaming=false` 并用于正式会话
- **THEN** 每种现有协议的正式 Job 均以真实封存准入发送非流式请求，sealed preimage 与实际正文一致，完整内容、reasoning、工具调用和 usage 沿原会话事件链呈现；其他模型继续继承流式，不借测试专用豁免或未接入的 Agent 开关假报支持

#### Scenario: 高级参数与传输选择冲突
- **WHEN** 用户将模型设为非流式，却在高级参数中另写 stream=true
- **THEN** 原校验边界明确拒绝冲突并定位字段，不让测试与正式请求各取不同优先级

### Requirement: 业务选择必须使用结构化模型引用

Agent primary／fallback、SessionDefaults、会话当前选择、任务与子 Agent 显式选择、模型工具测试和执行准入 SHALL 使用同一 `{ provider_id, model_id }` 引用。全局或跨目标操作还 SHALL 携带原 Gateway／Workspace 归属，不按名字猜路由。fallback 保持用户已声明的顺序；MUST NOT 新增“选 Provider 第一个模型”或另一个隐式默认模型来源。

Settings SHALL 能管理工作区完整模型目录，Composer 及 Session／执行选择 SHALL 保留原当前 Agent 候选限制，以其 primary／fallback 完整 ModelReference 校验。相同 Provider 的其他 Model、发现结果或收藏 MUST NOT 扩大候选。显式选择候选中的 fallback 只沿原执行规则将其置首，其余候选保持相对顺序，不改写 Agent 定义；切换 Agent 仍沿原 Session owner 解析新 Agent 选择。配置变化使旧会话绑定不再允许时，保留可辨认绑定及原因，后续执行要求明确选择，不静默改绑。

#### Scenario: 两个 Thread 选择同服务不同模型
- **WHEN** 同屏 Thread A 和 B 在各自 Session owner 下选择同一 Provider 的不同 Model
- **THEN** 之后的执行、会话恢复及选择器分别保留完整引用，A 的选择不覆盖 B 或工作区默认值

#### Scenario: 主模型与 fallback 共享服务
- **WHEN** primary 和 fallback 使用同一 Provider 下的不同 Model
- **THEN** 执行保留两者的 model_id、参数及原 fallback 顺序，不按相同 provider_id 将它们去重为一个模型

#### Scenario: 同服务未列入 Agent 的模型不能作为会话候选
- **WHEN** 工作区已配置并收藏某 Model，但当前 Agent 的 primary／fallback 未包含它，且允许集合中有同 Provider 的另一 Model
- **THEN** Settings 仍可管理该模型，Composer 的普通结果与收藏不提供该执行选择；直接提交也在 Session／执行准入拒绝，不能仅核 Provider ID

#### Scenario: 当前定义被删除后访问历史会话
- **WHEN** 一个历史会话引用已不存在的 Model
- **THEN** 原历史证据保持，后续需要执行时明确显示模型不可用并要求选择，不自动改绑或扫描重写历史

### Requirement: 私有 reasoning 回放必须采用完整模型来源

`same_source` SHALL 在原封存 profile／响应 metadata 和历史投影中统一使用实际 ModelReference、原 Gateway／Workspace、协议、规范连接地址、服务端模型名及认证来源身份，不仅比较 Provider ID。认证来源仅记录 secret ID、环境变量名、原 OAuth owner 可确认的身份或协议明确无认证的 none，不记录秘密或秘密摘要，不新增来源注册表。显示名变化不改变来源；密钥 revision 继续用于 runtime 失效，不能替代稳定模型身份。

同 Provider 的不同模型或不同连接／协议／认证来源 SHALL 按原跨源投影处理不可重放私有块，保留可见文本、摘要、工具配对与原因。历史缺少足够来源时不能按旧 Provider ID 或名字补猜；不扫描、重写或补造历史来源。模型切换、fallback 和会话恢复 SHALL 共用这一边界，新来源与实际执行快照一致。

#### Scenario: 同服务不同模型和连接不能共用私有密文
- **WHEN** A 产生私有 reasoning 后，后续显式选择或 fallback 到同 Provider 的 B，B 使用不同 Model ID 或连接／认证来源
- **THEN** 不因 provider_id 相等向 B 回放 A 的私有密文；原跨源投影保留可见内容和工具配对，当前请求记录完整 B 来源

#### Scenario: 恢复缺少模型来源的历史
- **WHEN** 历史 reasoning 只记录旧 Provider ID，无法确认完整模型来源
- **THEN** 不据此放行私有密文回放，保留原证据及跨源处理原因，不扫描改写历史或自动改绑模型

### Requirement: 配置保存与执行生效必须保持原快照边界

模型选项变更 SHALL 沿原配置来源、CAS、active／pending 与后续 Job 生效规则。已运行的 Job 和在途请求保持原快照；保存、active 和下一次执行使用分别报告。当前引用检查 SHALL 限于原 owner 的完整候选和当前工作区默认值，共享影响只报告已观察消费者，不以扫描所有工作区／会话／Trace 作为保存条件。

#### Scenario: 编辑默认值时已有模型流在运行
- **WHEN** 用户保存 Provider 默认参数，当前 Thread 的旧 Job 仍在流式输出
- **THEN** 旧 Job 保持，配置已 active 后后续 Job 使用新值；若候选含需重启字段则如实保持 pending，不将文件保存当作全部生效

#### Scenario: 删除当前 Agent 使用的模型
- **WHEN** 候选删除仍被 primary、fallback 或当前工作区默认值引用的模型
- **THEN** 原 owner 拒绝该候选并返回具体引用，旧有效配置保持，不自动选择替代项或全历史扫描

### Requirement: 模型管理读取必须满足完整交互预算

普通定义、来源和列表读取 SHALL 不触发模型调用、Provider 目录发现、全部密钥解密或后台审批轮询。可复用的解析及来源事实按原配置 snapshot revision 失效，不在每次暖读重做全量审计。未取得外部状态 SHALL 显示未验证，不能假称连接可用。

在本机回环且服务就绪、无外部等待时，不超过512KiB的完整暖 API 响应 SHALL 满足 p95≤500ms、单次≤1500ms；首次未缓存业务读取≤3000ms；进入正确模型设置可实际编辑及打开正确会话 Composer 每次≤2000ms。计时包括排队、内部调用、校验和序列化；加载占位、平均值、进程 timeout 不能替代完成终点。

#### Scenario: 大量已配置模型和六个编辑器并存
- **WHEN** 三层来源中有20个 Provider、各10个 Model，六个 Thread 编辑器已打开，用户重复读取列表并进入模型设置
- **THEN** 至少20个暖 API 样本的完整结果和每次真实可操作 UI 达到预算，原始样本及 request_id 保留，未发生全模型探测或六套凭据解密轮询

### Requirement: 模型发现必须有界且保持用户配置

系统 SHALL 支持用当前 Provider 草稿或 active 连接读取上游模型目录，不以保存来源、创建会话、已有模型列表或其他业务配置完整预检为前置。发现 SHALL 使用选定 Workspace 的实际网络与凭据来源，明确归属、输入版本、目录地址、观察时刻和是否完整。公共参考元数据、当前上游目录与推理测试是不同事实，目录返回成功不能证明聊天、流式、工具或视觉可用；缺少的能力保持未知。

目录发现 SHALL 在累计15秒、2MiB正文和5000个模型的边界内处理；上游支持的分页共用总预算，不能猜路径或无界读完。已取得部分结果时 SHALL 报告不完整原因，不假称完整列表。刷新只更新发现结果，用户选择后才进入原来源草稿；已有稳定键、显示名、能力、覆盖及同服务端模型的多个明确配置 MUST NOT 自动覆盖、合并或删除。

#### Scenario: 尚未保存的连接发现模型
- **WHEN** 用户新填连接信息且尚未添加 Model，当前来源还有不影响连接的其他未完成编辑
- **THEN** 仅验证发现所需字段并查询当前上游，返回候选模型，不保存来源、创建 candidate 或要求先把全部业务设置配置好

#### Scenario: 目录未提供能力或不支持目录接口
- **WHEN** 上游仅返回模型名称，或目录接口返回404而调用接口可能可用
- **THEN** 未提供能力保持未知；404显示为目录不可用，保留已有模型并允许手填，不能将全部模型标为调用失败

#### Scenario: 刷新发现差异与不完整结果
- **WHEN** 刷新返回新模型、未包含某些原模型，或分页／上限使结果未取全
- **THEN** 完整结果可展示新增与本次未返回项；不完整结果明确标记，任何情况下均不自动移除、改 ID 或覆盖已有模型，用户选入时才编辑原草稿

### Requirement: 快速测试必须区分草稿与已生效配置

快速测试 SHALL 支持 `active` 或 `draft` 两种明确输入；active 使用唯一 ModelReference 及当前已生效版本，draft 在原来源上下文解析当前非秘密草稿及短暂凭据。请求与回执 SHALL 绑定原目标／工作区、模型、协议、测试模式和输入版本，不因焦点改变而改投，不能混用 active 参数与草稿 Key 却仍标为 active。

active 请求 SHALL 携页面所见 active revision／digest；draft SHALL 携原 source document 的 `expected_source_baseline` 及本页编辑版本。draft 采用当前已保存来源（可含 pending），只以本次草稿替换所选来源，其余来源不混入 active 旧值或其他未保存草稿。原 owner SHALL 在一次有界读取中核对本次参与来源的既有版本／摘要；不一致在上游调用前明确报配置冲突并要求重读，零自动重基、零自动重发，不新增 candidate 或版本事实源。

发现回执及测试 start／result SHALL 带实际采用的来源基线或 active 版本、实际使用的凭据非秘密身份／revision、本页编辑版本与观察时刻；调用中使用同一冻结解析，不反复读配置或 vault。临时 Key 改变 SHALL 推进本页编辑版本，不记录秘密摘要。客户端 SHALL 区分请求编辑版本与实际观察版本，已知来源／凭据变化时保留原观察并标为过期；通过只证明所示版本当时的一次请求。原通知、写入回执或显式刷新得到的新版本用于过期判断，不新增后台轮询；不可观察的外部变化不得假称已实时检测，无凭据 revision 时只报告来源与观察时刻。

测试 SHALL 复用正式配置解析、凭据来源与协议请求规则，仅校验所选模型所需信息，不保存配置、创建正式 candidate、运行完整 Agent、使用 fallback 或自动重试。默认一次简短文本调用；用户可分别选择流式、非流式，或明确执行两次。模型参数按配置使用，不擅自增设输出限制、采样或关闭 reasoning。测试通过 MUST NOT 自动改变能力、传输或 active 状态；相关输入改变后，旧结果必须标为过期。

#### Scenario: 未保存草稿测试而不影响执行
- **WHEN** 用户修改模型地址或参数并测试当前草稿，而旧 active 配置仍有运行中的 Job
- **THEN** 测试使用当前草稿的真实有效选项，回执标明草稿版本；没有配置写入或生效动作，运行中 Job 与之后未经保存的 active 行为保持

#### Scenario: pending 与 active 分开测试
- **WHEN** 来源已保存为 pending，用户明确测试 active
- **THEN** 仅使用当前实际生效版本并标明与来源的差异，不暗用 pending 或把测试通过当成配置已应用

#### Scenario: 本页未修改但继承来源已变化
- **WHEN** 本页草稿编辑版本不变，而另一窗口已将参与继承的来源保存为 pending 或 active，页面仍提交旧来源基线
- **THEN** 网络调用前拒绝旧基线并定位重读入口，不用新继承值测试却仍显示为原草稿版本通过；重读后用户可明确重新发起，active 测试不混入 pending

#### Scenario: 测试期间密钥轮换与显式刷新
- **WHEN** 测试已冻结密钥 revision，期间收到相关轮换回执或随后显式重读发现其 revision 已变化
- **THEN** 本次结果仍注明原观察 revision 并标为过期，不覆盖为新密钥已通过，不重复解析或重发；没有变更通知时仅展示带版本和时刻的历史观察，刷新零推理请求

#### Scenario: 模型测试失败不能借替代项通过
- **WHEN** 当前模型的流式测试失败，而非流式或 fallback 模型可能可用
- **THEN** 报告原模式的本次结果；只有用户另外选择测试或编辑配置才采用其他模式，不能隐式换协议、模型或重复请求

### Requirement: 测试结果必须证明真实传输并可以取消

非流式测试 SHALL 验证可解析的有效完整模型结果；流式测试 SHALL 按所选协议验证有效事件／内容及正常结束，HTTP 200、心跳或仅响应头不能算通过，一个有效内容片段也不能仅因片段数量少而判失败。模型内容 SHALL 经真实 Gateway 增量到达当前页面，不等上游结束后才返回摘要；取消、截断、错误事件及中途断流不得标为通过。

结果 SHALL 分别显示模型／协议／模式、未测试／进行中／通过／本次失败／取消、首个有效内容与完整耗时、真实 usage 或未知、脱敏错误及原 request_id。临时失败不能永久写成不支持。快速测试默认总期限60秒，允许在现有执行期限内显式调整；不自动重试。展示片段最多64KiB，截断预览须标记且不改变结束判断。软件内部处理沿普通暖请求预算，外部等待单列；前端收到有效内容后每次≤250ms可见。

用户 SHALL 能取消单次操作或停止选中批量；批量仅限明确选中的非空模型范围，最多3个在途调用。停止后不发剩余请求，并关闭在途等待与连接；取消不是成功或不支持，也不承诺上游已停止计费。临时测试不创建持久队列或测试历史库，不改变既有工具准确率测试的生命周期。

#### Scenario: 流式 HTTP 200 后返回错误或断流
- **WHEN** 上游已返回200但随后仅返回错误、心跳，或内容输出中途异常关闭
- **THEN** 显示真实失败及已取得的有限片段，不因握手成功或已有部分正文判通过，不自动重试

#### Scenario: 页面先于上游完成显示内容
- **WHEN** 可控上游先发有效内容并等待信号才结束
- **THEN** 浏览器经过真实 Gateway 在上游结束前显示第一段；正常结束后才显示通过，收到内容至可见满足预算

#### Scenario: 用户取消或停止批量
- **WHEN** 用户取消当前慢请求、关闭测试面板、切换管理目标，或停止尚有未派发模型的批量测试
- **THEN** 本页临时请求停止且迟到结果不回写新页面；批量未派发项零请求，保留已知结果并标取消／未运行，不把此规则套到后台工具准确率任务

### Requirement: 临时操作必须具有明确的 HTTP 与流式终态合同

`POST /api/v1/config/llm/discover` SHALL 返回原 APIResponse JSON，客户端仅为本入口使用20秒完整请求期限，覆盖15秒发现预算和5秒传输收尾余量，不修改普通请求默认值。`POST /api/v1/config/llm/test` SHALL 始终通过现有带凭据的原始 Response POST fetch 返回 text/event-stream，包括上游非流式测试；不使用 EventSource、JSON 最终摘要或恢复型订阅器。事件 DTO／Protobuf／生成客户端 SHALL 随接口同步定义，响应头和各事件沿用同一个 request_id。

正常流 SHALL 依次提供一次 start、零到多个 delta 和一次终态 result。start 携冻结归属、实际来源／active 与凭据观察版本、ModelReference、协议和模式，在等待上游首内容前发出；delta 只追加有界展示；result 沿用同一观察版本，包含 passed／failed／cancelled、真实耗时、usage 或未知、脱敏错误和 preview_truncated，只有有效协议正常结束可通过。发头前确定的拒绝用原非2xx错误，发头后错误／超时用 failed result；SSE 注释心跳不算业务内容。非主动取消的缺 result EOF SHALL 判失败，MUST NOT 自动重连、恢复或重放推理 POST。

当前 ChatGPT OAuth 的原 SDK 只支持流式 Responses 请求；配置非流式时 SHALL 在读取 OAuth 凭据及请求上游前明确拒绝，MUST NOT 以聚合流冒充真实非流式支持。其它支持完整响应的协议继续按所选模式发出真实请求。

原共用 HTTP 入口在明确 invalid local token 且尚未业务准入时的既有一次凭据刷新 SHALL 保持，实际模型调用不得因此增加；网络错误、结果未知或流中断不属于这一情形，不得绕开原入口或修改全局重试策略。

Backend SHALL 从接收操作起使用单一绝对总期限，默认60秒；客户端覆盖取头及正文消费的总期限仅多5秒收尾余量，不受普通 JSON 15秒限制，也不被心跳延长。Gateway SHALL 及时透传 SSE 头、start 和后续增量。客户端取消 SHALL 沿原 fetch／reader、各 Gateway 上游响应和 Backend 调用传播，首内容前等待及非流式调用均需关闭并结清资源；不能仅隐藏 UI。断连后无法送达取消 result 时只报告本地取消事实，不宣称撤销计费。不新增 cancel API、测试 Job、持久游标或恢复队列。

#### Scenario: 首内容慢于普通 JSON 期限
- **WHEN** 可控上游等待超过15秒后正常返回，仍在本次60秒期限内
- **THEN** 浏览器先收到 start 和必要心跳，之后显示真实内容及一次通过 result，不被普通 JSON 期限提前终止；等待计入外部服务时间

#### Scenario: 首内容之前取消与无终态断流
- **WHEN** 用户在首内容前取消请求，或未取消但 SSE 在 result 前关闭
- **THEN** 前者沿真实 Gateway 释放上游连接并显示本地取消，后者显示失败；两者均零自动重发，不因已收到 start 或部分 delta 显示通过
