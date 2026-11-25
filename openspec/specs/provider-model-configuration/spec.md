# provider-model-configuration Specification

## Purpose

让同一模型服务中的多个模型能够共享连接默认值并保存独立覆盖，以稳定身份贯穿配置、会话和实际请求。用户应能解释每个有效值的来源，修改少量参数而不复制整组配置，并获得符合交互总延迟预算的管理和执行体验。

## Requirements

### Requirement: Provider 和 Model 必须以稳定 ID 为对象键

Workspace 业务命名空间的模型目录 SHALL 使用 `llm.providers.<provider_id>.models.<model_id>` 对象结构；对象键为唯一不可变身份，不在值中重复维护 id。显示名、Model 稳定 ID 和服务端模型名称 SHALL 分离，同一服务端名称可有不同稳定 Model ID。Provider 和 Model SHALL 各用 config_ref 选择一个配置节点，不能同时保存 defaults／overrides 作为第二套选项。数组顺序、endpoint 或显示名 MUST NOT 推导身份。

#### Scenario: 两个服务包含相同名称的模型
- **WHEN** 两个 Provider 都有 general 模型且显示名相同
- **THEN** 使用完整 Provider＋Model 区分，修改一个不改写另一个，选择器显示必要归属信息

#### Scenario: 修改别名和复制模型
- **WHEN** 用户改显示名或复制模型
- **THEN** 重命名保持身份；副本取得不同稳定键和明确的选项节点，继承可共享但后续本地覆盖独立，不借用原对象的秘密占位

### Requirement: 配置来源和模型选项必须使用统一单父继承

系统 SHALL 以同一节点规则解释文件、Provider 和 Model。Provider／Model 目录的 config_ref 只选择节点，选中节点的 extends 才决定唯一父。默认 Provider 继承本次 Workspace 节点、Model 继承对应 Provider，允许用户明确改变合法父链；MUST NOT 先按固定文件层 merge，再隐式合并 defaults 和 overrides。内嵌节点本体不跨来源自动拼接；需要覆盖既有节点时，新节点明确继承原节点并选择新节点。

对象递归合并、标量／数组替换、原子 reasoning 线格式整块替换 SHALL 使用统一 schema 语义。最终选项 SHALL 提供有效值、完整父链、定义来源和原文位置；最终 model_options 按实际协议完整校验，本地片段可省略父已定义字段。消费者和客户端不能另算权威值。

#### Scenario: 工作区只修改一个模型参数
- **WHEN** 用户让本工作区该 Model 的 config_ref 指向一个继承原模型节点的新节点，且仅写一个请求参数
- **THEN** 该参数在当前工作区覆盖，其他字段、Provider、Model 与其他工作区不变，inspection 显示真实新父链和定义位置

#### Scenario: 高层服务默认和低层模型覆盖同时存在
- **WHEN** 工作区将 Provider 选择到新的默认节点，而某个 Model 的节点仍明确覆盖同一参数并通过 context:provider 继承该 Provider
- **THEN** Model 本地参数优先是实际父链结果，显示其原来源；不通过两次隐藏合并计算，不把 Provider 更改说成压过所有 Model 定义

#### Scenario: 来源覆盖片段不能独立满足完整模型 schema
- **WHEN** 子节点只写一个合法参数，父链提供其余连接和模型选项
- **THEN** 校验最终 model_options；最终缺必需信息才定位 Provider／Model／来源字段，不伪造默认值

#### Scenario: 模型覆盖服务的协议及线格式
- **WHEN** Model 从 Responses 的父节点继承，明确改用 Anthropic 并提供完整合法 thinking_blocks 块及对应能力声明
- **THEN** Model 只使用目标线格式，不残留 reasoning_items；未覆盖通用字段继承，其他模型不变

#### Scenario: 高层来源切换已有模型的协议
- **WHEN** 工作区新节点继承 user 的 Responses 模型节点，并改用 Anthropic、明确给出完整目标线格式
- **THEN** 原子块在唯一节点链上替换，非法协议字段仍拒绝，不删未知字段或跳过校验

### Requirement: 恢复继承必须保留显式覆盖意图

恢复继承 SHALL 只移除选定节点的本地字段，原子块移除整个本地块，结果由实际唯一父链决定；MUST NOT 复制有效值回节点。显式值即使等于父值也保留。supports_reasoning 的显式空对象 SHALL 作为完整块替换，仍校验能力一致性，不等于恢复继承；普通空对象不清空父对象。移除本地 Provider／Model 目录定义可能露出祖先的同身份定义，须明确说明，不加隐藏 tombstone 或改写其他来源。

#### Scenario: 默认值变化前保存相同的显式值
- **WHEN** 模型本节点参数等于父值，随后父值改变
- **THEN** 本地参数保持，只有删除该定义恢复继承后才采用父值

#### Scenario: 删除本层覆盖后露出下层模型
- **WHEN** workspace 移除 Model 的本地目录定义，祖先仍有相同稳定 Model ID
- **THEN** 模型继续存在并显示祖先定义及其 config_ref，不宣称已从所有来源删除

### Requirement: 请求参数必须具有一致优先级及明确省略语义

现有 Agent 与调用接口支持的显式参数 SHALL 作为最终 Model 节点之后的临时单父子节点，采用同一合并规则；常用链为 Provider、Model、Agent、本次调用。临时节点不持久化新配置，不能借此新增改模型身份、认证、网络、消息、工具或业务期限的权限。用户明确自定义的持久父链按实际链解释，不在链外再套 Provider 默认。

请求选项统一使用 request_options.parameters，不保留 request_options.overrides 的平行优先级。schema 允许省略的字段 SHALL 区分缺键继承、显式值和 null 不发送；false／0 为明确值。null 标记保留至所有子节点合并完毕才在 SDK 发送前移除，更高显式值可以覆盖省略选择；未知扩展和 extra_body 的合法 null 依协议保留。

未配置采样、reasoning 和输出限制 SHALL 采用上游默认，不能从模型容量推导 max_tokens／max_output_tokens。能力与输入容量不是请求参数。已有协议必需参数合同保持。Agent 别名在所属输入边界规范化后参与合并，同节点重复协议字段拒绝。extra_body 展开字段不得与正式参数或受保护字段重复，即使值相同也在原预检／调用边界拒绝；不靠 SDK 最后写入顺序，不增加全厂商参数注册表。

请求参数别名 SHALL 先转换为协议无关的内部逻辑键，最终合并后才按最终叶子协议映射，不按每层声明协议分别生成线上字段。同节点重复逻辑键必须拒绝，来源保留原文键。参数映射本身按名称合并，各参数的对象值（包括 extra_body）SHALL 在 schema 声明整体替换；所有持久与临时节点一致，不保留 Provider／Model 递归而 Agent／调用浅覆盖的两套算法。原持久配置对象参数拼接行为的变化 SHALL 在转换预检明确展示。


#### Scenario: 多层参数最终进入模型服务
- **WHEN** 默认父链中 Provider、Model、Agent 与现有调用接口对同一参数设值
- **THEN** 实际上游请求使用最终临时节点的明确值，所有现有协议一致，inspection 与实际发送相符

#### Scenario: 继承省略与显式零
- **WHEN** Model 把可省略参数设为 null，把允许零的另一参数设为0
- **THEN** 前者在无后续明确值时不发送，后者发送0；恢复继承和不发送为不同操作

#### Scenario: 最小配置不增加额外请求参数
- **WHEN** 用户只配置必需字段，模型另声明输入容量
- **THEN** 实际请求不因此带采样、reasoning 或输出限制，输入容量仅作为元数据

#### Scenario: 高级正文不能绕过 Agent 参数
- **WHEN** extra_body 声明正式入口已有的 temperature，Agent 又明确设置 temperature
- **THEN** 网络请求前定位冲突并拒绝，不按 SDK 展开顺序决定，也不静默删用户字段

#### Scenario: 省略标记在最终合并后处理
- **WHEN** 父有参数、Model 设 null，Agent／调用未覆盖，另一合法扩展含 JSON null
- **THEN** 被省略参数不发，扩展 null 保留；后续明确设置该参数才恢复发送


#### Scenario: 改协议同时覆盖输出上限别名
- **WHEN** 父 Responses 节点写 max_output_tokens，子节点改为 Chat 并写 max_tokens
- **THEN** 最终请求只有目标协议接受的一个输出上限，采用子值；同节点同时写两种别名拒绝

#### Scenario: 对象请求参数只写部分字段
- **WHEN** 父 extra_body 为两个字段，子任意层明确提供仅含一个字段的 extra_body
- **THEN** 有效 extra_body 仅保留子对象，不补父字段；子未定义 extra_body 时才继承，inspection 与线上正文一致

### Requirement: 模型能力与协议覆盖必须明确且可验证

Provider SHALL 支持协议与连接默认，Model 可通过所选节点明确覆盖现有协议支持选项。能力、reasoning 线格式与请求参数分别展示，枚举只开放真实项；未知能力不靠名称或其他模型猜测，冲突组合拒绝。系统 SHALL 展示按真实规则生成的脱敏目录／请求地址及协议，客户端不自行拼接不同地址。

协议切换 SHALL 在同一来源草稿中明确提供完整 supports_reasoning 块，不自动转换或改写能力。原子替换由统一 schema 合并语义承担，原 owner 进行业务一致性校验，不保留按来源阶段和 Provider 阶段各一份实现，也不增加通用厂商策略框架。

api_mode.streaming SHALL 是节点链上可继承布尔项，内置 Workspace 根明确声明 true 以保留原默认及可见来源。false 必须让上游真实非流式，不能收集流式伪装非流式。测试与正式请求共用解析，非流式的消息、reasoning、工具调用和 usage 语义保持，页面事件流不受影响。普通请求参数不得另提供覆盖该值的 stream，失败不自动切模式或协议。

正式 Job SHALL 将同一冻结传输选择经原 Saver profile／wire 参数、统一 finalizer、request_hash preimage 和实际 adapter 发送，完整正文的 stream／stream_options 一致。不得剔除实际正文字段、绕过 sealed binding 或放宽 source-mismatch；必要投影变化沿原 projector 升版，不重写历史封存。

#### Scenario: 同服务中的模型采用不同协议
- **WHEN** 一个 Model 继承服务协议，另一个节点明确选择不同合法协议及完整选项
- **THEN** 两者各发其明确协议请求，认证、参数和代理按相应链，不以厂商名替代协议

#### Scenario: 模型没有支持某个推理强度
- **WHEN** 用户查看菜单或提交不支持的枚举
- **THEN** 控件只展示真实项，非法提交在原边界定位并拒绝，不换成另一个值

#### Scenario: 同服务有仅支持非流式的模型
- **WHEN** Provider 默认流式，Model 节点明确 streaming=false 并用于正式会话
- **THEN** 现有各协议真实发送并封存非流式正文，reasoning、工具和 usage 沿原事件链呈现；其他模型保持流式，不靠测试豁免证明

#### Scenario: 高级参数与传输选择冲突
- **WHEN** 模型为非流式，高级参数另设 stream=true
- **THEN** 原校验拒绝并定位，不让测试与正式执行采用不同优先级

### Requirement: 共享选项编辑和复制必须保持对象隔离

普通对象表单 SHALL 默认只修改当前 Provider／Model：选中共享或只读祖先节点时，在可写来源的同一草稿创建子节点并修改当前 config_ref，一次原 CAS 提交。只有显式编辑共享定义才改原节点，并显示已知影响方，不扫描全部工作区。复制 SHALL 创建新稳定实体键、复制自有定义并重绑定内部引用；Provider 副本的 context:provider 引用指向副本，外部共享祖先保持明确引用，不克隆全图或复制秘密值。

#### Scenario: 两个模型选用同一 profile
- **WHEN** 用户在一个模型的普通详情改参数
- **THEN** 只为当前模型形成局部覆盖，另一个模型保持；明确编辑共享定义才影响两者

#### Scenario: 复制 Provider 后编辑其模型
- **WHEN** 复制含自有模型节点的 Provider，再修改副本默认或模型局部参数
- **THEN** 副本内部引用绑定新 Provider，原对象和其定义保持，外部共享祖先仍可识别

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

模型节点、引用和代理变更 SHALL 沿原 source writer、CAS、active／pending 和后续 Job 生效边界。正在运行的 Job／请求保持原快照；保存、active 和后续执行分别报告。完整候选包含选定节点及其父依赖；当前引用检查仍只覆盖原 owner 的候选和当前工作区默认值，共享影响只报告已观察消费者，不扫描全部工作区、历史会话或 Trace 作为保存条件。

#### Scenario: 编辑默认值时已有模型流在运行
- **WHEN** 用户保存 Provider 选项节点或代理，旧 Job 仍输出
- **THEN** 旧 Job 保持原绑定，后续合法边界采用已 active 新值；需重启则显示 pending，不把文件保存当全部已生效

#### Scenario: 删除当前 Agent 使用的模型
- **WHEN** 候选删除被 primary、fallback 或当前工作区默认值引用的模型，或使其选项节点无法解析
- **THEN** 原 owner 返回具体引用冲突并拒绝，旧有效配置保持，不自动选择替代或全历史扫描

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
