## Context

动机和范围见 [proposal.md](proposal.md)。当前 `llm.providers` 是数组，每项同时包含连接、`model`、凭据和请求选项，Agent 的 primary／fallback 与 SessionDefaults、会话当前选择也只记录 Provider ID。`request_options.overrides` 在运行时压过 Agent 参数，不能只改 UI 为“默认值”而保留相反执行语义。

已有 Gateway 密钥库使用稳定 `sec_…` ID、加密正文、私有文件权限、revision CAS 和内部批量 resolve；配置来源有正式文本模型、JSONC、schema、来源预检、文件／层 CAS 与 active／pending。复用这些 owner，不再增加保存队列、密钥库或配置镜像。

参考源码固定为 `reference_repo/vscode` 的 `eb50e1bf17fc87a192079dda6ab5d2b6523ee68c`，更新时通过本机代理 10809 对已有浅克隆的 main 做近期增量更新。产品 `src/clients/workbench/upstream` 仍为原固定提交，设计不要求升级它。

| 上游位置 | 已有行为 | 本项目采用方式 |
| --- | --- | --- |
| `extensions/copilot/src/extension/byok/vscode-node/customEndpointProvider.ts` | 组级协议与模型协议覆盖、共享 API Key、逐模型请求选项 | 扩展为统一的 Provider 默认与 Model 覆盖；不误称上游已为全部字段实现继承 |
| `extensions/copilot/src/extension/byok/node/openAIEndpoint.ts` | 显式请求参数优先，部分采样参数支持 null 表示不发送 | 明确参数优先级和三态，不照搬其默认容量／输出预算 |
| `src/vs/workbench/contrib/chat/common/languageModels.ts` | schema 驱动枚举菜单、准确模型归组、恢复默认、配置变更事件 | 复用原菜单和控件；业务配置仍写本项目唯一 owner，不并行写语言模型配置文件 |
| `src/vs/workbench/contrib/chat/browser/chatManagement/chatModelsWidget.ts` | Provider 分组、筛选、模型详情、次要操作菜单 | 沿用视觉与交互，纳入当前 Settings 入口及实际 Gateway／Workspace 归属 |

体验调研还核对 OpenCode `df46f6931da0c934a59d375ac44525846f21c718` 的 `packages/app/src/settings/models/`、`providers/models/` 和 `packages/core/src/plugin/provider/local.ts`，以及 New API `ce8fc49e4b1a874aa917dce4f1a8416c52ec8ec0` 的 `web/src/features/channels/`、`playground/` 和 `controller/channel-test.go`。采用目录筛选、草稿发现、逐模型协议／流式测试、参数按需发送和模型偏好。OpenCode 的本地发现有周期刷新，New API 的渠道流式测试只返回最终 JSON 摘要；本项目不照搬前者的常驻轮询，并补足后者未证明的 Gateway 至页面增量展示。

## Goals / Non-Goals

**Goals:**

- 同一服务下多个模型共享连接与凭据，只为差异写覆盖；工作区只改一个字段时不复制整组定义。
- 一套配置解析、模型身份和保存合同贯穿 Settings、Composer、Agent、任务及测试入口，六个 Thread 同屏也不串选中模型或参数。
- 模型密钥只由所属 Gateway 持久化，多个 Provider／Model 显式复用同一引用；配置可查看不以全部凭据可解密为前置。
- 参数与来源可解释，普通管理读取保持原总延迟预算。
- 连接表单可自动发现模型并测试未保存草稿，不为验证连接先写配置或创建会话；测试范围与使用版本清楚，慢上游不阻塞普通设置。

**Non-Goals:**

- 不把 `llm` 配置域搬到 Gateway，不让 Gateway 读取工作区文件。Gateway 持有密钥不等于持有模型业务配置。
- 不新增 SQLCipher、通用加密数据库、系统钥匙串、跨 Gateway 密钥同步或插件秘密迁移；已有 OAuth 继续由原认证 owner 管理，不强改成 API Key。
- 不新增自动模型路由、隐式 fallback、后台连通性巡检、审批中心或全历史引用索引。已有显式 fallback 顺序保留。
- 不为未来 Thread 参数编辑、未实现的模型能力或插件模型 Provider API 预写接口与假控件。
- 不新增渠道权重／计费／自动禁用、常驻模型巡检、全功能 Playground、独立 Variants 身份或自动全能力矩阵。参数预设用现有复制 Model 配置表达；工具准确率沿原入口，不为本次快速测试重建它。

## Decisions

### D1. Provider 与 Model 均以稳定 ID 为对象键

采用 `llm.providers.<provider_id>.models.<model_id>`。键就是身份，不再在值里重复一个可冲突的 `id`。沿用现有 Provider ID 的合法字符约束；Model ID 在所属 Provider 内唯一。可选 `name` 是显示名，未填时显示稳定键；Model 的 `model` 是发给服务端的模型名称，与稳定 ID、显示名分别管理。修改显示名不影响引用；创建后无原地改 ID 操作。相同服务端模型可以有两个不同 Model ID，以保存不同明确配置。

示例只说明结构，密钥 ID 为占位引用，不表示内置配置应添加这些服务或能力：

```jsonc
{
  "llm": {
    "providers": {
      "team-api": {
        "name": "团队模型服务",
        "defaults": {
          "endpoint": "https://models.example.com/v1",
          "custom_llm_provider": "openai",
          "api_key": "secret:sec_00000000000000000000000000000000",
          "api_mode": { "protocol": "chat_completions" }
        },
        "models": {
          "general": {
            "name": "通用模型",
            "model": "service-model-name",
            "overrides": {
              "api_mode": {
                "model_info": { "supports_function_calling": true },
                "supports_reasoning": { "reasoning_content": false }
              }
            }
          }
        }
      }
    }
  }
}
```

Provider `defaults` 与 Model `overrides` 复用一套可执行模型选项定义：endpoint、现有适配族／协议／认证配置、请求选项；不接受其中再次定义 `model`、Provider／Model ID 或 `models`。Model 的能力和 reasoning 线格式仍沿现有模型合同，通常填写在具体模型；用户明确设置的 Provider 默认能力才参与继承，不按厂商或模型名称猜测。

传输选项使用 `api_mode.streaming: boolean`，Provider 可给默认，Model 可覆盖，最终未声明时为 true 以保持现有流式行为；false 必须实际发送非流式请求。它不是能力声明，也不是可被 `request_options.parameters`／`extra_body` 另行改写的普通参数。不借用当前未接入的 Agent `execution.stream`。所有现有协议 adapter、正式模型执行和工具测试必须消费同一最终选项；非流式结果沿原消息／reasoning／工具调用／usage 规范化链一次发布，页面的 SSE 事件传输不因此关闭。不能强制上游流式再在服务端聚合成“非流式测试通过”，也不在失败时自动切模式。

该选项同时进入原 Saver 的纯值 profile／wire 参数、统一 wire finalizer、封存 request_hash preimage 和实际发送链；`stream` 及协议实际使用的 `stream_options` 必须反映同一次冻结的传输选择。不能只改 adapter 或快速测试，也不能从 hash 中剔除这些实际正文字段、跳过 sealed binding 或放宽 source-mismatch 校验。投影语义变化时沿原 projector 更新版本，既有封存正文保持不变。

不采用“数组按 ID 特殊合并”或“旧扁平 Provider＋模型数组”并行结构。对象键直接复用正式配置递归合并，减少索引漂移和额外规则。

### D2. 分清配置来源继承与 Provider／Model 继承

执行顺序：

1. 在原 Workspace 配置 owner 按 inline → user → user_local → workspace → 已支持 runtime override 合并来源；不增加 Gateway 到 Workspace 的隐式配置继承。
2. 对选定的 ModelReference，从已合并 Provider `defaults` 和该 Model `overrides` 得到模型选项；将该 Model 的 `model` 作为服务端模型名。
3. 对请求参数继续应用 D3 的显式调用优先级，之后由现有协议 adapter 构造请求。

对象默认递归合并；标量与数组由高优先级整体替换，已有 fallback 数组同样如此。唯一新增的模型选项例外是 `api_mode.supports_reasoning`：它是按协议解释的完整线格式声明，显式提供时整体替换，包括显式空对象；缺键才继承。同协议只改其中一个标记时也提交完整该块，避免把 Responses 的 `reasoning_items` 与 Anthropic 的 `thinking_blocks` 拼成一个对象。这一规则同时适用于各来源中的 Provider defaults／Model overrides 和随后两者的合并，由原配置 owner 复用，不扩展成通用合并策略框架。

切换协议时由用户在同一草稿明确给出目标协议的完整 `supports_reasoning` 块；UI 说明这一替换边界，不自动转换旧线格式或猜能力。缺省继承后若与目标协议冲突，明确定位并要求修正；`model_info`、streaming、连接等其余字段仍按原规则继承，能力与线格式的一致性校验保留。来源覆盖片段无须包含完整实体，但合并后的每个模型必须满足实际协议的完整要求。未知键、非法能力组合、无效引用在原 schema／预检边界明确拒绝。

“恢复继承”只删除选定来源的选定字段；原子线格式块恢复继承时删除整个本层块。用户显式设值即使与当前继承值相同也保留，否则默认值变化会意外改变其明确覆盖；不照搬上游按“等于默认值”自动删除键的全部行为。移除本层 Model／Provider 子树可能露出下层同 ID 定义，UI 须事先解释；不增加 tombstone。继承定义要彻底移除时定位真正定义它的可写来源，不能在高层伪造删除。除上述原子块外，空对象不是清空低层对象的指令。

有效值与两类来源由原配置 owner 在现有 inspect／source document 上提供：来源文件／层，以及 Provider 默认或 Model 覆盖；前端不再实现一份权威解析器。预检及新 active revision 时计算，普通读取复用原 snapshot，来源或有效 revision 变化后失效；不另建持久缓存或每次遍历全部实体重做审计。

### D3. 请求参数使用一套明确的优先级和省略规则

将原 `request_options.overrides` 统一替换为 `request_options.parameters`，位于 Provider `defaults` 或 Model `overrides` 内；不在运行时接受两个同义字段。常用参数按当前协议展示，服务扩展继续使用现有 adapter 支持的 `extra_body`，不为全部厂商建庞大参数注册框架。

优先级从低到高：Provider 请求默认 → Model 请求覆盖 → Agent 已明确配置的参数 → 本次调用已支持的显式参数。最后一项只指现有调用能力，不凭此新增 Thread／Job 持久参数 owner。未设置采样／reasoning／输出参数时不向请求填值；协议确实必需的参数按已验证的 adapter 合同处理，不由 UI 根据上下文容量推导。

请求参数 schema 明确支持省略的字段区分三态：缺键＝继承；值＝显式覆盖；null＝本层要求不发送，除非更高优先级再明确设值。零和 false 是真实设值。`extra_body` 内的 null 按对应协议的 JSON 语义保留，不做全对象递归删除；非请求参数的 null 不适用该省略规则。

Agent 的参数别名先沿现有 adapter 映射为所选协议的请求字段，再参加上述优先级合并；同一优先级用不同别名重复声明同一字段时拒绝。省略标记保留到所有优先级合并结束，只在交给 SDK 前移除对应请求字段，不能提前删除 null 而让低层值重新出现。

`extra_body` 仅承载当前 adapter 未提供正式参数入口的上游扩展。其将展开到请求正文的字段若与该 adapter 已有正式参数或受保护字段同名，必须在原预检／调用选项边界拒绝，即使值相同；不由 SDK 的最后一次展开决定胜负。例如 `extra_body.temperature=1` 不能与 Agent 的 `temperature=0` 并存，应定位扩展字段并要求改到 `parameters.temperature`。判断使用现有 adapter 的字段与映射，不新增全厂商参数注册中心；扩展字段内部合法 null 原样保留。

模型身份、认证、消息／工具、传输与既有业务期限不能借高级参数改写；`stream`／`stream_options` 等传输字段由 adapter 根据 `api_mode.streaming` 构造，冲突输入在原选项解析边界拒绝。请求头按 HTTP 名称大小写不敏感覆盖，同一层大小写重复项明确报错，不生成两份 Authorization。认证继续由现有协议与凭据字段生成；普通请求头不是密钥字面量的旁路。

这是明确的执行语义变更，须用真实 adapter 收到的请求证明；不能仅重命名字段或以单个合并 helper 的单测代替验收。

### D4. 所有选择使用同一 ModelReference

配置与业务协议中的模型引用为 `{ provider_id, model_id }`，不拼字符串、不自动挑 Provider 的第一个模型。工作区内部引用由既有 Workspace owner 限定；跨目标 UI 额外沿原管理合同携带 `gateway_id`、`workspace_id` 和连接路由，不能只按同名 Provider 定位。

| 消费者 | 目标合同 |
| --- | --- |
| Agent 模型定义 | `model.primary` 为一个 ModelReference，`model.fallbacks` 为有序 ModelReference 数组；替换 `primary_provider`／`fallback_providers` |
| SessionDefaults 与会话当前选择 | 原 owner 中 Provider 单值字段替换为结构化模型引用；响应中的当前选择使用 `current_model`，选择请求使用 `model` |
| Job／子 Agent／持续任务及模型 judge | 在原执行准入与快照中携带同一引用；显式模型选择不能丢失 model_id |
| 模型工具测试 | 新 UI 提交明确的非空 `model_refs` 范围；保留原重复次数、run_id 及后端空范围表示全部的合同，不用空数组表示当前模型 |
| 模型详情、诊断和执行记录 | 同时标识 Provider ID、Model ID 与服务端模型名称；API Key 不进入记录 |

必须清点配置默认值、fallback、会话创建／更新／恢复、导航摘要、持久任务、工具测试、回放与所有客户端；修改 DTO 时一并迁齐 Protobuf、生成客户端、schema、原持久化 owner 及测试。不是再加一个接受旧字符串的适配函数。默认模型继续由原默认 Agent／SessionDefaults 规则选择，不增加 Provider 级“默认模型”造成第二个隐式选择源。

模型管理目录与会话可执行候选分开：Settings 可管理该工作区全部已配置模型，Composer 只列当前 Agent 的 primary／fallback ModelReference；收藏、最近和目录发现不扩大这个集合。原 Session owner 与执行准入都按完整引用校验，不能只因 Provider 相同就允许旗下全部模型。显式选中原 fallback 只沿既有规则将其置首、其余候选保持原相对顺序，不改写 Agent 定义；切换 Agent 时仍沿原 Session owner 解析新 Agent 的选择，不保留不允许的旧选择。旧会话的模型被移出候选后保留可辨认的原绑定及原因，后续执行要求明确选择，不自动改绑。

`same_source` 私有 reasoning 回放也必须随身份拆分迁齐，不能继续仅比较 `provider_id`。在现有封存 profile／响应 metadata 中记录实际执行的 ModelReference、原 Gateway／Workspace 归属、协议、规范连接地址、服务端模型名及认证来源身份；vault 使用 secret ID，环境使用变量名，OAuth 使用原认证 owner 可确认的身份，原协议明确无认证时记录 none，不记录秘密正文或其摘要。这些现有执行事实共同限定回放来源，不另建来源注册表或历史索引；仅修改显示名不改变来源，密钥 revision 仍归运行时失效而非模型 ID。

同 Provider 的不同 Model、不同连接／协议／认证来源不能因服务分组相同而重放私有密文。模型切换、fallback 和会话恢复统一经过原历史投影；历史缺少足够来源时不得按旧 Provider ID 或同名模型补猜，按原跨源规则移除不可重放私有块并保留原因、可见文本、摘要及工具配对。不扫描或重写既有历史，也不改变封存正文；新调用的源身份与其实际采用的快照一致。

删除只检查完整 candidate 的当前 Agent primary／fallback 及当前工作区的原 SessionDefaults 引用。共享来源只报告已观察的消费者；不扫描全部工作区／会话／Job／Trace。历史证据保留原始标识；具体旧会话无法解析已删除模型时明确要求选择，不自动改绑、重写历史或借同名模型补齐。

### D5. 模型密钥只有所属 Gateway 一个持久化 owner

继续使用现有 `${BOXTEAM_HOME}/state/gateway/credentials/secrets/` 内的密钥库与主密钥机制，配置只保存 `secret:sec_…` 引用。API Key 明文不能写入 Workspace JSONC、配置 SQLite、会话、浏览器文本模型、缓存或诊断。Browser／Electron 只使用原受保护的写入交互和元数据 API，不提供读取已存密钥明文的前端 API。

`${ENV_NAME}` 保留为显式非持久化注入来源：沿既有合同由实际发起模型请求的目标 Workspace 进程解释，界面明确标注环境归属，不读取浏览器／入口 Gateway／另一工作区的环境来补齐。它不是 Backend 密钥库；不把环境值复制入配置或自动导入 vault。最终 `api_key` 指向哪一种来源就只使用哪一种，缺失、解密失败、权限或归属错误直接报告，不尝试另一来源。

Provider 默认引用可被旗下模型继承；模型也可显式改用同 Gateway 另一引用。多个定义复用密钥只靠同一稳定 ID，不比较明文自动去重，不按名称合并，不跨 Gateway 复制。Key 名称可改，ID 保持。更换引用只改选定配置；更新共享密钥沿现有 vault revision CAS 修改，会影响其后新执行，UI 说明这两种动作的差别。移除 Provider／Model 不删除密钥；删除密钥是独立显式操作，只报告已知引用而不建立全局反向索引。

不把 VS Code 上游“删除组时删除秘密”的做法带过来，也不把它前端解密秘密交给 Provider 的方式套到本项目。

### D6. 按原执行边界解析凭据与应用配置

每次新 Job／原允许创建运行时的边界，解析该 Agent 实际可能使用的 primary／显式 fallback 所需引用，按 ID 去重后经现有受信 Gateway 内部接口一次批量 resolve；普通管理读取不 resolve。结果只进入原执行 runtime 所需内存；Gateway 密钥 revision 纳入已有 runtime generation 身份，后续 Job 重新准备时不得复用已过期凭据客户端，不创建新 TTL 缓存、秘密数据库或刷新轮询。

目录发现与快速测试的凭据解析限定在 D8 当前临时请求边界，不创建或替换正式 Job runtime，不将草稿参数／新 Key 注入 active 客户端缓存。

在途请求／Job 保持已有快照。来源保存、配置 active、下个 Job 使用及密钥存在性分别报告；密钥轮换不强杀运行，密钥删除使后续需要它的执行明确失败，不使用旧值继续冒充可用。混合修改中有需重启字段仍走原候选整体 pending，不把单个 Provider 页保存描述成当前执行已更新。

定义读取、选项本地校验、凭据状态、外部连通和真实调用是不同事实。缺少密钥或 OAuth 登录时仍能查看和修正定义；不为列表构造模型、登录或探测。

### D7. 统一原生设置、模型选择器和 JSONC

全局入口沿原管理菜单 → Settings → 模型与 Provider，初次默认入口 Gateway 的本地目标，在界面内选目标／工作区／来源；选择不激活导航工作区。从某 Thread 模型选择器进入时直接定位该对象的原 owner／Provider／Model，不能受稍后焦点变化重定向。入口不增加一套自绘外层设置页。

| 区域 | 交互 |
| --- | --- |
| 列表 | 原生列表／树的 Provider 分组、模型行、搜索、图标和次要描述；名称与 ID 分开，窄宽度省略而不改变整体布局 |
| Provider 详情 | 连接默认、凭据来源、默认请求参数、旗下模型；新增／复制／移除等次要动作入菜单，复制分配新 ID |
| Model 详情 | 服务端名称、模型能力、覆盖项；默认只显示常用字段，高级参数按需展开，数值零和 false 不显示成“未设置” |
| 参数来源 | 有效值旁显示来源层和 Provider／Model 来源，“恢复继承”删除选定层字段；“不发送”与恢复继承分开 |
| 凭据 | 选择已有密钥／新建／环境引用；显示名称、所属 Gateway 与状态，新增输入只进入短暂密码控件 |
| 快捷配置 | 仅对 schema 支持的枚举提供原生子菜单、当前勾选、说明和只读状态；更多配置定位同一 Settings 来源 |
| 原文 | 从详情定位原 JSONC 精确实体，使用同一文本模型、撤销、dirty 与原预检／CAS；不再保存第二份模型参数或上游语言模型配置文件 |

新增服务使用当前已支持协议的最小模板，先展示协议、Base URL、凭据及模型；高级请求头／代理／参数按需展开，不预设温度、reasoning 或输出限制。创建时为 Provider／Model 生成合法且不冲突的稳定键，允许提交首次定义前修改；显示名可选，不能成为后续 ID 重算依据。由同一后端解析／adapter 地址规则提供脱敏的最终目录和调用路径，不让 UI 自行判断是否补 `/v1` 或换协议。

模型选择器在 D4 的 Agent 候选范围内自动聚焦搜索，支持按显示名／稳定 ID／服务端模型名搜索、Provider 分组、当前选中、最近使用、收藏、隐藏和进入对应设置；模型信息提示容量／能力的来源，未知保留未知。收藏／隐藏／最近／展开只属原 Workbench 展示存储，按 Gateway／Workspace／ModelReference 隔离，显示时再与当前 Agent 候选取交集，不随焦点变化把另一工作区的同名模型当成最近项。隐藏当前模型不改 Session，保留当前选中展示与设置入口；不可解析或不再允许的旧偏好不触发恢复配置、修改 Agent 定义或网络探测。复制已有模型并调整参数可表达多个预设，不再引入第三层引用。

快捷编辑永久配置时也修改原来源草稿，由原显式保存链提交，不能同时写 profile 全局模型参数。收藏／隐藏与移除定义、停用服务、切换当前 Session 分开。

原 source writer 的预检、并发和未知提交结果查询继续有效。API Key 新建是 vault 写入，绑定引用是配置保存，两者分别给回执；随后取消绑定不假报密钥未创建，也不自动清理可能被复用的密钥。脏来源、目标切换或多 Tab 保存不跨 owner 合并草稿，不重发结果未知的写入。

当前 vault 没有配置 writer 的 submission key／历史回执接口，本变更不假定两者共用。密钥创建请求 `CreateSecretRequest` 增加必填 `id`，由客户端在提交前沿 `sec_<32hex>` 格式生成并冻结；Gateway 原 vault writer 原子拒绝已存在 ID，不做正文比较、覆盖或重复创建。协议、所有客户端与测试同步替换原服务端随机分配链，不保留两种创建形式。发出前仅在原 Workbench 展示存储保留原路由、secret ID 和操作种类，不保存密码、秘密摘要或整份请求。

创建响应丢失后，只显式读取原目标已有元数据列表并匹配该 ID：存在可确认该资源已创建，显示它的当前 revision 后由用户决定绑定；未找到或读取失败仍保留结果未知，不重发 POST、不按名称找替代。更新／删除继续使用原 ID 和 revision CAS，响应丢失只显示原结果未知与重读到的当前状态；revision 增加、值名称相同或当前不存在均不能证明原操作成功。后续操作需用户明确基于新元数据发起，不能作为原未知操作重试。配置引用保存仍使用其真实 submission key 查询。恢复不新增密钥操作账本、删除墓碑或后台查询轮询。

### D8. 编辑时发现目录，显式测试草稿，共用正式执行规则

#### D8.1 只有当前连接编辑触发一次自动发现

默认在当前 Provider 连接字段首次填齐、或协议／endpoint／凭据／相关请求头和网络选项改变并结束编辑时自动发现一次。触发点是离开连接编辑区域或确认该次编辑，不是每个字符变化；模型显示名、参数、列表排序改变不触发。提供当前表单的“自动获取模型”开关与显式刷新；开关是 Workbench 展示偏好，不写工作区业务配置。打开已有配置、恢复 Tab、切换焦点、保存或六 Thread 初始化不触发上游发现。

发现只查询当前 Provider 的有效连接，不要求先有 Model 或通过无关 Agent／工具配置的完整预检。修改连接时取消原请求、将旧结果标为过期；用当前编辑版本废弃迟到响应，失败保留已有配置并允许手填。当前表单内相同连接只保留一个在途请求，不增加后台服务、TTL 缓存、历史指纹库或周期重试。

发现列出上游模型名称，支持搜索、多选、选择全部匹配项、已配置／新增／本次未返回的差异及手工补充；只有用户选入后才修改原来源草稿。稳定键、显示名、能力与覆盖不被刷新重建或覆盖，同一服务端模型的多个明确预设不自动去重。分页未取全、字节／数量上限或超时后的部分结果须标为不完整，不能据此认定原模型已消失；任何情况下都不自动删除。公共参考目录、当前上游目录和真实测试结果分别标注来源与观察时刻；目录缺少的能力保持未知，不按名称猜值。

#### D8.2 两个临时业务操作，不增加配置 writer

工作区原配置 API 增加 `POST /api/v1/config/llm/discover` 和 `POST /api/v1/config/llm/test`，Gateway 仍透明路由，入口绑定的目标／工作区不因导航或焦点变化而改投。两者复用一套判别输入：`active` 使用当前已生效 Provider／Model 引用；`draft` 使用原 `source_key`、当前非秘密 JSONC 草稿、所选 Provider／Model 稳定键和本次编辑版本。发现不要求 Model ID；测试要求唯一 ModelReference。草稿由原 owner 在目标上下文合并来源并调用原 resolver，只校验当前操作所需字段，不为试一个模型执行全库引用审计、插件预检或全配置提交。

版本合同沿现有来源读取补齐：`active` 请求携页面所见的 `active_revision`／digest；`draft` 携 `expected_source_baseline`，来自原 source document 对本次参与来源的版本／摘要记录，而非仅本页编辑计数。draft 用当前已保存来源（包括尚未 active 的 pending 内容）替换所选来源为草稿后解析，其余来源不取 active 旧值或另一 Tab 未保存草稿。原 owner 在这次有界读取时一次核对基线；不一致返回配置冲突及重读入口，零上游请求，不自动重基或重发。复用原来源基线，不创建测试 candidate、额外版本库或全模型指纹审计。

后端在准入时冻结本次解析结果和实际解析的凭据 revision；发现 JSON 与测试 start／result 返回实际路由、输入模式、本页编辑版本、实际采用的 active 版本或来源基线、凭据非秘密身份／revision、选定模型及脱敏协议／地址。临时 Key 的变化同样推进本页编辑版本，不保存其正文或摘要。客户端不能仅凭编辑计数接受结果；期间出现已知来源／active／凭据变化时保留本次冻结观察并标为过期，不把旧结果回写为新版本通过。调用期间不反复读取配置、复查 vault 或重发上游请求。草稿不落盘、不注册为 pending／active candidate，也不调用 source writer 的保存／生效；用户选择“应用此模式”等动作才编辑原草稿。

已选 vault 引用仅解析本次连接所需值；`${ENV_NAME}` 仍由实际 Workspace 环境解释。新输入且尚未创建密钥的 Key 放在请求独立的短暂 `credential_input`，只允许 draft 探测使用，不写入 JSONC 的 `api_key`、浏览器持久存储或诊断。前端密码控件存活于当前表单，关闭／取消／换目标时清理；后端只持有本次请求所需内存，不增加临时 secret handle、测试密钥库或回读明文 API。用户保存时才沿原 vault 创建及来源引用保存链处理，分别反馈事实。

`credential_input` 使用 `{ scope: "provider" | "model", provider_id, model_id?, value }`；provider 范围禁止 model_id，model 范围必须包含 model_id，且只能绑定本次请求选中的对象。它在本次内存副本中分别替换 `defaults.api_key` 或指定 Model 的 `overrides.api_key`，随后按 D2 继承，再解析最终唯一凭据；不是旧引用失败后的 fallback。已替换的失效引用不读取、不解密。Provider 临时 Key 不压过 Model 原有覆盖；若提供的临时输入未成为当前操作的有效凭据，明确拒绝并指出覆盖来源，零上游请求，不悄悄测试另一把 Key。Provider 目录发现只接受对应 provider 范围输入，active 和 OAuth 模式拒绝此 API Key 输入；保存绑定沿同一 scope 写回原来源引用。

#### D8.3 流式和非流式都必须真实调用

“快速测试”默认选当前模型／连接有相关改动时的草稿，否则选 active，并在按钮旁显示实际采用版本；有 pending 时不能把 pending 当 active，用户可将当前来源明确作为 draft 测试。用户可明确选择任一现有模式，active 测试不能带草稿或临时 Key 混入。默认只对当前一个模型发简短文本请求，使用配置中真实参数及 `api_mode.streaming`；另有“测试流式”“测试非流式”或显式选择两种，后者是两次明确调用。不猜协议、不重试换模式、不用 fallback 模型冒充所选模型成功，不启动 Agent／Skill／工具。已有工具准确率入口继续沿原 Agent、ModelReference、run_id 和重复次数合同，快速测试不自动调用它；首版不提供未实现的视觉探测或完整 Playground 控件。

通过原模型构造及协议 adapter 发请求，快速测试无自动重试；保留原执行必需边界，不套用重型工具准确率重试流程。非流式验证有效完整结果；流式按各协议验证有效内容／事件及正常完成，HTTP 200、心跳或只有响应头不算成功；短回复只有一个内容片段也不判失败。测试增量沿既有流式响应传输并穿过真实 Gateway 至当前面板，不能在后端收完再只给 JSON 摘要。页面连接保持流式不表示上游请求也流式。

当前 ChatGPT OAuth 的原 SDK 只发送流式 Responses 请求；选择非流式时在读取凭据和请求上游前明确拒绝，不能用收集流后返回完整结果冒充非流式支持。其它支持完整响应的协议继续使用真实非流式请求。

结果按模型／协议／模式分别显示未测试、进行中、通过、本次失败或取消；明确拒绝能力时显示上游原意，但一次超时或临时故障不能永久写成不支持。记录请求至首个有效内容与正常结束耗时，非流式首内容就是完整结果到达，未知 usage 不估造。通过仅表示所示版本和时刻的本次观察，不承诺持续可用。根据原配置变更通知、密钥管理回执或显式重读得到的新基线／revision 标记相关旧结果过期；没有通知的其他窗口／进程变化不假称已实时检测，显式刷新只读当前版本、零自动重测，不为此新增后台轮询。临时 Key／环境来源没有可比较的 vault revision 时只标来源、编辑版本及观察时刻，不取秘密摘要制造版本。结果不写回能力、业务配置或新持久历史。

#### D8.4 有界请求和可直接修正的错误

目录发现默认总期限15秒，采用现有有界 HTTP 读取原语，累计响应正文最多2MiB、模型最多5000个；支持协议明示的分页，所有页共用总预算，不能每页重置期限或猜路径遍历。到限时报告不完整原因，只保留已完整解析的页，不修补截断 JSON 或把截断当完整目录。推理快速测试默认总期限60秒，可在现有执行期限允许范围内显式调整；不为“快速”擅自添加 max_tokens、禁用 reasoning 或改采样。结果仅保留最多64KiB脱敏展示片段，达到预览上限不影响真实结束判断，非流式响应仍用原有界读取限制。

单次发现／测试支持取消，取消关闭本次服务端等待与上游连接，不宣称已撤销上游计费或已完成。关闭测试面板或改变目标停止本页临时请求；这不改变已有后台工具准确率测试的生命周期。批量测试只覆盖用户明确选中的非空模型范围、每次最多3个在途调用；停止后不派发剩余项，取消在途并保留各项已知结果，不搭建调度系统或全目录测试循环。

#### D8.5 单次 POST 的传输、终态与取消合同

`discover` 返回原 `APIResponse` JSON；客户端对这一个入口显式使用20秒完整请求期限，容纳15秒发现预算及5秒传输收尾余量，不修改普通 JSON 默认15秒。该余量不放宽 D10 的软件内部延迟预算。

`test` 无论上游流式与否均返回 `text/event-stream`，通过现有 `requestGatewayResponse` 的 POST fetch 及原 SSE 解析／心跳原语消费，不使用只支持 GET 的 EventSource 或恢复型订阅器。请求体仍为原业务 DTO；响应 data 及 HTTP 头携带同一个原 request_id，事件 DTO／Protobuf 和生成客户端一并定义。事件合同如下：

| 事件 | 次数与含义 |
| --- | --- |
| `start` | 成功进入测试流时一次，携带冻结的路由归属、D8.2 的来源／active 与凭据观察版本、ModelReference、协议和测试模式；发出后才等待上游响应，不等待首个模型内容才打开页面流 |
| `delta` | 零到多个，传递规范化的可展示内容；达到64KiB预览上限只停止追加展示，继续消费协议终态与 usage |
| `result` | 正常可达时恰好一次终态，status 为 passed／failed／cancelled，沿用 start 的观察版本，携带真实耗时、usage 或未知、脱敏错误及 preview_truncated；只有完整有效协议结束才可 passed |

发头前已确定的鉴权／本地输入拒绝沿原非2xx错误格式；发头后失败或超时发送 failed result 并关闭，不能再改 HTTP 状态。心跳是原 SSE 注释，不是 delta 或成功证据。不带可恢复事件游标；缺少 result 的 EOF／中途断连按失败处理，显式本地取消按取消处理；不得自动重连或重放推理 POST。协议结束时仅一个有效片段仍可成功。

原共用 HTTP 入口在明确 invalid local token、尚未业务准入时的一次凭据刷新保持；它不得增加上游模型调用。网络错误、结果未知或流中断不能冒充该认证拒绝而重发业务，不为测试绕开原 HTTP 凭据入口或修改全局重试策略。

Backend 从接收操作起使用一个绝对总期限（默认60秒，含准备及上游等待）；客户端在所选期限外只留5秒传输收尾余量，覆盖取响应头和读完整正文，不套用普通 JSON 的15秒超时，也不让心跳重置业务期限。SSE 头与 start 必须及时穿过各层 Gateway，避免首内容慢导致代理响应头等待耗尽；已有心跳保持传输活跃，不增加探测轮询。

取消沿同一请求传递：客户端 AbortSignal 同时取消 fetch／reader，各层 Gateway 断开并关闭对应上游响应，Backend 结束本次生成器及未完成的准备／模型调用，并在 finally 结清连接。覆盖首个 delta 前的等待与非流式调用；不只停止前端渲染。断开后不保证可送达 cancelled result，页面只报告本地取消事实，不假报上游已停止计费。无额外 cancel API、测试 Job、恢复队列或持久游标。

错误区分本地字段、凭据来源、网络／TLS、鉴权、路径／协议、模型不存在、限流、上游错误和超时；有结构化证据才细分，无法确定时保留脱敏状态码／原错误，不按模糊文本猜诊断。短摘要与可展开详情并存，附原 request_id 和定位字段／JSONC 的动作；目录接口404不能据此判定模型调用不可用。局部状态就地展示，自动发现不弹连续全局错误 Toast，不因展开详情改动整个工作台布局。

### D9. 保持现有目录及权威入口

```text
configs/
  workspace_schema.jsonc、workspace_inline.jsonc、workspace_dev.jsonc
  migrations/                         # 仅经 release 核实或明确授权的必要转换
app/
  services/infrastructure/config/     # 唯一 Provider／Model 解析，沿现有 llm_resolution
  services/infrastructure/config_service/ # 正式来源、inspect、预检及保存
  services/infrastructure/gateway_secrets/ # 原请求期批量凭据客户端
  runtime/provider_model.py           # 统一选项校验、按需构造；adapter 不重复继承合并
  agents/providers/wire_finalize.py、agents/sealed_assembly_dispatch.py # 真实传输选择与封存／发送一致性
  services/infrastructure/rollout_context/checkpoint/seal/ # 原 Saver 封存 profile／参数与 preimage
  api/config.py                      # discover/test 工作区业务入口；不复制 Gateway 端接口
  gateway/secrets/、gateway/routes/secrets.py # 唯一密钥持久化与管理 API
  services/business/…                 # 原 SessionDefaults／Session／任务 owner
proto/boxteam/                         # 原公开协议中统一 ModelReference 与消费者
src/clients/
  workbench/src/settings/             # 原生入口、设置导航、原生选择控件
  web/src/components/settings/        # 复用的实体字段与来源草稿
  web/src/…                           # 原 Composer、API、业务状态消费者
```

优先修改现有 owner，不为这次设计新增“模型中心”“凭据桥”“兼容服务”或通用配置框架。resolver 可返回必要来源信息，但不把多个临时投影变成持久事实源。新懒加载依赖继续归 runtime；首版不需要新的第三方依赖。

### D10. 用少量真实纵向场景约束性能与完整行为

复用现有测试装具和明确的本地模型测试服务；Workbench、Gateway、Workspace、配置保存和真实请求发出均走产品入口，不拦截其业务响应或预写浏览器缓存。模型测试服务捕获实际接收的模型名、协议、参数和测试凭据，证明各 adapter 执行规则，测试秘密不进入截图、普通日志或报告。

核心场景：稳定键与跨层小覆盖；六 Thread 同屏的独立模型选择与目标切换；共享密钥复用／轮换／缺失；原生 Settings 与 JSONC 同草稿／恢复继承／CAS 冲突；真实请求的参数优先级、省略、协议和 fallback 引用；连接编辑自动发现一次／取消旧请求／目录不完整及手填；草稿测试零保存／临时凭据不落盘；仅非流式上游／真正流式上游／HTTP 200 中的错误／中途断流；用户未编辑连接或显式测试时零上游探测。

流式本地服务须先发有效内容，等待测试控制信号后再发结束；浏览器经真实 Gateway 在服务结束前已显示第一段才算通过，不能用最终拼接文本证明增量。每种现有协议均以拒绝流式的 fixture 验证正式 Job 从 Saver 封存至 adapter 真实发送，核 `stream`／`stream_options`、sealed preimage 与实际正文一致，消息、reasoning、工具调用和 usage 正确；不跳过 binding 或放宽 hash 校验。测试接口单独通过不代表执行已接入。用可控迟到发现响应证明不会回写旧连接；取消与停止批次由上游已接收请求数／断连事实验证，不只检查按钮状态。

补齐三类场景：Provider 默认与 Model 覆盖采用不同协议、以及来源切换协议时完整替换 reasoning 线格式，保留通用单字段继承并拒绝错误协议块；本页编辑版本不变但其他来源已保存 pending／active 时在准入前拒绝旧基线，以及调用中发生已知密钥轮换时保留原观察并标过期；Settings 全目录与 Composer 当前 Agent 候选分离，同 Provider 未列入候选的另一个 Model 即使被收藏也不可执行，显式切换与 fallback 重排保持原合同。

补充五类定向回归：同 Provider 不同 Model／连接的私有 reasoning 不跨源回放及缺来源历史恢复；创建响应丢失按预定 secret ID 查元数据且不重发、更新／删除未知不误判；extra_body 同名参数与别名冲突网络前拒绝及省略标记不复活低层值；失效旧引用被临时 Key 替换、Model 覆盖阻止 Provider 临时 Key 误用；真实 POST SSE 的 start／delta／result 顺序、无终态 EOF、发头前后错误和首内容前取消。用可控服务暂停首内容超过15秒再正常完成，证明客户端不误用普通 JSON 期限；该外部等待单列，不混入暖读性能样本。

性能 fixture 使用三层覆盖、20 个 Provider 每个10个模型、多个引用共享密钥、六个 Thread 编辑器。完整列表／来源读取不超过512KiB时，至少20个暖样本满足 p95≤500ms、max≤1500ms；首次未缓存业务读取≤3000ms；从入口到对应模型设置真实可操作、以及打开正确会话 Composer 每次≤2000ms。若数据变大超过512KiB，沿原有界列表／详情合同读取，不能解除限制后把全量解密或探测塞进一次请求。

排队、内部 Gateway resolve、校验、序列化和已有重试计入软件内部总耗时；外部模型服务等待、构建和进程启动单列。保存并生效到下一次新 Job 的软件处理沿相同普通 API 预算，不把“按钮恢复可点”当保存完成。发现／测试的软件内部完整处理同样沿暖请求 p95≤500ms、max≤1500ms，明确扣除并单列外部等待；本地可控服务无外部延迟的完整请求不能假借总期限放宽。浏览器收到有效测试内容至可见每次≤250ms，无须增加生产监控，沿现有事件／浏览器证据计时。测试带进程外超时和内存上限，保留命令、源码指纹、request_id、原始样本、p95／max及失败阶段；不为本变更新增生产性能监控、全仓默认长跑或重复取绿。

## Risks / Trade-offs

- [稳定键结构改变配置与持久选择] → 同一切片迁齐直接／间接引用和生成协议，再物理删除旧读写；不以字符串兼容 adapter 延期。
- [参数优先级改变真实请求] → 明确作为 breaking change；按实际 adapter 请求验证，不仅核 JSONC 或 UI；历史封存请求不改写。
- [用户误把移除覆盖理解为删除继承定义] → 明确显示下层来源，区分移除本层和到定义来源删除，不增加隐藏 tombstone。
- [共享密钥轮换影响多个模型] → 区分更新共享值与改引用，显示已知影响；不承诺未知离线工作区已应用，不全局扫盘求完整。
- [设置保存与密钥创建不是同一事务] → 分别显示真实结果，保留可复用密钥；不新增跨 owner 两阶段提交或自动补偿。
- [模型目录能力信息不完整] → 保留手填和显式声明、显示未知；既有执行必需能力校验保留，不用猜值换成功。
- [原生控件可复用但上游配置 owner 不同] → 复用 UI 和交互，仅保留本项目一个业务 writer，避免同时写两份模型参数。
- [自动发现误用旧连接或将目录缺失当模型失效] → 仅连接编辑提交触发，取消／版本隔离，部分结果明确标注，刷新不改已有定义。
- [草稿测试绕过正式请求规则或泄漏新 Key] → 同一 resolver／adapter，独立短暂输入、零保存，输出限长且脱敏；不另造模型客户端和临时秘密持久层。
- [流式测试通过但正式请求仍强制流式或 Gateway 缓冲] → 真实请求捕获 `stream` 和浏览器先见内容的门控场景同时验收，非流式正式 Job 一并覆盖。

## Migration Plan

规划时 `bun run release:policy list` 返回空数组，尚无可核验的 `R_current`／`R_previous`；不把当前根／dev 配置当作发布契约，也不自动读取／改写它们。

实施先按当前目标结构迁齐 schema、解析、协议、原 owner 和前端消费者，并递增 Workspace 配置域版本；Gateway 配置域未变不机械升版。受影响数据库只有实际结构／语义改变才在原 owner 增加必要迁移。删除旧链前全仓检查所有调用方、fixture、测试及生成代码；不借本改动删除既有已发布迁移。

若核实支持基线含平铺 Provider 或旧持久模型引用，沿原迁移入口离线构造 Provider／Model 稳定键映射并更新确需转换的当前引用，保留原服务端模型名、显式参数与 fallback 顺序。API Key 明文转入 Gateway vault 涉及凭据写入，必须有明确来源与支持契约或用户授权，预览／备份／原子发布／失败恢复复用原机制；不得在普通读取中导入、按明文自动合并或批量跨 Gateway 搬密钥。旧强制覆盖与新默认优先级的差异必须列入转换预览，无法保留意图时阻止该转换并定位具体字段。

无支持基线时继续当前功能和隔离 fixture 验证，报告升级覆盖缺口；真实用户数据保持原样。发布回退通过原发布／备份恢复流程，不能运行时双读新旧结构、降级猜模型或自动回滚密钥轮换。
