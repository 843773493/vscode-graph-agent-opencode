# network-proxy-settings Specification

## Purpose

为 Gateway、工作区及模型相关的已支持外部请求提供一致、可解释的代理策略。代理选择使用项目统一配置继承机制，明确环境、直连与指定代理的行为及真实执行位置，防止跨工作区串线、SDK 隐式覆盖和失败后悄悄换线路。

## Requirements

### Requirement: 代理必须是继承链中的明确互斥配置

`network.proxy` SHALL 支持 environment、direct 和 explicit 三种模式，并作为 schema 声明的整体替换块。缺少本地 proxy 块才表示继承。environment 只采用执行 owner 已捕获的标准代理环境及 NO_PROXY；direct 不读取代理变量；explicit 必须给出已支持的代理 URL，只采用本块的 bypass 和认证引用，不附加环境优先级。非法 mode、URL、模式残留字段或不支持的 bypass／代理类型 SHALL 在候选预检失败。

#### Scenario: 模型明确直连覆盖工作区代理
- **WHEN** Workspace 为 explicit，Model 的节点将 proxy 设为 direct
- **THEN** Model 的实际请求直连，不保留父 URL、认证或环境代理；同 Provider 的另一模型仍按自己的链生效

#### Scenario: 指定代理不混入环境绕过项
- **WHEN** explicit 的 bypass 为空，而进程环境 NO_PROXY 包含目标主机
- **THEN** 请求仍经过指定代理，界面与 inspection 不宣称 NO_PROXY 覆盖该明确配置

### Requirement: 环境代理必须绑定实际执行机器和配置代次

环境模式 SHALL 使用出网 owner 的环境捕获结果及已安装客户端库的标准解析行为，不读取浏览器用户电脑或其他进程的环境。环境捕获随启动或已有显式 reload 能力更新，不增加后台 dotenv 轮询。UI SHALL 展示执行目标、模式、脱敏地址及当前代次，不能宣称另一个 shell 的 export 自动更新当前服务。代理隔离 MUST NOT 意外改变既有独立 TLS／CA 配置。

环境解析输入 MUST 仅为已捕获的标准大小写代理变量，禁止无环境值时静默回退至 Windows 注册表、macOS 系统代理或 PAC。不得用关闭全部 trust_env 而遗漏既有 CA／TLS 支持来替代代理隔离。

#### Scenario: 系统设置有代理但环境没有代理
- **WHEN** Windows／macOS 系统配置了代理，owner 捕获的标准代理变量均为空且采用 environment
- **THEN** 不使用系统代理；大小写冲突与 NO_PROXY 匹配按既定环境解析语义处理，不读取其他配置源

#### Scenario: 远端配置回环代理
- **WHEN** 远端 Backend 的有效代理为 127.0.0.1:10809
- **THEN** 请求连接远端机器该端口，不转为浏览器或本地 Gateway 的端口；不可达时报告真实连接失败

#### Scenario: 明确代理模式且环境含认证
- **WHEN** 用户检查带认证的环境代理或 auth 指向 Gateway 密钥库的指定代理
- **THEN** 配置及 inspection 不返回原始认证，执行准备才解析所需凭据，来源读取不以解密成功为前提

### Requirement: 真实请求必须使用其选定配置节点的代理

Provider 发现 SHALL 使用 Provider 节点；模型测试、正式调用、流式／非流式和 embedding SHALL 使用对应草稿或 active Model 节点。模型回退必须重新选择目标 ModelReference 的策略及凭据。Gateway 自有外部下载／目录／外部 MCP 转发 SHALL 使用 Gateway 的有效配置；Backend 第一方外部 HTTP／已支持 Web 搜索请求采用 Workspace 策略。执行端 MUST NOT 在这些结果外继续使用 SDK 全局代理或临时修改进程环境。

#### Scenario: 同一模型发现和调用有不同配置粒度
- **WHEN** Provider 经代理发现目录，一个 Model 明确直连，另一个继承 Provider
- **THEN** 捕获到的发现走 Provider 代理，第一个模型测试与正式请求均直连，第二个模型均走代理，任何路径不依赖随机选取目录中首个模型

#### Scenario: 并发请求与模型回退
- **WHEN** A、B 工作区并发使用不同策略，A 又回退到另一个有效模型
- **THEN** 每条真实请求只命中对应模型和工作区的路由，回退使用目标策略，B 不受 A 的环境或客户端变更影响

#### Scenario: OAuth 和 SDK 元数据出口
- **WHEN** Provider 登录、设备授权轮询或刷新凭据，SDK 另下载模型元数据
- **THEN** 认证始终使用原绑定 Provider 节点，元数据使用发起下载的 Workspace 默认网络节点；不得使用 SDK 隐式全局代理或任意一个 Model 的策略，多 owner 的共享下载不串线

### Requirement: 通用外部代理必须保持内部控制通道边界

Gateway 与 Backend、本地受管服务、终端、CDP、DAP 及既有受管远端控制通道 SHALL 按原内部传输合同显式直连，不被通用代理环境劫持。分类必须依据消费者而非仅判断 URL 是否回环。SSH SHALL 保留已有 SSH 配置方式；浏览器页面、插件自行出网和用户命令不因 Backend HTTP 设置而自动获得代理能力。未接入消费者 MUST 在 UI 明确支持范围，不展示伪生效开关。

#### Scenario: 环境代理不通但内部 API 可用
- **WHEN** HTTP_PROXY 指向不可达地址，用户通过 Gateway 查询工作区或使用内部插件控制通道
- **THEN** 内部请求保持原直连链路，不等待外部代理；真实外部请求则按选定策略失败

#### Scenario: 本机的外部模型 endpoint
- **WHEN** 一个模型服务 endpoint 位于 localhost，且模型明确选择指定代理
- **THEN** 仍按模型配置处理，不能将其误归类为内部控制面并强制直连

### Requirement: 插件配置归属必须跟随实例范围

Gateway 范围插件自身外部请求 SHALL 继承 Gateway 节点；Workspace 范围插件 SHALL 继承实例绑定的 Workspace 节点，插件覆盖仍使用同一单父规则。Gateway 负责启动不改变插件配置归属，MUST NOT 为此读取工作区文件；所属 Backend 解析工作区策略。只有已有配置与凭据通道、实际 HTTP 客户端已接入的插件可声明支持，不新增代理专用 SDK／IPC，也不让未接入插件的默认环境行为冒充已应用 JSONC。浏览器页面网络仍不自动接入。

终端控制面 SHALL 直连；PTY 用户命令 SHALL 沿原进程环境合同运行，不把 network.proxy 静默转换为代理环境变量。用户显式在开发 .env 设置标准代理变量时，新插件／PTY 可正常继承；修改 JSONC 或 .env 不改写已有 Shell 环境。python_exec、条件脚本、ffmpeg 原执行机制保持。

#### Scenario: Gateway 启动工作区终端插件
- **WHEN** Gateway 和 Workspace 配置不同网络策略，并由 Gateway 启动该工作区终端插件
- **THEN** 插件已接入的自有外部 HTTP 按 Workspace／插件节点，控制通道直连，用户 Shell 环境不被上述策略重写

#### Scenario: 开发环境显式设置终端代理
- **WHEN** 实际启动入口加载的 .env 配置标准代理变量，随后创建新的终端实例和 PTY
- **THEN** 遵守该环境的命令无需额外 export 即可走代理；旧进程保持原环境，不声称任意程序都自动支持代理

### Requirement: 代理变更必须复用客户端生命周期且不隐式降级

策略及凭据代次 SHALL 进入现有客户端缓存身份，在已有安全生效边界供后续调用采用。活动请求保留原策略，旧连接池在不再使用后沿原 owner 生命周期关闭，不按每次请求新建池。失败 SHALL 使用已有期限、取消、错误和重试合同，不另增代理探测或重试预算，不自动换直连、换协议或覆盖用户的模型回退规则。

#### Scenario: 流式请求期间修改代理
- **WHEN** 已建立的模型流仍在输出，代理候选通过并适用于后续调用
- **THEN** 原流不中途转投，新调用使用新有效策略，旧池最终释放，单纯来源变化而路由未变不强制重建池

#### Scenario: 代理不可达
- **WHEN** explicit 代理连接失败
- **THEN** 在原请求期限内返回原失败阶段及 request_id，不先做秒级全量检查或再直连上游；不得通过放宽总交互预算获得通过

#### Scenario: 代理密码轮换或删除而配置未变
- **WHEN** Gateway 密钥库中的代理 password 更新 revision 或被删除，配置文件未修改
- **THEN** 原必要凭据准备批次同时处理该引用和 API key，后续调用使用新认证或明确失败；旧流保持，不沿用过期池认证，也不每请求扫描全部配置和密钥

### Requirement: 录放必须保持网络隔离和代理真实性

现有 replay transport SHALL 完全接管模型请求，禁止 proxy mounts 或 SDK 客户端绕过它连接真实代理或上游。record SHALL 覆盖按有效策略实际发送的网络请求；live SHALL 明确使用真实网络。三种模式复用现有录放 owner，不新增一套实现。

#### Scenario: 回放时配置指定代理或环境代理
- **WHEN** 任一现有 adapter 在 replay 模式执行，且选定配置含非空代理
- **THEN** 返回既有录制结果，代理与上游真实连接计数均为零；同步与异步路径均满足

### Requirement: 终端和模型代理必须经过真实产品集成验收

实施验收 SHALL 复用实际 Gateway／Workspace／Workbench 与隔离工作区，显式加载验收 .env，创建新终端、发现上游模型、执行模型测试并完成普通会话请求。端口／凭据从本机环境取得；本机约定 HTTP 代理为 127.0.0.1:10809，不进入产品默认或测试硬编码。外部验收显式选择 live 模式，少量请求按需执行，不能污染默认离线回归。所有测试沿原进程外保护、期限、产物和秘密脱敏约定。

#### Scenario: 终端和模型均使用本机验收代理
- **WHEN** 已按实际 Home 加载验收 .env，从 UI 新建 PTY 执行一次外部 HTTPS 请求，再配置只有连接信息的 Provider、发现模型、选择模型测试并发送正式消息
- **THEN** 环境感知的终端命令、目录发现、模型测试及正式模型请求均有可关联的代理连接证据；合法流式与非流式均验证，内部控制请求不命中代理；仅变量存在、HTTP 200 或 mock 返回不算通过

#### Scenario: 验收环境尚未加载或不可达
- **WHEN** 只修改根 .env 而现有 Home／进程未更新，或代理／上游不可用
- **THEN** 报告实际 Home、捕获代次和未完成阶段，不伪报通过；不可达代理在原期限内失败，不直连重试；不重启其他用户或任务的实例
