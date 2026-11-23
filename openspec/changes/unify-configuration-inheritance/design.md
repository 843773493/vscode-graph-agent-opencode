## Context

动机见 proposal。现有可复用基础为：

| 已有能力 | 本次处理 |
| --- | --- |
| `configs/runtime.py` 的 JSONC、递归合并和最终校验 | 收敛为唯一节点解析算法；去除按业务路径硬编码的合并特例 |
| Gateway／Workspace 各自的 source owner、source revision／digest、active／pending snapshot | 保留所有权及发布流程，来源集合改由实际可达依赖决定 |
| 来源正文编辑、配置 URI、JSONC 脱敏往返、CAS | 扩展节点定位与依赖基线，不造第二个 writer |
| watcher、用户来源 fan-out、配置事件与重载策略 | 跟踪当前及候选依赖，继续由各 Workspace 独立导入 |
| `llm_resolution.py`、模型 inspection 与原生 Settings | 消费唯一解析结果，删除 defaults／overrides 第二遍合并 |
| HTTPX／模型 SDK 客户端 | 在原创建边界注入确定的代理路由，消除 SDK 隐式读取代理环境 |

当前外部请求部分依赖 `trust_env`；Provider／Model 只有 `request_options.no_proxy`。内部控制面多数显式直连。Gateway 的 HTTP 配置不能改变 Backend 进程或 SSH、浏览器页面的实际出网方式。

本次检查模板版本为 Gateway v5、Workspace v3；未找到 `refs/boxteam/releases/` 发布基线。实施前必须重核，不能以当前开发文件推测兼容合同。

2026-11-22 实施前重核结果仍为 Gateway v5／Workspace v3，目标分别为 v6／v4；`refs/boxteam/releases/` 为空。当前不依据开发 Home 新增迁移或覆盖真实配置，格式切换时由各 owner 同批更新 schema、模板、安装器和保存入口。直接／间接消费者按以下落点迁齐，业务 API 的平面投影不机械套入源文件信封：

| 链路 | 已确认的入口和消费者 |
| --- | --- |
| 固定来源合并、诊断、安装 | `configs/runtime.py`、`configs/diagnostics.py`、`configs/installer.py`；既有 `configs/maintenance/runtime_logging_*` 保留历史维护职责 |
| Gateway 来源及发布 | `app/gateway/config/{loader,sources,source_edit,reload_lifecycle,reload_pending_restart}.py`、Gateway state 的 source／active／pending 合同 |
| Workspace 来源及发布 | `config_service/config_source_layers.py`、`config_reload.py`、`config_llm_probe.py`；`config/source_owner.py`、`source_document.py`、`snapshot.py`、`store.py` |
| 模型选项及 UI | `config/llm_resolution.py`、`runtime/provider_model.py`、`config/llm_catalog.py`；`internal_v2/config.py` 的旧 defaults／overrides 来源枚举、Web `api/configSources.ts` 与 `components/settings/Model*` 及 Workbench 原设置入口 |
| 模型实际网络 | `providers/provider_http_client.py`、`litellm_chat.py`、`openai_responses.py`、`runtime/embeddings.py`；普通认证模型仍可能经全局 SDK，需随配置切片迁齐，不能只验测试客户端 |
| 隐含 SDK 出口 | `runtime/chatgpt_auth.py`、LiteLLM OAuth authenticator、`runtime/litellm_runtime.py` 元数据下载；`runtime/model_stream.py` 与其 record 的 live transport |
| Gateway 外部 HTTP | `gateway/plugins/{sources,installation}.py`、HTTP Skill source、外部 MCP 真正出口；区分 `proxy_upstream.py` 与联邦控制通道的内部传输 |
| Backend 外部及内部传输 | `agents/tools/web.py`、`resource_platform/sources/http_skill_fetch.py`；`gateway_*client.py`、`terminal_manager_client.py`、`browser_manager_client.py`、MCP／DAP 控制客户端继续按真实传输分类 |

首批实现只建立纯解析器与已创建 Provider 客户端的确定路由，不将尚未迁移的旧 owner 宣称为已采用新信封；待整体切片接入后删除旧运行时合并，禁止增加“有新字段走新链、否则旧链”的兼容分支。

## Goals / Non-Goals

**Goals**

- 用户只需理解“本节点继承唯一父节点，写下来的值覆盖父节点”；代理不另造优先级。
- 可在 JSONC 明确组织文件、共享选项和实体配置；UI 呈现真实链路、实际 owner 和字段来源。
- 文件、Provider、Model、已有 Agent／调用参数的覆盖共用语义；原快照、密钥、封存及业务期限保持边界。
- 明确配置图与机器／进程拓扑是两回事，同名 localhost 不跨机器换算。

**Non-Goals**

- 不把插件 manifest、会话事件、数据库、普通 JSON 数据及密钥库改成继承文档；不改变原生 Workbench Settings 自身的 VS Code 解析规则。
- 不建配置 daemon、跨目标全局图、配置数据库副本、分布式事务或配置表达式语言。
- 不提供 HTTP 代理服务、PAC、VPN、系统代理、SSH 代理配置，也不接管用户命令的环境。

## Decisions

### 1. 一套算法、按 owner 隔离的森林

每个节点为 `(node_id, parent_id?, local_settings, source)`，每节点零或一个父节点。一个叶子的有效值只有一条祖先链；不同叶子可共享祖先，整体可以是多棵树。Gateway 与 Workspace 不是强制父子关系，Gateway 不向工作区广播自己的网络默认值。

相同算法指相同的节点信封、引用、合并、校验和来源语义，不代表所有 owner 共用同一个版本号或可相互读取文件。每个 owner 在本机、本 Home 与本工作区上下文解析自己的图；远端仍由远端 owner 解释。浏览器只展示／编辑该 owner 返回的结果。

拒绝“保留固定层、外加 extends 和单独 proxy profile 合并”。也不采用按字段选择父、多个 extends 数组、mixins、include 顺序或隐含的第二根默认链。

### 2. 文件信封、命名空间与默认入口

Gateway／Workspace 根文档采用同一信封；schema 仍由所属域及其 `config_version` 决定：

```jsonc
{
  "$schema": "./workspace_schema.jsonc",
  "config_version": 4,
  "extends": "builtin:workspace",
  "settings": {
    "network": {
      "proxy": { "mode": "environment" }
    },
    "workspace": {
      // 原 Workspace 业务字段归此命名空间。
    }
  },
  "profiles": {
    // 可选的内嵌配置节点；定义不自动参与有效 settings。
  }
}
```

- 根节点允许 `settings.network` 和本域 `settings.gateway` 或 `settings.workspace`；Workspace 还允许 `settings.model_options` 作为所有模型的共同祖先选项。禁止在 Workspace 来源写 Gateway 命名空间，反之亦然。
- 业务消费者仍从已校验结果提取所属命名空间，例如 Workspace 的 `llm`、`agents` 与 Gateway 的 `logging.runtime`；这是字段投影，不是另一次默认合并，不为无关 API 强制增加信封。
- `profiles.<稳定键>` 是 `{extends?, settings}` 节点，版本由所在文档管理。Workspace 选项节点可写 `network`、`model_options`；Gateway 选项节点只允许其 schema 已登记的字段。继承携带的祖先命名空间不被改写，消费者只提取自己声明的命名空间。
- `settings.model_options` 表达连接、协议、凭据引用和请求选项，不含 Provider／Model 身份或模型服务端名称。统一命名空间使 Workspace → Provider → Model 的网络继承无需转换字段路径。
- `config_version`、`$schema`、`extends`、`profiles` 是节点／文档元数据，不参加 settings 合并；禁止把父文件的版本、父指针或 profile 定义拼接成子文件定义。
- `profiles` 不作为全局注册表；定义身份为“文档 URI＋JSON Pointer”，绑定后 node_id 还包含 owner／workspace 身份；快照 generation 是版本，不用它替换稳定定义身份。两个文件的同名 profile 是不同定义，同一文件在两个工作区的解析节点也不混用。
- 发行包 inline 文档是无父根，完整声明各自业务默认及 `network.proxy.mode=environment`；Workspace 根明确声明 `model_options.api_mode.streaming=true`，保留原流式默认且给出可见来源。最终 schema 校验不自动注入另一份默认值；未配置的模型采样／输出限制保持省略。

安装器把常用链写成真实父边，而不是让 loader 再读一张优先级表：

```text
Gateway inline → 用户 gateway.jsonc → gateway_local.jsonc（Gateway 启动入口）
Workspace inline → 用户 workspace.jsonc → workspace_local.jsonc
                                              └→ 工作区 .boxteam/workspace.jsonc（Backend 启动入口）
```

箭头表示子节点继承前一个节点。安装／显式工作区初始化创建缺失的最小空节点及父指针；保留已有文件，不由普通读取自动创建。启动器显式传一个配置入口，默认如图。需要改变入口时通过所属启动配置／参数选择一个文件；入口选择不再按照“哪些可选文件碰巧存在”猜测。开发模板只用于生成配置，不自动参加继承。

`builtin:gateway`／`builtin:workspace` 解析到当前发行包默认来源；`user:gateway`、`user:gateway_local`、`user:workspace`、`user:workspace_local` 只定位本 owner 的既定 Home 文件，不隐含加载顺序。用户可以将一个中间节点的父指向自定义同域 JSONC 文件；绕过某个文件后，该文件不会继续影响结果。

这是有意的格式变更；比保留旧 payload 再做跨类型路径投影更直接。并非要求所有配置实体各建一个文件，普通使用只需现有文件和少量内嵌节点。

### 3. 引用规则和上下文绑定

`extends` 只接受一个引用：

- 本机文件路径／同 owner 的既有 config URI，可带标准 JSON Pointer fragment，定位根或 `/profiles/<key>`。
- `builtin:*`、`user:*` 是上一节限定的本地来源别名。
- `context:workspace` 或 `context:gateway` 明确引用本次解析的最终 scope 节点；`context:provider/<provider_id>` 引用最终 Workspace 目录中该 Provider 的选项节点。

相对文件引用以**声明它的来源文件目录**为基准，不以 cwd 或打开编辑器的位置为基准。继承来的 `config_ref` 字符串保留其原定义来源的基准；不得把用户文件中的 `#/profiles/a` 改解释为工作区文件中的同名 profile。路径别名／符号链接归一后，同一文件有同一来源身份，不能通过不同写法绕过循环检测。对外 URI 继续沿既有 `kind=config` 文法，由 owner 登记，不能用 basename 当唯一 ID 或暴露本机路径充当跨目标 URI。

`context:*` 不是全局“当前工作区”，也不是模板表达式：绑定键包含真实 owner／workspace 身份和本次配置快照。A、B 两个工作区即使引用同一用户文件，得到的父边分别明确指向 A、B；单次图中的每个节点仍只有一个父。UI 展示绑定后的边与原始引用。禁止 `context:gateway` 从 Backend 访问 Gateway 配置，反之亦然。

仅文件和同 owner 配置 URI可作为持久来源，不支持 HTTP 拉取父文档、联网追踪引用或跨 Gateway 的自动继承。Gateway 禁止读取工作区 `.boxteam/` 来源，不能用文件引用绕过所有权；跨域字段与父引用在候选预检中失败。

缺父、非节点 fragment、未知别名、循环或缺少 schema 必需值均失败，报告来源 URI／节点／字段与链路；不跳过父节点，不按旧文件顺序兜底。解析只跟随明确引用，不扫描配置目录。节点形状校验适用于已读文档；父链和最终值校验适用于入口及已配置消费者所引用的节点，不递归展开普通业务数据中的任意 URI。

active／pending 恢复由原快照 owner 固定本次解析所使用的来源定义（来源 URI → 原始节点文档），随原快照记录保存，不另建配置库或原生路径注册表。恢复仅使用这批定义解释引用；缺失定义直接失败，不读取现有父文件补齐旧快照。URI 到本机文件的定位由原 owner 在内存维护，继续依据入口、别名和文档中显式引用定位；当前文件变化及 watcher 依赖与历史定义分别处理，固定定义不能冒充当前磁盘 CAS 基线。

### 4. Provider／Model 选择节点，避免二次合并

Provider／Model 目录仍以稳定 ID 为对象键，位于 `settings.workspace.llm.providers`；目录身份与选项继承分离。Provider 与 Model 各声明一个 `config_ref`，仅**选择**选项节点，不在选定结果外再加覆盖。

```jsonc
{
  "config_version": 4,
  "extends": "user:workspace_local",
  "settings": {
    "workspace": {
      "llm": {
        "providers": {
          "lab": {
            "name": "实验模型服务",
            "config_ref": "#/profiles/lab",
            "models": {
              "fast": {
                "model": "upstream-fast",
                "config_ref": "#/profiles/fast"
              }
            }
          }
        }
      }
    }
  },
  "profiles": {
    "lab": {
      "extends": "context:workspace",
      "settings": {
        "model_options": {
          "endpoint": "https://models.example.invalid/v1",
          "api_key": "secret:sec_0123456789abcdef0123456789abcdef",
          "custom_llm_provider": "openai",
          "api_mode": {
            "protocol": "chat_completions",
            "model_info": {},
            "supports_reasoning": {}
          }
        }
      }
    },
    "fast": {
      "extends": "context:provider/lab",
      "settings": {
        "network": { "proxy": { "mode": "direct" } }
      }
    }
  }
}
```

示例密钥 ID 只表示既有格式，不创建真实密钥；此处不增加新 adapter。`config_ref` 和目录可以跨文件继承，**节点本体不跨文件自动合并**。例如 workspace 想覆盖 user 中一个 Model 参数，应将该 Model 的 `config_ref` 指向新 profile；新 profile 的 `extends` 明确指向原模型节点，并只写改动。更改 Provider 默认而保持 Model 自身覆盖，是常用链的自然结果；若用户显式改父链，以实际链为准。

默认由 UI／安装器生成：

```text
当前 Workspace 入口
  └─ Provider 选项节点（extends: context:workspace）
       └─ Model 选项节点（extends: context:provider/<id>）
            └─ 本次已有 Agent 显式参数节点
                 └─ 本次已有调用显式参数节点
```

`config_ref` 是目录到节点的选择关系，不是第二条父边。scope 根解析其 settings 时不跟随目录中的 config_ref 进行值合并；选项解析阶段沿引用构图，避免把“Workspace 数据含 Provider 引用”误判为“Workspace 继承 Provider”。

Agent／调用节点只在已有执行边界创建，只有一个父节点与原本支持的参数，不持久化另一套 profile，不新增允许改代理／认证／消息或期限的请求入口。参数 null 的省略标记保留到最后发送边界。Provider 目录发现选择 Provider 节点；模型测试、embedding 和正式调用选择相应 Model 节点。模型回退切换到真实目标的 ModelReference 和节点，不能继续使用前一个模型的代理或凭据。

禁止为了保留旧的特殊顺序自动克隆一堆节点；新格式直接按用户明示的边解释。既有保存合同、ModelReference、私有 reasoning 来源和 request hash 语义继续成立。

### 5. 合并与字段来源

唯一公式为 `effective(node) = merge(effective(parent), local_settings)`；根为自己的 settings。类型和规则来自 schema，算法不识别 Provider ID 或业务路径字符串。

| 输入 | 语义 |
| --- | --- |
| 缺键 | 继承 |
| 标量、false、0 | 明确覆盖，不按 truthiness 判断 |
| 数组，包括空数组 | 整体替换，不拼接／去重 |
| 普通对象，包括空对象 | 递归合并；空对象不清空父对象 |
| schema 标记 `x-merge: replace` 的对象 | 整块替换；该块内字段不继续继承 |
| null | 仅在该字段 schema 接受时为显式值；不是恢复继承或通用删除符 |
| 删除本地字段 | 恢复父节点的值；若父也无值则保持缺省 |

`network.proxy` 与 `model_options.api_mode.supports_reasoning` 使用 replace。`direct` 不遗留父 proxy URL；协议线格式切换不残留另一协议字段。`request_options.parameters` 按参数名合并，但每个参数的对象值（包括 extra_body）在 schema 中声明 replace：子节点写该参数即完整替换其值，未写该参数才继承。这统一用于持久节点及 Agent／调用节点；原 Provider／Model 对象参数的递归拼接改为此规则，是明确的语义变化，转换预检须展示差异，不另留按层特例。

参数别名先归一为与协议无关的内部逻辑键，合并完毕后仅按最终叶子协议映射为线上字段。例如父写 max_output_tokens、子改为 Chat 并写 max_tokens 时只发送一个最终输出上限；不能先按各层自身协议映射后留下两个键。同节点重复逻辑键拒绝。HTTP header 按大小写无关键规范化。规范化均保留原文字段定位，不新增要求用户填写的参数名；合法 null、最小模型配置和 extra_body 冲突合同保持，不增全厂商注册表。

输出包含节点 ID、父 ID、配置身份／generation、有效 settings 以及每个有效字段的定义节点／source key／原文 JSON Pointer；原子块内部来源均来自选中的完整块。允许原有 inspection 按需展示被遮蔽定义。每字段不用持久化另一份来源数据库，直接从同一不可变解析结果投影。

这使“最终值看起来相同”和“本节点明确覆盖了相同值”仍可区分。UI、预检、目录发现、测试与正式调用不能各自重新 merge。

### 6. 依赖、保存与热重载复用原 owner

- loader 读取入口，稳定读取明确父文件及引用节点，保存源 revision／digest／generation、规范化父边和原文定位。共享文件同一读取批次只读一次。schema／引用／业务校验在候选建立时执行，不在普通读请求执行。
- 同一次候选内节点只解析一次，复用已编译 schema；不可变父 settings 可共享，局部合并只复制改变的对象路径，禁止为每个 Model 深拷贝整份 Workspace。根业务命名空间校验一次，选项消费者只校验自身最终 network／model_options，不对每个模型重复全 Workspace 校验。不为此引入新的持久数据结构框架。
- source owner 仍物化独立文件和 runtime override，不把几层合成一条来源；来源集合从固定枚举改为实际依赖。已有 runtime override 是实际 scope 入口的显式子节点，在 inspection 中可见；文件配置是其唯一父，不能另保留同优先级 merge 通路。
- 改一个父文件或父指针，重建受影响的候选及其来源投影。候选同时记录旧和新的依赖集合；旧文件改动、文件新增／消失、父边变化、环境代次变化均使准备基线失效。
- promotion 复用现有域事务，对完整候选依赖集合和适用的旧依赖 presence、revision／digest、generation、active／pending／registry 基线执行 CAS；同一 source writer 锁覆盖读入到最终基线检查。外部编辑仍沿稳定文件读取／watcher 导入与已有发布边界处理，不声称能将任意编辑器写文件和 SQLite 提交变成一个物理事务。变化在最后检查后到达时，按后继 generation 处理，已经封存的请求不改写。
- source revision 已改变但有效值未变，更新来源与依赖记录，结果沿现有 unchanged 语义，不重建无关网络客户端；父边来源变化即使值相同，也须更新可见 provenance。
- watcher 覆盖 active 和正在准备的候选依赖，失败候选中缺失文件的声明位置也保留观察，修复文件后可以再次预检；不得因新父缺失而丢掉原 active 的观察。
- 共享用户来源仍由真正读取它的 Workspace 独立 watcher、CAS、候选与事件处理。改父后不再依赖该来源的工作区不参加其 fan-out；不新增 Gateway 成员登记、广播或跨 Workspace 原子提交。
- 文件 writer 以明确目标／节点／源基线保存最小本地变更，引用只能指向 owner 可读取的来源。已有 dirty JSONC、脱敏往返、只读层、冲突保留和 pending 状态继续使用。不实现全图一键写回或自动重基。
- 生效分类按展开后的有效路径沿原 policy 执行：模型选项／代理影响后续允许的调用，当前 Job／已建立流／终端和已封存请求不转移。无法安全替换的消费者报告需重启，而非假报即时生效。

### 7. 代理字段与环境解释

`network.proxy` 为三种互斥结构：

```jsonc
{ "mode": "environment" }
{ "mode": "direct" }
{
  "mode": "explicit",
  "url": "http://127.0.0.1:10809",
  "bypass": [],
  "auth": {
    "username": "proxy-user",
    "password": "secret:sec_0123456789abcdef0123456789abcdef"
  }
}
```

`url` 为明确模式必填；`bypass`、`auth` 可省略，分别表示无显式绕过项、无代理认证。配置 auth 时 username 与 password 均必填，password 只接受既有命名密钥引用；不为代理新增一套密码库、复合秘密格式或自定义密文。环境模式只从执行 owner 的已加载进程环境解析 HTTP_PROXY／HTTPS_PROXY／ALL_PROXY／NO_PROXY 及小写形式，大小写优先级和主机匹配复用已安装客户端库的标准语义，但输入严格限于捕获的环境变量。禁止通过 urllib.request.getproxies 等入口在无环境值时回退到 Windows 注册表或 macOS 系统代理；本次不支持系统代理或 PAC。显式模式只使用该块的 URL、bypass、认证，不再追加环境 NO_PROXY。直连完全不读代理变量。SSL／CA 等独立环境行为不能因关闭代理环境读取而被意外改变。

代理 URL 支持项目已安装传输依赖真实支持的 HTTP／HTTPS 类型；SOCKS 等未接入类型明确不支持，不靠安装时在线补依赖。URL 禁止把用户名／密码作为新配置保存，认证只用原密钥库稳定引用；环境中已有带认证 URL 在内存解析，对外只展示脱敏地址。认证引用在实际出网 owner 的现有密钥准备边界批量解析，不在 Settings 读取时联网解密。bypass 使用传输库已支持的主机／端口规则，未知语法在预检拒绝，不新增自制 CIDR／PAC 解释器。

代理 password 与模型 API key 一起进入本次操作已有的必要凭据准备批次，只收集实际选定节点及本次既有回退目标所需引用，不扫描、解密全部 profiles。密钥轮换／删除即使不改变配置文件，也沿已有密钥 revision／失效机制影响后续调用的认证和池身份；不无限缓存旧密码，不增加每次请求全库检查。旧流保留原认证；后续调用遇到已删除引用明确失败，不能复用旧值或降为无认证。

“继承”是缺少整个本地块，不是第四个 mode。用户选择环境模式，是显式选择环境作为**值来源**，不是在最终配置后再套一次环境优先级。内置根明确选择 environment，以保留通常的外部网络使用习惯。

环境在启动／显式配置 reload 时捕获为该 owner 的网络策略值，代次沿既有配置 snapshot generation 变化，不另建环境账本；值未变不递增池身份。操作系统不能自动修改已经运行的进程环境。现有 `.env` loader 的值加载顺序保持，不新增后台反复加载 dotenv 的机制。更新 `.env` 时沿现有启动／明确 reload 能力处理，UI 如实说明当前已捕获状态，不宣称另一个 shell 的 export 会即时影响服务。

不存在每请求的“环境→工作区→Provider→Model”额外链，只有节点链上的最终块。`localhost` 是执行网络请求的机器；远端 Backend 中 `127.0.0.1:10809` 必须在远端可达，不自动复用用户电脑代理。

### 8. 请求覆盖矩阵与客户端生命周期

| 实际请求 | 策略来自哪里 | 本次边界 |
| --- | --- | --- |
| Gateway 插件包／目录等自有外部 HTTP | Gateway 最终配置／该 consumer 明确选择的节点 | 接入 |
| Backend 经 Gateway 转发的外部 MCP HTTP | 真正出网的 Gateway 最终配置 | 接入外部出口；Backend→Gateway 这一段仍直连，不静默改用 Workspace 网络值 |
| Provider 模型发现 | 该 Provider 节点 | 接入，不能借用任意已配置 Model |
| 草稿模型测试／正式 LLM／回退模型／embedding | 同一草稿或 active 的目标 Model 节点 | 接入所有现有 adapter，真流式与非流式一致 |
| OAuth 登录／设备授权轮询／刷新 | 原认证绑定的 Provider 节点，草稿登录使用草稿 Provider | 接入 SDK 的真实认证 HTTP 客户端；Model 的网络覆盖不改变共享 Provider 认证出口 |
| SDK 模型元数据下载 | 发起下载的 Workspace 默认网络节点 | 不借用任意 Model；进程共享任务按实际 owner／路由隔离，未完成下载不阻塞已有本地配置读取 |
| Backend 中现有 Web 搜索等外部 HTTP | Workspace 最终节点，或原工具配置明确选择的同域节点 | 接入已发现的第一方 HTTP 出口；不增加新的模型配置字段优先级 |
| Gateway↔Backend、本地插件控制、终端／CDP／DAP 控制通道、远端 Gateway 已建立的受管控制通道 | 内部传输合同 | 显式直连，不应用通用外部代理环境 |
| SSH 建连 | 现有 SSH config／ProxyJump／ProxyCommand | 保持；不把 HTTP 代理 URL 转为 SSH 配置 |
| Gateway 范围插件自身出网 | Gateway 节点→该插件明确选定的配置子节点 | 使用同一继承语义；仅现有协议与客户端实际接入后宣称支持 |
| Workspace 范围插件自身出网（包括终端插件自身的外部 HTTP） | 所属 Workspace 节点→该插件明确选定的配置子节点 | Backend 解析所属工作区结果，Gateway 不读取工作区文件；不自动替换成启动进程的 Gateway 策略 |
| 浏览器页面请求 | 浏览器实例的真实 owner | 不自动支持；本次不新增页面代理能力，UI 不提供伪生效控件 |
| 终端用户命令、python_exec、条件脚本、ffmpeg | 现有进程环境／执行合同 | 不把 network.proxy 转成命令环境；开发测试显式在 .env 设置标准代理变量，新进程正常继承 |

内外部分类按消费者合同，不能用“目的地是 localhost 就永远直连”替代用户配置；用户外部服务 endpoint 即使位于本机也仍采用其明确策略。内部控制面若将来需要代理，必须扩展该 consumer 的显式能力，不顺带接入本次默认。

插件范围来自插件声明和真实实例绑定，与“由 Gateway 启动”分离；插件覆盖仍为唯一父节点，不增加插件专用优先级。配置归属和协议支持须分别展示：已有实例配置通道可承载的已解析非秘密策略复用该通道，秘密沿已有准备能力，不新造 SDK／IPC 代理协议；尚未具备注入能力的插件出口明确未支持。终端 PTY 控制不因此需要新代理客户端。Shell 可以继承启动环境中的 HTTP_PROXY 等变量；工作区或插件 JSONC 的代理修改不重写运行中 Shell，也不隐式影响以后创建的用户命令环境。用户在 Shell 中主动修改环境保持原行为。

统一的是策略值、路由准备和来源，不是把所有 HTTP 请求先集中经 Gateway 发送。出网进程在原客户端创建入口使用已解析的策略，禁止通过 `os.environ` 临时切换或依赖 SDK 全局代理，从而避免 A／B 工作区并发串线。HTTPX 代理环境解析可复用库行为，但路由必须显式固定；TLS 环境逻辑保持独立。

现有模型 record／replay transport 仍是唯一录放 owner。replay 必须完全接管请求，禁用所有真实网络路由（包括 HTTPX 因 proxy 创建的优先 mounts），即使配置环境或指定代理也不得连接代理／上游。record 记录经最终有效代理实际发送的请求；live 集成验收显式选择真实网络模式，不能用回放成功代替代理命中。同步／异步和所有 adapter 都遵守同一规则，不另造录放实现。

客户端／连接池键在原缓存身份中加入实际有效路由、凭据 revision、既有 TLS／认证／协议隔离项；相同网络值的无关来源更新不增加新池。新调用切换到新配置 generation，旧流继续使用旧池；沿已有 consumer 生命周期在无引用后关闭，不新增通用排空服务、持久池账本或每请求建客户端。配置不能热替换的路径沿原 policy 要求重启。

代理失败只走已有请求期限、可取消和重试合同，绝不增加“代理失败再直连”、切代理列表或按网络故障切协议。业务已配置的模型回退仍按原规则，可转到另一个模型自己的策略，不能伪装成同一请求换线路重发。

### 9. 前端只展示同一个模型

复用原生 Settings 的分类、搜索、行控件、上下文菜单、来源跳转与 JSONC 文本模型。首次全局管理默认本地，已有对象入口绑定其真实 owner；更换导航不改写已打开的配置范围。

设置中显示“配置继承”，默认一条面包屑／紧凑链；多实体查看时按同一 owner 展开为唯一父节点树。选中 Model 可直接看到 inline → user → local → workspace → Provider → Model 的实际链，显式自定义父链则展示真实关系。字段旁显示继承／本地值、定义节点、最终值；展开细节才显示源 URI、机器及 generation。根没有恢复继承按钮，子节点该字段未定义也不提供无效恢复按钮。

代理行选择“使用父配置／使用环境代理／直连／指定代理”，第一项只删除本地 proxy 块；其余三项写完整互斥块。环境模式展示捕获来源与实际执行目标，凭据脱敏；未支持的插件／页面出口单独说明范围，不给出已覆盖全系统的暗示。

用户改父引用与字段共享原 JSONC 草稿、撤销、预检和来源 CAS。父候选选择只列 owner 可读节点，支持手写路径并预检，不隐式扩大到远端。来源本身只读时原生 JSONC 只读，不为表单增加绕过 writer 的路径。

多个实体选择同一 profile 时，普通详情默认编辑“当前对象覆盖”：在同一可写来源草稿新增继承原节点的子节点并修改该对象 config_ref，一次 CAS 保存，不悄悄修改共享定义。只有明确进入“编辑共享定义”才修改原节点，显示已知引用方及影响范围，不为此扫描全部工作区。复制模型／Provider 时复制其自有定义节点并重绑定副本自有节点间引用；Provider 副本内的 context:provider/<id> 指向新稳定 ID，外部共享祖先保留明确引用；自有定义被其他实体共享时采用私有子节点避免改动原实体。不复制完整祖先图或秘密值，后续局部编辑独立。

### 10. 代码落点与删除清单

```text
configs/
├─ inheritance/                 唯一纯解析：节点、引用、merge／schema 注解、来源投影
│  └─ AGENTS.md
├─ runtime.py                   JSONC／schema 原语，调用上述实现
├─ *_inline.jsonc / *_schema.jsonc / *_dev.jsonc
└─ migrations/<domain>/         仅核实来源后的显式版本转换
app/core/
├─ config_sources.py            复用配置来源身份，扩展 node／parent 诊断字段
└─ network_proxy.py             小型策略值／环境路由准备；无独立状态库或 daemon
app/gateway/config/             本 owner 入口、writer、快照及依赖监听
app/services/infrastructure/config/  Workspace 同上；llm_resolution 仅选择解析结果
src/clients/workbench/          原生设置控件与继承视图
src/clients/web/                原配置 API／DTO 消费与复用表单
```

目录是职责约束，不预建空模块。共享解析器不能 import Gateway、ConfigService 或业务运行时。JSONC 原文定位、schema 规范化和 source owner 保持原职责，避免 `network_proxy.py` 变成所有 HTTP 客户端的多层 facade。

每个垂直切片检索直接／间接调用、schema、模板、生成 DTO 和测试，迁齐后删除：固定 source precedence merge、`defaults/overrides` 特殊解析、`request_options.no_proxy`、外部 HTTP 请求隐式 trust_env 路由及客户端来源另算有效值。不得保留“新节点存在走新逻辑，否则走旧逻辑”。更新本次已改变行为所对应的既有配置技能／文档注释，不能让 AGENTS 或配置技能继续要求旧固定顺序；不新增无关总结文档。

## Risks / Trade-offs

- [JSONC 信封和选项引用是明显格式变更] → 独立配置域升版，完整 schema、默认样例、UI 与 CLI 同批迁齐；不把此变化伪装为兼容加字段。
- [用户显式树可能绕开默认或改错父] → 预检显示真实链和缺失字段，保留 active，禁止静默补旧父。单父机制无法把两个独立分支同时混入；用户需选择一个父并在该链内安排共享节点，不新增多继承便利语法。
- [共享配置与上下文绑定容易串工作区] → cache／inspection 身份包含真实 owner／workspace 和快照；外部 profile 的相对引用固定在其原来源，不用“当前导航”。
- [第三方 SDK 自己读代理环境] → 真实上游与假代理记录命中路径，测试覆盖流式、非流式、embedding 和目录发现，不能只检验解析 helper。
- [复杂的配置重载现有合同容易被误删] → 只替换依赖图与值解析；保留 source writer、CAS、fan-out、prepare/apply/promotion 和封存边界，不借机重造事务框架。
- [父链很长拖慢每次交互] → 在启动／变更时按图一次解析并缓存于现有快照；暖读不读文件、不全图验哈希、不解密、不探测代理。

## Migration Plan

1. 实施前重新核实当前配置版本和 `R_previous`／`R_current` 发布证据，记录真实受支持来源。检查时 Gateway v5、Workspace v3 仅用于说明；若未被并发变更占用，目标分别 v6、v4，否则按 owner 的实际版本分配。文档示例同步实际 schema，不新建独立树版本账本。
2. 先完成统一解析器及隔离 fixture 中的当前格式入口，再纵向迁齐各 owner。未完成的中间提交不能作为发布物；普通 runtime 不同时接受两种格式。
3. 新 Home 与测试工作区由原安装器生成完整默认父链；真实 Home 不在本次规划阶段改写。缺少发布基线时继续新结构实施，不为任意开发残留写猜测迁移；既有历史迁移不因此删除。
4. 对核实支持或用户另外授权的来源，在 `configs/migrations/<domain>/v<源>_jsonc_to_v<目标>_jsonc.py` 通过原维护入口先预检、备份、CAS／原子保存、校验后恢复。Provider／Model 旧的两阶段结果可能不能用一条天然父链保持所有未来编辑语义；转换须显式生成能保持当前有效值的节点并报告哪些值被固定，不得偷偷大量复制或承诺完全等价。存在取舍时保留原件并由用户选择；不在普通 loader 自动转换。
5. 新格式只走新解析。回退软件前沿原维护流程恢复其匹配版本的配置备份，停止 writer 后操作；不让旧程序直接读取新信封。文件保存、当前 active 与 pending 状态分别报告。
6. 迁齐后物理删除旧生产读链及悬空 schema／测试，历史迁移输入仅保留在受支持 owner。以真实配置入口、请求捕获、多工作区并发及原生 UI 验收替代只勾任务。

## Validation and latency

沿 `interactive-latency-budgets` 既有完整入口预算，不能另给代理解析或继承图一份可串加的秒级预算。普通 Settings／模型列表的终点是可操作 UI 或完整响应，发请求的终点包括内部准备到首次出网，外部等待另记；故障也必须落在原请求期限内。

少量代表性验收：含共享父／重名 profile 的多工作区继承；父指针更改与 promotion 竞争；环境／指定代理／直连的真实代理命中；模型回退；活跃流期间改配置；暖读父文件零读取与无代理探测。按现有装具保存规模、版本、request_id、原始样本、p95／max，先短定向检查，出现真实风险再扩展，不默认全仓长测。无源码／配置变化不重复失败长跑，不新增线上全量审计或防御性性能检测。

### 本机代理的产品集成验收

本机开发验收使用被 Git 忽略的 .env，将标准大小写 HTTP_PROXY／HTTPS_PROXY／ALL_PROXY 指向本机 10809 HTTP 代理，并保留内部控制端点所需 NO_PROXY。10809 是本机验收环境值，不进入产品默认、发行配置或测试硬编码；测试读取显式环境配置，不因端口不可用换一个地址或静默直连。根 .env 是开发安装种子，已存在 Home/config/.env 不会自动被种子覆盖；运行证据必须登记实际 Home／启动入口，只改源码种子不算运行环境就绪。修改 .env 不等于已运行服务／PTY 已捕获新值，按已有实例管理入口重启自己的验收实例后再测。

复用隔离产品测试装具，通过真实 Gateway、目标工作区与 Workbench 完成以下一次纵向验收：

1. 从终端 UI 创建新 PTY，无需临时 export 或命令行 --proxy，使用遵守标准代理环境的客户端访问明确的外部 HTTPS 服务；终端独立于激活 Thread，内部终端控制仍直连。不得要求任意用户程序都自动遵守环境代理。旧 PTY 不被改环境。
2. 从原生模型配置填入有效 base_url、协议及密钥引用，在 Provider 未配置 Model 时发现上游模型；选取结果创建 Model，执行模型测试，再从普通会话发送一个最小真实请求。覆盖合法流式与非流式配置；各调用均采用该操作真实节点的代理。
3. 通过测试代理的连接／CONNECT 记录或已有代理访问日志核实每次外部调用确实命中代理；环境变量存在或 HTTP 200 不足以证明。禁止抓取原始密钥和认证头，记录 request_id、脱敏目标和可关联的时间／连接证据。内部 Gateway／Workspace／插件通道不得命中外部代理。
4. 隔离的失败用例用不可达代理验证原期限内失败、不直连回退；replay 用出网计数验证零真实连接，与前述 live 测试分开。代理与上游不可用须报告未完成验收，不在默认离线回归中隐式发付费请求。

上述是实施后的集成门；仅修改 .env、TCP 端口探测或写入任务不能算通过。真实外部验收按需显式运行少量请求，复用既有隔离工作区、进程外超时／内存保护和产物规范，不为此新增常驻探测与监控。
