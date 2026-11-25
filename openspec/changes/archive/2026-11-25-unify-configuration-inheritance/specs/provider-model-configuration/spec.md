## RENAMED Requirements

- FROM: `### Requirement: 配置来源和模型默认必须按两种继承关系解析`
- TO: `### Requirement: 配置来源和模型选项必须使用统一单父继承`

## MODIFIED Requirements

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

Provider SHALL 支持协议与连接默认，Model 可通过所选节点明确覆盖现有协议支持选项。能力、reasoning 线格式与请求参数分别展示，枚举只开放真实项；未知能力不靠名称或其他模型猜测，冲突组合拒绝。只读 inspection SHALL 展示所选节点解析出的脱敏 endpoint、协议及目录地址，不为计算模型请求地址加载模型 SDK。显式模型测试 SHALL 展示由实际 SDK 规则解析的脱敏请求地址，标明其为 SDK 解析值；实际调用的最终发送地址由原调用观测负责，客户端不得自行拼接或把配置 endpoint 当作已观察到的请求地址。

协议切换 SHALL 在同一来源草稿中明确提供完整 supports_reasoning 块，不自动转换或改写能力。原子替换由统一 schema 合并语义承担，原 owner 进行业务一致性校验，不保留按来源阶段和 Provider 阶段各一份实现，也不增加通用厂商策略框架。

api_mode.streaming SHALL 是节点链上可继承布尔项，内置 Workspace 根明确声明 true 以保留原默认及可见来源。false 必须让上游真实非流式，不能收集流式伪装非流式。测试与正式请求共用解析，非流式的消息、reasoning、工具调用和 usage 语义保持，页面事件流不受影响。普通请求参数不得另提供覆盖该值的 stream，失败不自动切模式或协议。

正式 Job SHALL 将同一冻结传输选择经原 Saver profile／wire 参数、统一 finalizer、request_hash preimage 和实际 adapter 发送，完整正文的 stream／stream_options 一致。不得剔除实际正文字段、绕过 sealed binding 或放宽 source-mismatch；必要投影变化沿原 projector 升版，不重写历史封存。

#### Scenario: 只读查看与显式测试分别展示地址
- **WHEN** 用户只读查看 Model 的有效配置，随后显式运行模型测试
- **THEN** 只读查看从同一配置结果展示 endpoint 与协议，不为地址展示导入模型 SDK、不调用模型；既有 extra_body 等参数冲突校验保持。模型测试在原期限内使用 SDK 规则解析并脱敏请求地址，回执保留该值，真实 HTTP 验证覆盖解析地址与实际发送一致

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

### Requirement: 配置保存与执行生效必须保持原快照边界

模型节点、引用和代理变更 SHALL 沿原 source writer、CAS、active／pending 和后续 Job 生效边界。正在运行的 Job／请求保持原快照；保存、active 和后续执行分别报告。完整候选包含选定节点及其父依赖；当前引用检查仍只覆盖原 owner 的候选和当前工作区默认值，共享影响只报告已观察消费者，不扫描全部工作区、历史会话或 Trace 作为保存条件。

#### Scenario: 编辑默认值时已有模型流在运行
- **WHEN** 用户保存 Provider 选项节点或代理，旧 Job 仍输出
- **THEN** 旧 Job 保持原绑定，后续合法边界采用已 active 新值；需重启则显示 pending，不把文件保存当全部已生效

#### Scenario: 删除当前 Agent 使用的模型
- **WHEN** 候选删除被 primary、fallback 或当前工作区默认值引用的模型，或使其选项节点无法解析
- **THEN** 原 owner 返回具体引用冲突并拒绝，旧有效配置保持，不自动选择替代或全历史扫描

## ADDED Requirements

### Requirement: 共享选项编辑和复制必须保持对象隔离

普通对象表单 SHALL 默认只修改当前 Provider／Model：选中共享或只读祖先节点时，在可写来源的同一草稿创建子节点并修改当前 config_ref，一次原 CAS 提交。只有显式编辑共享定义才改原节点，并显示已知影响方，不扫描全部工作区。复制 SHALL 创建新稳定实体键、复制自有定义并重绑定内部引用；Provider 副本的 context:provider 引用指向副本，外部共享祖先保持明确引用，不克隆全图或复制秘密值。

#### Scenario: 两个模型选用同一 profile
- **WHEN** 用户在一个模型的普通详情改参数
- **THEN** 只为当前模型形成局部覆盖，另一个模型保持；明确编辑共享定义才影响两者

#### Scenario: 复制 Provider 后编辑其模型
- **WHEN** 复制含自有模型节点的 Provider，再修改副本默认或模型局部参数
- **THEN** 副本内部引用绑定新 Provider，原对象和其定义保持，外部共享祖先仍可识别
