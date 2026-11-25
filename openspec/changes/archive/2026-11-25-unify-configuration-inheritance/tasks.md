## 1. 契约与实现边界

- [x] 1.1 核实 Gateway／Workspace 实际版本、release 支持基线和并发修改，登记本次目标版本；全仓列出固定层加载、defaults／overrides、no_proxy、隐式环境代理及其直接／间接消费者，区分源文件格式与现有业务 API 投影。
- [x] 1.2 在原配置／协议 owner 定义节点信封、settings 命名空间、单父引用、config_ref、inspection 来源字段和 schema merge 注解；明确原密钥引用、Agent 参数、ModelReference 与封存合同，完成相关真实类型／schema 检查，不新增独立配置服务。

## 2. 唯一节点解析器

- [x] 2.1 在 configs 的聚焦模块实现文件及内嵌节点解析、声明文件相对引用、规范化来源身份、限定别名和 owner／workspace 绑定；覆盖缺父、符号链接等价循环、跨域拒绝、同名 profile 和继承引用基准。
- [x] 2.2 将递归合并、数组替换、schema 原子块、合法 null、显式 false／0 与最小本地覆盖收敛到唯一算法；参数别名先归一逻辑键、最终按叶子协议映射，header 规范化保留原文来源。同节点别名重复拒绝；对象参数值按 schema 整体替换并明确旧持久参数递归行为的变化，删除旧路径特例的调用方后下线旧逻辑。
- [x] 2.3 产出同一不可变结果的有效值、实际父边和字段来源；候选内节点／文件只解析一次，不为每个模型深拷贝或重复校验整个工作区。用定向测试区分显式等值覆盖、恢复继承和原子块空值。

实施进度：Gateway v6／Workspace v4 的来源加载、来源编辑、模型解析、诊断及维护链路已共用 schema 驱动的 `SettingsMerger`，删除原 `_path` 特判和 defaults／overrides 第二遍合并。协议生成及真实 Workbench 类型检查通过；对象参数按当前 v4 合同整体替换，定向验证覆盖 false／0／null、别名冲突与外部文件名转义。

## 3. Gateway 配置纵向切片

- [x] 3.1 迁齐 Gateway schema／模板／安装器／启动入口到显式父链，更新 loader、source owner、inspection 和原 JSONC writer；runtime 不再按固定层序合并，不接受旧格式 fallback。
- [x] 3.2 将 Gateway 父依赖及候选依赖集合接入原 watcher、source CAS、prepare／apply／promotion、失败／重启状态；验证父指针切换、失败候选修复和 apply 期间父变更，保留 registry／连接身份／租约合同。
- [x] 3.3 增加代理互斥 schema、环境捕获及密钥准备；将插件目录／包下载和外部 MCP 真正出网路径接入 Gateway 策略，将内部 HTTP／受管远端控制路径明确保持直连；真实捕获验证，不能共享同一隐式环境客户端。

Gateway 切片验收：来源编辑定向测试32项通过；真实 Gateway 来源保存／watcher 3项通过；正式 Workbench 浏览器1项通过（默认 inline→user→local、表单／JSONC、CAS 与来源 URI），页面错误为空。外部 HTTP／Skill 真实 socket 4项通过，覆盖候选代理与来源同事务切换、认证轮换、旧流引用结清与后续刷新；相关单测及真实前端类型检查、构建通过。原件分别位于 `out/tests/temp/2026/11/24/{configuration_inheritance,gateway_http_skill,gateway_v6_acceptance}/artifacts/`。完整 Launcher handoff、SSH 与 Windows／Docker 本切片未重跑；profile 节点原文定位与完整 Settings 链树仍由第6组继续实施，不能据此视为整个变更完成。

## 4. Workspace 和模型配置纵向切片

- [x] 4.1 迁齐 Workspace schema／模板／显式工作区初始化／启动入口及读取路径；保留每来源物化和原 runtime override，将后者作为可见 scope 子节点，不保留独立固定优先级表。
- [x] 4.2 接入完整父依赖 CAS、候选／active 监听和共享文件 fan-out；验证两个工作区不同父链、单工作区冲突、停止后追赶、移除依赖和 provenance-only 更新，不建跨工作区事务。
- [x] 4.3 将 Provider／Model 目录读写、发现、测试、复制、移除、引用检查和 inspection 迁为稳定 config_ref；迁齐表单／DTO 的实际消费者后删除 defaults／overrides 二次 merge，覆盖自定义模型节点只写一个参数的真实保存链。
- [x] 4.4 将已有 Agent／调用参数接为受限临时子节点，保持 null 省略、extra_body 冲突、协议切换原子块和流式合同；验证真实序列化、Saver／request_hash／adapter 一致，不放宽 sealed binding。
- [x] 4.5 迁齐共享 profile 的真实表单写入与复制：默认局部编辑在同一草稿创建子节点并重指 config_ref，明确共享编辑显示已知影响方；复制自有定义、重绑定副本内部 Provider 引用，外部祖先保持，验证原对象不被改写，不克隆整图。

## 5. 外部请求代理纵向切片

- [x] 5.1 将有效 proxy 和凭据 revision 纳入原模型客户端身份，接入所有现有协议、同步／异步及流式／非流式请求；覆盖目录使用 Provider、草稿使用原草稿、正式执行／回退／embedding 使用真实 Model 节点，不改进程环境。
- [x] 5.2 将现有网页抓取、Web 搜索及其他第一方外部 HTTP 出口按消费者清单接入 Workspace 策略；对第三方 SDK 使用其真实客户端／代理注入能力，不以修改全局环境替代。复核内部 Gateway、文件、MCP 控制和调试请求仍直连。
- [x] 5.3 沿原 owner 生命周期切换／释放客户端，验证代理热变更时旧流存活、新调用使用新策略、相同有效路由不无谓重建；失败使用原期限和取消，不加探测、直连 fallback 或代理专用重试链。
- [x] 5.4 用隔离真实 HTTP 上游／代理捕获验证 environment、explicit、direct、NO_PROXY／bypass、认证脱敏、远端 localhost 语义、多工作区并发和模型回退；验证现有 TLS／CA 行为不因禁用隐式代理读取而丢失。
- [x] 5.5 接入 OAuth 登录／设备轮询／刷新真实 HTTP 路径的 Provider 策略，以及 SDK 模型元数据的 Workspace 默认策略；复核全局 SDK 客户端不串 owner／路由。扩展原必要凭据批次到代理 password，并验证配置未变时秘密轮换／删除影响后续调用，旧流保持，不扫描全库。
- [x] 5.6 复用原 record／replay owner，防止 HTTPX proxy mounts 绕过回放 transport；同步／异步及现有 adapter 验 replay 零真实代理／上游连接，record 记录真实有效路由，live 代理命中验收与回放分开。
- [x] 5.7 环境捕获仅输入标准大小写环境变量，覆盖冲突优先级和 NO_PROXY；验证 Windows／macOS 系统代理存在而环境为空时不隐式回退，不新增 PAC 或系统代理读取。
- [x] 5.8 按插件实例范围绑定 Gateway／Workspace 父节点与可选插件覆盖，复用已有实例配置与凭据能力，不让 Gateway 读工作区文件；未具备注入能力的出口标未支持。验证终端控制直连、Shell 只继承原启动环境，JSONC 不重写用户命令环境，不改 python_exec／条件脚本／ffmpeg。

实施进度：有效路由、owner snapshot generation 和秘密 revision 已接入正式模型／回退身份，真实 Thread 验证 API 与代理秘密轮换、删除及旧 lease 排空。OAuth 同步／异步刷新与 owner 隔离 metadata、Embedding 的真实 SDK 注入已验证；未具备注入能力的专用 SDK 明确拒绝。录放定向验证 replay 30 项和 record 22 项通过，覆盖五种 adapter 的同步／异步、环境／显式及 bypass，回放禁止构造真实 transport，录制有受控代理 socket 证据。内部 HTTP／WebSocket 显式直连定向通过。上述证据不替代第7组真实 UI／10809 live 验收。

## 6. 原生 Settings 与 JSONC 交互

- [x] 6.1 在既有 Settings 入口接入统一链／树和字段来源，支持 config_ref／extends 编辑与原文跳转；绑定精确 owner，复用原文本模型、草稿、预检和来源 CAS，不另做配置控制台或保存队列。
- [x] 6.2 接入代理模式、继承恢复和密钥引用控件；根、只读或无本地定义时隐藏无效动作，显示执行机器、真实 active／pending 和未支持的插件／浏览器页面网络范围；补齐中英文现有资源。
- [x] 6.3 真实浏览器验证本地默认入口、远端对象保持绑定、两工作区共享文件冲突、表单↔JSONC、父链失败修正与窄布局；完成相关真实类型检查及 `bun run --cwd src/clients/web build`，必要时补受影响 Workbench 构建。

## 7. 版本切换、验证与清理

- [x] 7.1 根据第1组核实来源决定受支持迁移；有真实合同／额外授权才在原 owner 编写有版本 JSONC 转换与备份恢复，预检报告旧模型双重优先级转换中可能固定的值及取舍。无发布基线不猜测开发数据版本、不改真实 Home，保留已有历史迁移及其支持链。
- [x] 7.2 更新受影响现有配置技能、AGENTS、说明和测试注释，替换固定合并顺序／两种继承的旧约定；检索清理悬空读写、旧字段 schema、客户端另算来源和不再使用的特殊缓存，不保留双读双写。
- [x] 7.3 通过实际 Gateway／Workspace／Workbench 产品入口验证配置导入、保存、热加载、冲突和原会话执行；新 Home、真实目录与 ZIP／发行包中的默认来源均可定位。发布平台按实际改动补相应检查，不以单个 helper 成功代替验收。
- [x] 7.4 复用性能装具验证20个 Provider×10个 Model、共享祖先和六编辑器并存的配置 inspection／模型设置：至少20次暖读 p95≤500ms、单次≤1500ms，可操作模型设置每次≤2000ms，首次未缓存读取≤3000ms；父文件零暖读，不能以 loading 作为终点。记录原始样本、p95／max、数据规模、request_id、源码指纹、命令／退出码及未验证范围。
- [x] 7.5 所有测试加进程外超时与内存保护，产物沿既有隔离目录；失败先归因，不默认全仓长跑或无变化重跑。复审是否能减少包装、分支、重复状态、深拷贝、重复校验和额外持久化，删除无明确必要性的新增机制，再做相关定向回归。
- [x] 7.6 增补显式 live 集成用例：从实际验收 Home 的 .env 读取标准代理配置（本机 10809），通过真实 UI／Gateway／Workspace 新建 PTY，无临时 export 或 --proxy 访问外部 HTTPS；Provider 填 base_url、协议、密钥后发现上游模型，再模型测试与正式会话请求，覆盖流式／非流式。复用已有装具，外部调用少量按需运行，不写死测试端口／凭据，不在离线套件暗发付费请求。
- [x] 7.7 为7.6保留脱敏代理连接／CONNECT 关联证据与 request_id，证明外部各路径命中代理、内部控制直连；隔离故障用例验证原期限和无直连 fallback。登记实际 Home／源码／命令／退出码及未验收阶段，根种子更新、TCP 探测或 mock 成功均不能代替产品验收。

2026-11-24 剩余审查补证：配置技能删除按文件名推荐固定合并层序的旧说明，Workspace 开发模板说明改为显式节点差异，技能校验通过。配置读取错误出口与来源／模型设置的安全呈现完成窄范围复审，相关证据见 `out/tests/temp/2026/11/24/remaining_spec_validation/artifacts/`；没有新增兼容分支、配置状态或持久账本。现有 live 原件 `workspace_external_http/artifacts/configuration-live-009.log` 已在真实 UI 模型发现阶段收到上游 `401 api_key_expired`，不重复请求、不虚报模型测试或正式会话完成。4.3 与第7组其余完整验收门保持原状态。

当前实施进度：Workspace v4 schema／模板／安装与真实 ConfigService 来源 owner 已迁齐；双工作区共享来源、停止后追赶、依赖移除、provenance-only 更新、CAS 父变更及带特殊字符的来源引用均有真实入口验证。维护链路 122 项通过，正式非流式产品测试 30 项分别已有通过证据。没有可核验 release 发布基线，本轮不为开发 Home 新增猜测迁移，保留既有历史支持链。

原生 Settings 的节点树、原文精确跳转及代理原子块已实施。正式浏览器节点验收第7轮通过（1 passed，93.01s，exit 0）：保持 Apply 后立即保存，Provider／Model 同批创建私有节点并只写局部参数，外部共享原件与 peer／sibling 不变；config_ref／extends 非法值 422 后可修正；Provider 副本的模型节点独立且不改原件；Gateway／托管 Backend 重启与新浏览器上下文读回原值和副本值，720px 窄布局可操作。页面及清理错误为空，8项源码指纹前后一致。原件为 `out/tests/integration/clients/web/test_configuration_node_persistence/runs/run-aa60e5b29519/`，命令、原始输出及退出码见 `out/tests/temp/2026/11/24/workspace_external_http/artifacts/node-persistence-007.*`。4.3 的真实外网发现／测试、6.3 的远端与双工作区浏览器冲突仍由各原验收门继续覆盖，未提前勾选。4.4 已补真实 Provider→Agent→调用参数→Saver→SDK/TCP 验收4项，以及协议原子块等4项；完整 wire body、preimage 与 request_hash 一致。5.2 的第一方 Web／DDGS／HTTP Skill 出口已接通，文件／MCP 直连结论以源码复核与现有回归为限。5.8 当前第一方插件没有已接通的自有外部 HTTP 消费者，不新增无消费者代理 SDK／字段；终端用户命令继续继承原启动环境，真实 PTY 验收已通过，证据如下。

7.4 保持未完成：20 次暖 inspection p95=135.3ms、max=137.7ms、请求窗口父文件读取为0；20次模型设置 max=1618.8ms。冷 inspection=3766.4ms，首次 Settings 完整可操作=5155.6ms，均超过预算；瓶颈证据为 LiteLLM 首次导入，未以预热或挑选样本掩盖。原样本、request_id、命令／退出码及指纹保存在 `out/tests/temp/2026/11/24/config_inheritance_latency/artifacts/`。7.6／7.7 的真实外网产品验收继续独立推进。

冷路径复审补充：当前 LiteLLM 1.110.2 的官方 URL 计算与正式参数声明均触发完整 SDK 初始化；官方 CLI 延迟入口仅覆盖 CLI client，providers.json 不足以计算任意配置 endpoint。TODO：在依赖层具备可复用轻量 API 后重新验证冷 inspection／首次 Settings；本轮不自建 URL 规则、元数据副本或预热掩盖超时，7.4 保持未完成。

2026-11-24 Settings 收口补验：独立 dist09 经真实双 Gateway／双 Workspace 浏览器完成远端对象打开后切导航仍保存原 B，以及共享父来源 A 保存、B 旧 CAS 409 保留草稿，原件 `out/tests/integration/clients/web/test_settings_source_ownership/`，受保护命令与退出码见 `out/tests/temp/2026/11/24/workspace_model_inheritance/artifacts/source-owner-ui-06.*`（1 passed，198.92s 含独占名额等待）。原文件服务丢 cause 的真实展示缺口已沿原保存通知修复，不修改 CAS 预期。无父根和未定义字段不提供可执行恢复继承；继承 Agent 判定改为消费唯一 source_inherited，相关组件18项、设置合同43项通过。Workbench 219份真实转换源码类型检查、Web全量类型检查04与完整构建09通过。4.3及7.6／7.7尚未完成：实际Home与根.env的 OPENCODE_ZEN_API_KEY 相同，上游模型发现返回401 api_key_expired；终端HTTPS已成功，未据此声称模型测试或生成通过。

5.4／5.8 补验：隔离 HTTP／HTTPS／CONNECT 捕获覆盖 NO_PROXY、同步 SSL_CERT_FILE、异步 SSL_CERT_DIR、两真实 ConfigService 并发，以及正式消息 API 的主模型失败后备用模型成功。26项回归通过；测试清理栈语法修正后，该场景另行1项通过，不表述为同一次27项全绿。远端 docker-debian 与控制机同时监听各自 localhost 同端口，目标实际 Provider client 仅命中目标代理1次、控制机0次，exit 0；原件 `out/tests/temp/2026/11/24/proxy_contract_acceptance/artifacts/run-evidence.txt`。真实 Gateway／Workbench 新建 PTY 后，将 Workspace JSONC 的代理从 environment 保存为 direct；原 PTY 与新 PTY 的六种代理变量摘要均保持原启动值，PTY 身份不同。105次连接全为 loopback，无模型调用、页面错误或清理错误；1项通过（63.76s，exit 0），原件 `out/tests/e2e/clients/web/test_configuration_live_proxy/runs/run-945a103b40a6/`。不以这些受控场景替代7.6／7.7的真实外网模型验收。

2026-11-24 最终真实外网产品验收通过：`test_configuration_live_proxy_pty_and_formal_model_entry` 1 passed／216.14s／exit 0，原件 `out/tests/e2e/clients/web/test_configuration_live_proxy/runs/run-82d5dc073521/`，命令、Home／环境来源、request_id、源码指纹和脱敏摘要见 `out/tests/temp/2026/11/24/config_live_continuation/artifacts/live-04/result-summary.json`。复用已授权环境中的 KILO 凭据及实际返回的 openrouter/free，真实UI发现上游目录，两种探测和两种正式会话均成功；临时终端无 export／--proxy 访问 HTTPS。故障代理沿原60秒期限返回真实失败，内部健康与工作区接口继续直连，无外网直连回退；Workspace JSONC 变更前后原PTY与新PTY的启动环境摘要相同。connect-only原始系统调用和各阶段请求关联保留，未采集代理认证报文。页面及清理错误为空，相关配置源码指纹无漂移。此前几轮装具失败原件保留：漏Agent绑定、未等待真实预检响应及离开Thread后缺创建终端上下文均已按原产品合同修正，没有放宽模型允许列表、CAS或资源归属。结合既有真实冲突／重启保存、默认来源目录／ZIP、77项目录配置及36项来源／安装检查，7.3／7.6／7.7完成。最小充分实现复审沿原解析／保存／Trace owner，没有新增配置状态、代理探测或账本；Ruff、JS语法及严格OpenSpec校验通过。7.4仍按冷SDK首次加载超预算的既有实测保留未完成TODO，不以暖结果代替。

目录自动发现复审又发现真实冗余请求：原Provider writer将整个request_options、api_mode编辑都当作连接修改，采样／streaming／能力修改后失焦会重新发现目录。现沿原计数与commit机制收窄至目录实际使用的连接字段及父链／网络变化，删除设置凭据时的重复计数；Model覆盖仍不触发Provider发现。原源AST＋JSONC编辑链14项／53断言通过；真实模型UI第10轮1 passed／124.83s，保留原26目录／18推理请求等精确计数、并发取消、合法无认证创建及实际来源隔离断言。收据见 `out/tests/temp/2026/11/24/failure_ui_completion/artifacts/real-ui10.log` 与 `.exit`；同目录 `ui-connection-build-receipt.json` 对应真实Web／Workbench类型检查和完整构建均exit 0，1242份构建输入当前无漂移。没有新增计数器、发现缓存或另一套保存链，7.5完成；7.4冷SDK性能仍未达标。

后续冷路径复核：通过本机10809代理取得PyPI已发布LiteLLM 1.110.3发行包并核SHA-256；`__init__.py`、`utils.py`、两份懒加载模块均与当前1.110.2逐字一致，不以补丁升级承诺冷读改善。原证据在 `out/tests/temp/2026/11/24/config_inheritance_latency/artifacts/upstream-sdk-02/receipt.json`，未改依赖或安装环境。只读inspection的最终URL字段是当前公开合同；是否改为展示配置endpoint与协议、将SDK最终URL移至模型测试／实际调用，已向用户请求确认，未获答复前保持现状，7.4不勾选。

兼容依赖与调用方补核：`openai-sdk-04/receipt.json` 使用正式 `model_request_url`，当前OpenAI 2.37.0冷执行3966.1ms，临时wheel中的兼容2.54.0为5224.4ms，返回URL相同；zip导入与已安装目录并非严格性能对照，不能据此宣称新版稳定变慢，也没有升级收益证据，依赖及锁文件未改。03因错误解析venv解释器而未进入SDK，原始失败保留，不计性能样本。独立只读复审核对active inspection与带Model引用的来源预检均确实需要request_url；LiteLLM根包无条件导入utils／main／Router／dispatch，具体Provider URL模块仍依赖根包，现有CLI lazy入口不能用于此合同。没有发现可直接移除的误触发；不自拼URL、不预热，7.4继续未完成。


2026-11-25 用户已批准调整地址展示合同：只读 inspection 展示配置 endpoint 与协议，SDK 请求地址移至显式模型测试 observation，真实调用继续由 SDK 发送。此前等待轻量 SDK API 的冷读 TODO 由此合同调整替代；本轮迁齐协议、配置投影、Settings 和实际 HTTP 验证，不增加配置格式版本或地址缓存。7.4 仍待新合同下完整冷／暖产品性能验收后勾选，既有失败原件保留。

地址展示纵向切片已迁齐：inspection 删除 request_url（proto 编号5及名称 reserved），从原 options 展示 endpoint／协议；模型测试在原 worker／期限内、创建模型前解析 SDK 地址并写入 observation。配置解析18项、真实SDK／HTTP与来源预检11项、组件／API 54项均通过；实际wire地址与测试start／result中的SDK地址一致。Web 全源码 tsgo、Workbench 219份变换源码类型检查、完整79扩展构建、协议生成及两份OpenAPI一致性通过，相关产品源码无漂移。Buf格式／lint通过；对固定HEAD的breaking检查仅获准删除字段这一项，原件见 `out/tests/temp/2026/11/24/config_inheritance_latency/artifacts/inspection-contract18/`。宽范围32项既有Ruff诊断与HEAD逐项相同，未扩大修复；本次其余生产文件静态检查通过。最小充分复审保留原解析、probe与transport owner，无新地址规则、缓存、配置版本或持久状态。必要的extra_body正式参数冲突校验仍可能加载SDK；OAuth只读测试移除无关extra_body后验证连接展示不加载SDK，不据此宣称所有配置都不加载。完整冷／暖性能及真实模型UI验收继续进行，7.4尚未勾选。

7.4 最终性能验收通过（2026-11-25）：原20 Provider×10 Model、共享祖先、六编辑器负载，1次冷inspection 139.3ms，20次未筛暖读p95／max=128.4／156.8ms，20轮可操作Settings最大1244.4ms，暖请求窗口父文件读取0；另用独立新进程／Home验证首次完整Settings 1636.8ms、冷inspection 139.6ms。两门exit 0，源码指纹无变化，不预热、不筛样、不改预算；原件为 `out/tests/temp/2026/11/24/config_inheritance_latency/artifacts/20261125T011620-248d6214/`、`20261125T011740-e59fd221/`，命令、退出码和前后指纹见同任务 `artifacts/inspection-contract18/latency-{full05,first-settings06}-receipt.json`。此前3766.4ms／5155.6ms失败证据继续保留，不能据单轮结果承诺所有环境相同收益。

最终真实模型UI回归通过：`model-ui08` 1 passed／131.25s／exit 0，三协议 SDK request_url 与真实上游路径一致，原26总请求／18模型请求和CAS／取消／导入断言保持，pageErrors为空；正式原件在 `out/tests/integration/clients/web/test_model_connection_probe/runs/run-95e610b8a2f3/nodes/node-20d5dd782416/artifacts/`。最终产品源码、四份受影响测试和Workbench资产指纹无漂移，测试代理自有进程与owner已清理。完整收据 `out/tests/temp/2026/11/24/config_inheritance_latency/artifacts/inspection-contract18/cold-sdk-review-delivery.json`。本变更31/31已完成，主规范原有3项能力块与delta逐块相同，另同步新增统一配置继承和网络代理两项主规范后归档；未同步dev／release／main或重启8027。
