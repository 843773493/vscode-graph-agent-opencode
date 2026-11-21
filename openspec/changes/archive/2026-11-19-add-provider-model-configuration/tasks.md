## 1. 确认合同与支持边界

- [x] 1.1 全仓清点平铺 Provider、`primary_provider`／`fallback_providers`、`current_provider_id`、`provider_ids` 的直接／间接读写、协议、持久选择及测试；区分其他业务同名字段，形成具体迁移清单，不误删搜索 Provider 等无关概念。
  - 2026-11-17 核对 `app/src/configs/tests/docs/proto`：执行、配置、Session、队列、任务、工具测试、封存来源和正式协议消费者已按纵向切片迁移；余下测试入口包括 Launcher／插件工作区、通信／进程独占、rollout 历史、MCP／Browser／Debug、HTTP 错误、远端资源和 context／工具 E2E，具体文件与指纹保存在 `model_configuration_closure/artifacts/model-remaining-consumers-static-03.json`。旧自动热迁移重试测试改为当前配置 watcher 生效后的真实失败重试，产品不恢复开发兼容。团队协作测试仍按用户裁定延期；固定旧源码生成器和 legacy migration 输入保留。Proto reserved、Responses 内部 item ID 集合及 Web 搜索 Provider 属于不同职责，不按旧模型字段删除。盘点完成不代表所有消费者验收通过。
- [x] 1.2 核对真实 release 支持基线及用户数据来源，确认 Workspace 配置升版和确实受影响的数据库 owner；无基线时保留已记录缺口，以隔离 fixture 继续实现，不自动转换根／dev 数据或凭据。
  - 发布登记仍为空，按用户裁定的首次发布处理。Workspace 配置版本为 3，会话模型绑定由原 Session catalog 的 `current_model_json` 保存，SessionDefaults 使用版本 2；来源位置沿原 WorkspaceStateStore 基线保存。未新增开发数据迁移，未验证正式旧版升级。
- [x] 1.3 在原协议／配置 owner 确定 ModelReference、回放来源、请求参数及 streaming schema；补齐原 source document 来源基线、discover／test 的预期版本及实际凭据观察版本，定义必填创建密钥 ID、临时凭据 scope 及测试 SSE start／delta／result，生成各客户端。无认证／OAuth 合同保持，发现／测试与来源保存分开，不新增第二 writer 或兼容入口。
  - `model-final-protocol-check-02` 与 `model-final-types-02` 校验当前生成绑定和 212 份实际工作台源码；原 proto／DTO 声明覆盖完整引用、来源基线、凭据观察、必填密钥 ID、临时 scope 及 SSE 三类事件。三协议真实无认证和 OAuth 配置验证分别见 `model-owned-client-02`、`model-configuration-contracts-04`，保存仍只有原 source writer。

## 2. 稳定键结构与唯一配置解析

- [x] 2.1 将 Workspace schema、内置／开发模板及相关 fixture 统一到 Provider／Model 稳定键对象，支持可选显示名、服务端模型名、`defaults`／`overrides` 及可继承的 streaming，拒绝重复身份字段与旧平铺格式。
  - 当前 schema、正式模板和加载的配置 fixture 均使用稳定对象键；真实配置／Agent／Session 检查及模型设置页面已覆盖显示名与服务端名称分离、继承和 schema 拒绝。全仓剩余数组仅为显式 legacy migration 输入及未作为现行配置加载的项目样本 `default_test_workspace/boxteam.json`，不保留生产数组读取。团队测试只平移配置准备并保留延期 TODO，不恢复团队功能验收。
- [x] 2.2 在原配置解析入口完成先来源合并再默认／覆盖解析；两阶段复用 supports_reasoning 完整块替换规则，保留其余单字段继承、整块恢复继承及显式等值覆盖。完善原 source document／inspect 的两类来源信息，协议切换不混入旧线格式，不在前端或 adapter 重复合并或新增策略框架。
  - `out/tests/temp/2026/11/17/provider_model_execution/artifacts/` 中 `model-origin-unit-03`（67 项）、`model-origin-backend-03`（27 项）及 `model-origin-browser-04`（真实 Workbench 1 项）已通过；覆盖参数别名原文定位、请求头大小写、等值来源更新、active／pending 来源重启及草稿预检零保存。页面仅展示后端回执。
- [x] 2.3 将请求选项统一为 `request_options.parameters`，沿原 adapter 映射别名后按优先级合并，最终处理 null 省略，保留零／false；网络前拒绝 extra_body 与正式／受保护字段重复及同层别名冲突，保留未知扩展合法 null，迁齐所有现有 adapter，不建参数注册中心。
  - `model-configuration-contracts-04` 的 39 项及源码未变的 `model-origin-unit-03` 覆盖别名、优先级和拒绝；三协议真实 SDK／正式 Job 的 wire 断言见 `model-owned-client-02`，页面至真实 TCP 的零值、null／false 及缺省输出限制见 `discovery-active-browser-04`。
- [x] 2.4 更新原配置生效策略、changed paths、预检及完整 candidate 的当前引用检查；保持后续 Job 生效、混合候选 pending 和原共享来源 fan-out，不加全历史扫描。
  - `model-selection-policy-final-02` 的 14 项和源码未变的 `model-origin-backend-03` 覆盖当前选择、next_job／混合候选、预检及保存后当前默认引用再次检查；真实 active／pending 切换另由 `discovery-active-browser-04` 验证。共享来源继续由各 Workspace 的原 watcher／source owner 消费，只汇总原 fan-out 结果。

## 3. 结构化模型引用贯穿业务链

- [x] 3.1 更新原 Protobuf／共享 schema 中的 ModelReference 和受影响公开 DTO，重新生成各正式客户端；完成真实跨层类型检查，不以额外字符串解析器保留旧 Provider 选择。
  - 当前生成产物一致性、真实类型检查及完整 Workbench 构建分别由 `model-final-protocol-check-02`、`model-final-types-02`、`model-final-build-02` 通过；后两者都采用实际 sourceTransform `fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca`。协议保留退役字段号／名称，不保留 Provider-only 解码入口。
- [x] 3.2 将 Agent primary／fallback、原默认选择解析及 SessionDefaults 的读写迁齐；Session／执行准入按完整引用保留当前 Agent 候选限制，显式选 fallback 置首但其余顺序不变。保留默认 Agent、切换 Agent 和新会话生效语义，不增加 Provider 首模型默认值或扩大允许集合。
  - 同一证据目录的 `model-contract-tests-01`（ConfigService 133 项）和 `model-contract-backend-01` 中 11 项模型选择检查通过；`model-contract-backend-02` 的定义删除及双工作区真实执行 2 项通过。原共享回放计划已改从 Session catalog 读取完整选择，不再猜默认 Provider。
- [x] 3.3 将 Session 创建／更新／恢复、导航摘要及 Composer 当前模型选择迁齐，按原持久 owner 保存完整引用；已删除或被移出候选的旧绑定保留身份并要求明确选择，不自动改绑或批量重写历史。
  - `model-names-backend-integration-01` 的真实配置／catalog 检查覆盖创建、选择、重读与删除模型后保留原绑定，显式重选才写入新引用；`model-picker-scale-02` 覆盖六会话选择及刷新恢复，双 Gateway 的原 owner 定位见第 5.3 项。Session 原 catalog 保存 `current_model_json`，导航与 Composer 消费完整 `current_model`，未增加自动改绑或历史重写。
- [x] 3.4 将 Job、子 Agent、持续任务／模型 judge、工具测试、诊断和回放消费者迁齐；same_source 沿原 profile／metadata 核完整模型、连接与认证来源，缺来源不补猜，不扫描改写历史。测试范围改为 `model_refs`，保留原重复次数、run_id 与后端空范围语义。
  - 执行队列构造与投影消费者已迁到 `model`／`boxteam_session_model`；全仓 AST 检查无退役 `ExecutionQueueInput(provider_id=...)` 或模型字符串位置参数，旧 v5 解码仍由原迁移 owner 保留。`queue-model-service-02` 的 17 项真实存储检查通过，包含完整模型冷读、无显式选择的 v5 导入、Provider-only 旧输入拒绝且原件／队列不变；原 23→28 有序迁移保留。`queue-model-unit-02` 中不受后续修改影响的 45 项及 `queue-model-unit-03` 的 19 项通过，命令、指纹和原始输出位于同一 `model_configuration_closure/artifacts/`。附件装具已注入现有资源占用账本、使用真实领取准入，并修正故障包装器漏返回正式接纳记录的问题；`config-consumer-baseline-02` 中附件文件全部 19 项通过，该次运行另有两项配置基线表示比较失败，不能把整个运行记为通过。
  - Embedding 工厂、网页排序、来源结果与测试统一消费完整模型身份，移除旧 `id`／`from_provider` 链，未解析秘密拒绝进入传输。`embedding-consumers-unit-04` 的 38 项检查通过，包含真实工厂至计算边界、环境／Gateway 按需解析、Model 覆盖及无关模型秘密不解析；补齐其它适配族原非空凭据边界后，`embedding-runtime-unit-final-01` 的 12 项复验通过。OpenAI 路径复用现有 SDK client 构造与官方 Authorization 省略方式，实际 SDK 请求确认显式无认证不回退进程 Key，选定 Key 和自定义请求头保持；HTTP send 被替换，此证据不代称真实 TCP 验收。其它 Embedding 适配族的无认证支持保留代码 TODO，不放宽原凭据要求。配置来源 noop 检查按持久／公开共同字段精确核基线，`config-consumer-baseline-03` 的两项通过；同源码先前通过的 45 项仍可复用。
  - Debug 公共回放装具及七项 Agent 场景配置已迁到稳定键／完整引用；三个 Web 驱动和八个 HTTP 场景同步改为提交并断言完整 `current_model`，MCP 配置替换回放调用原完整模型入口。Debug 真实场景的 `model-debug-consumers-01/02` 均在插件绑定准备阶段返回 409，第二次保留了精确来源 CAS 错误正文；尚未进入 Agent 执行，不记为通过。`embedding-consumers-static-03`、`embedding-final-static-01` 与三个 Web 驱动语法检查通过；两个旧场景的 ASYNC220 已在 HEAD 原文复现，证据见 `model-session-drivers-static-baseline-01`，未为取绿扩改进程生命周期。后续真实 Debug 产品入口由 `model-debug-http-04` 通过，403 与来源重试由 `model-reload-http-05` 通过；该批曾失败的其他项不改写为通过。
  - 扩大恢复组 `queue-model-recovery-01` 在 180 秒外部期限到期，退出码 124，过程中已有装具错误且未取得最终汇总；此运行只保留诊断证据，不记为验收通过，也不在缺新证据时重复运行同组。
  - 两个 rollout 生成入口已迁到完整模型引用，真实模型生成器按选定候选批量准备凭据并沿原执行作用域构造模型；移除仅 Provider ID 的命令参数。`model-fixture-migration-02` 的 9 项检查通过，覆盖同 Provider 双 Model、跨协议切换、工具循环、真实 canonical owner／admission 及封存请求等于实际 HTTP 正文。后续剩余调用方清理及全仓退役字段检查见第 8.2 项，未恢复开发中间格式。
  - `model-dispatch-consumers-02` 的 79 项检查通过，消息派发使用完整 `boxteam_session_model`，Session 创建测试沿原 catalog 使用 `current_model_json`，子线程与 owner 创建不保留 Provider-only 替身；简化消息替身后 `model-message-consumers-03` 的 18 项复验通过。静态分析通过；本轮 `model-consumer-evidence-reuse-04.json` 再核生成器 9 项和消息 18 项对应源指纹未变，79 项原批次仅消息测试后来简化并由这 18 项补验，不重复长跑原批。
  - `out/tests/temp/2026/11/17/model_configuration_closure/artifacts/tool-test-model-web-03` 经真实 Gateway／受管 Workspace／Workbench 验证原工具测试入口：明确提交 `model_refs`、保留两次重复、20 个样本及原 2 通过／18 失败，刷新恢复同一 run；同 Provider 的非候选 Model 不进入可选范围，持久结果逐样本保留完整引用。业务模型边界复用原 model_stream，配置凭据使用显式环境引用，正式页面复用同批构建；未新增兼容链。
  - `model-session-consumers-01` 的 6 项会话活动／生成器检查通过，后续 fork 在装具准备阶段因未通过矩阵启动看门狗而拒绝；保留原始失败，随后沿正式矩阵仅补验未执行的两个 fork／回报场景，`model-session-fork-02` 的 2 项检查通过。`model-consumers-static-01` 通过；Launcher 交接配置已迁完整引用，本轮仅完成静态核对，未重新执行完整交接验收。旧自动热迁移测试已改为 `test_config_reload_session_retry.py`：真实 Gateway／Backend 产生失败历史，原 watcher 生效当前模型配置后公开重试，逐调用核实际模型及 owner；`model-reload-http-05` 通过，不恢复开发兼容。本轮最终检索及 `model-consumer-closure-static-05` 核对完成；持续任务的 model judge 继续沿原 Thread 消息派发消费 Session 模型，不新增旁路配置。尚未复跑的交接与完整业务场景保留上述范围说明，整体产品验收由第 7 节单独记录。

## 4. Gateway 密钥归属与执行使用

- [x] 4.1 移除普通模型配置中的 API Key 字面量写入；复用原 Gateway 密钥创建／选择／更新入口，Workspace 只存正式引用，保留目标 Workspace 的显式环境注入和原 OAuth owner。
  - 第 4.2／4.3 的真实 Gateway、配置 writer 和 Workbench 闭环保持；当前 `model-configuration-contracts-04` 再核字面量在 snapshot 首写前拒绝及 OAuth，`model-origin-backend-03` 同时核普通预检／保存拒绝明文且原件不变。实际环境与 vault 凭据选择、轮换和缺失的发送边界见 `model-owned-client-rotation-03`，普通来源不新增秘密存储。
- [x] 4.2 在原执行准备边界按实际 primary／fallback 需求去重批量 resolve，凭据 revision 接原 runtime generation；验证轮换供后续 Job 使用、在途快照保持，缺失不复用旧值或换来源。
  - `secret-execution-unit-01` 的 34 项执行服务检查通过；迁齐旧 `providers`／Provider-only 调用，直接验证同代复用、轮换等待原 lease 排空及删除后拒绝新准入。`secret-execution-http-02` 的 21 项真实 HTTP／SDK 检查通过：真实 Gateway 密钥 writer 和配置 CAS 建立状态，暂停旧 Job 首个模型响应后轮换，新 Thread 按新值完成工具循环，旧 Job 后续请求仍用旧值；同 Thread 后续执行换代，删除后零模型请求。每 Job 对重复 primary／fallback 引用只 resolve 一次，未使用失效引用及普通定义读取不解密。Workspace 经过真实 ASGI 入口，上游为真实 TCP 协议替身，不能代称独立后端进程验收。
- [x] 4.3 创建密钥改为客户端预定必填 ID，原 vault 原子拒绝重复 ID；响应丢失只读原目标元数据恢复，更新／删除未知不凭当前状态假报成功，不复用配置 submission key 或新增操作账本。临时 Key 按 scope 在内存替换后再解析，被 Model 覆盖或绑定不符时网络前拒绝；普通读取不全解密，取消零秘密持久化。
  - 同一证据目录的 `secret-owner-unit-01`（22 项）、`secret-relay-unit-02`（5 项强化回执断言）与 `secret-settings-unit-02`（43 项）通过。创建前沿原 Workbench storage 等待路由／ID 落盘；明确 409 不记为未知，远端写入后的路由／回执失效返回 502 并保留未知结果。`secret-probe-contract-01` 的 8 项真实 HTTP／配置入口检查通过，覆盖临时输入替换旧引用、实际 revision 和被 Model 覆盖时零上游请求。
  - `secret-recovery-browser-02` 经真实 Gateway／Workbench 通过（`run-efd316880a23`）：真实创建落盘后丢回执，刷新仅按原 ID 查询且只有一次 POST；更新后另一窗口沿真实 CAS 写入 revision 3，原操作仍未知；删除后当前缺失也仍未知。保留密钥复用、配置绑定冲突、密码不进来源／展示存储及 Gateway 重启后的解密断言。类型检查和完整 Web 构建通过；设置来源标题仅在用户查看来源位置时建立操作生命周期，修复先于 Gateway 初始化恢复设置页的错误。

## 5. 原生设置与模型选择体验

- [x] 5.1 在原 Settings 模型入口实现 Provider 分组与模型列表／详情，提供最小协议模板、稳定键生成、新建、编辑、复制参数预设、移除本层及准确来源；高级字段按需展开，复用原控件和菜单，添加中文／英文文案，不新增 Variants 身份。
  - `integration-web-configuration-entities` 的 `run-deeca48ee23c` 经真实 Gateway／Workbench 通过，包含创建／编辑／删除与恢复继承；源 JSONC 与表单共用草稿，原生确认定位实际所选定义。已修正异步目录／空搜索刷新丢失当前 Provider、Model 或 Agent 选择的问题，真实类型检查及完整 Web 构建通过。远端及完整规模验收仍归第 7 节。
- [x] 5.2 为模型参数及 streaming 提供继承／显式覆盖／按 schema 不发送的交互，能力、容量和请求参数分别展示；协议切换在同一草稿明确替换完整线格式块，解释空块与整块恢复继承。最终地址复用后端规则，原文、dirty、撤销、预检、保存共用原来源文本模型。
  - `out/tests/temp/2026/11/17/model_configuration_closure/artifacts/settings-unit-01` 的 24 项组件检查、`settings-types-01` 和 `settings-build-01` 通过。`settings-browser-04` 经真实 Gateway／Workspace／Workbench 通过：参数零值／null、同草稿最终值与来源、凭据显式无认证及精确恢复继承、协议／streaming 覆盖与独立恢复、reasoning 空块和能力冲突的 422 均按原 owner 验证；实际流式／非流式及取消继续完成，草稿零保存。前面三次失败原件保留，分别定位测试标签、能力声明冲突及合法空父对象，不放宽产品校验。目录规模、跨目标及总延迟仍由第 7 节验收。
- [x] 5.3 将聊天模型选择器与上下文配置动作接到完整 ModelReference，支持聚焦搜索、Provider 分组、当前／最近／收藏／隐藏及准确设置定位；Settings 全目录与 Composer 当前 Agent 候选分开，偏好显示与候选取交集。偏好按目标和模型身份隔离，隐藏当前项不改会话，快捷枚举只编辑同一来源草稿，全局入口默认本地，对象入口绑定原 owner。
  - `model-picker-scale-02` 经真实 Gateway／Workspace／Workbench 通过显示名、服务端名称和完整稳定引用搜索；六会话独立选择、当前项隐藏、收藏／最近／刷新恢复、Agent 候选约束、新会话默认值及设置精确定位继续通过。目录只投影同一原快照的显示名，执行选项不加入展示字段；本轮未验证真实远端同名模型偏好隔离。
  - `model-target-browser-02` 经双真实 Gateway／Backend 和完整 Workbench 通过：两端使用同一 Workspace UUID、Provider／Model 名及 secret ID，收藏／隐藏在刷新后仍隔离；模型行设置精确定位原目标，实际 HTTP 推理分别采用 B／A 各自凭据，request_id 只进入对应 Backend。零目录探测、来源未被展示操作修改、460px Composer 可编辑。SSH 配对执行与隧道在装具中替换为本机正式 CLI／loopback，此证据属于双 Gateway 集成，不代称真实 SSH 验收。
  - 当前构建的 `model-probe-budget-browser-05` 完整通过，六会话模型选择、Agent 候选交集、最近／收藏／隐藏、刷新恢复及精确设置定位全部复验；普通展示阶段零探测仍单独断言，之后的 21 次显式发现和 21 次显式推理逐请求核对真实认证与模型，不把显式测试混记为自动探测。参数枚举及字段编辑继续只写同一来源草稿；极端拖拽与整体工作台验收仍由 7.3 保留。
- [x] 5.4 在凭据控件区分选已有、创建、环境引用、改引用和更新共享值，显示临时 Key 的 Provider／Model 绑定；原展示存储只保留创建恢复所需路由、ID 与操作种类，密钥写入未知／当前状态／引用保存分开，不回读明文或另存秘密。
  - 第 4.3 项的真实页面与设置入口证据已覆盖凭据管理闭环；`secret-probe-ui-01` 的 11 项当前源码检查通过，包含临时 Provider／Model 绑定、换模型／取消清空密码、密钥回执使旧测试过期及零自动重测。执行轮换证据见第 4.2、7.4 项。
- [x] 5.5 接入当前连接完成编辑后一次自动发现、关闭自动获取、显式刷新／取消、过期及不完整状态，提供搜索、多选、匹配项全选与目录差异；用户选入才写原草稿，手填始终可用，打开已有页面／保存不触发探测。
  - `discovery-boundaries-browser-03` 经真实 Gateway／Backend／Workbench 通过：连接编辑一次发现、纯焦点与参数编辑零探测、关闭自动获取后零自动请求、搜索和匹配项全选只改原草稿；真实目录响应受控延迟后不覆盖新连接，5001 项单页明确返回不完整且保留原模型，手填继续可用。延迟场景仅暂停真实 Gateway 回执，不代称上游取消证明；取消组件检查和真实模型断连证据分别保留。
- [x] 5.6 接入草稿／active 快速测试及流式／非流式分项结果、短输出、真实耗时、取消与选中批量停止；区分编辑计数、来源／active 与实际凭据观察版本，已知变化标过期，冲突不自动重基，显式版本刷新零推理／零后台轮询。保留错误字段定位，应用测试模式只改原草稿，深入工具测试沿原入口。
  - 同一页面验收覆盖流式提前可见、真实非流式、密钥观察 revision 及显式刷新、首内容前两种模式取消、三项在途批量停止和超过 15 秒首内容等待。另一用户来源经正式 CAS 保存为 pending 后，旧草稿基线返回 409 且零上游；active 不含 pending header，显式刷新后 draft 才使用新来源，本页编辑计数不变。总计 20 个上游请求、13 个推理请求，工作区来源原文不变；命令、指纹、原始输出及退出码见 `model_configuration_closure/artifacts/discovery-boundaries-browser-03`。内部处理和内容可见预算仍由 7.7 独立验收。

## 6. 目录发现与快速测试执行链

- [x] 6.1 在原配置 API／协议增加 discover JSON 与 test POST SSE，统一 active／draft、来源原文和精确目标；draft 按已保存来源（可含 pending）替换选定草稿，一次核对预期来源基线或 active 版本，过期零上游请求。回执／start／result 携同一实际观察版本，原带凭据 HTTP／SSE 原语处理同一 request_id、发头前后错误与无终态 EOF；禁止恢复型订阅或重放，不保存 candidate 或执行无关完整审计。
- [x] 6.2 实现所支持协议的上游目录读取、累计期限／大小／数量边界及协议明示分页，返回来源／完整性／真实错误；与公共参考元数据分开，不猜能力、不改模型配置或增加周期刷新。
- [x] 6.3 将 `api_mode.streaming` 接入所有现有 adapter、正式执行、测试及 Saver profile／wire 参数；统一 finalizer、sealed request_hash preimage 与真实发送，完成必要 projector 版本更新。补齐非流式消息／reasoning／工具调用／usage，拒绝重复 stream 参数；不剔除实际正文字段、绕过 sealed binding 或放宽 source-mismatch 校验，不以聚合流或未接入 Agent 字段假报实现。
- [x] 6.4 实现当前单模型的轻量文本调用和明确模式选择，共用正式构造／凭据／adapter，按配置发参数、零自动重试与 fallback、不启动 Agent；输出增量及正常结束必须经过真实 Gateway 至页面，不只给最终 JSON 摘要。
- [x] 6.5 发现使用15秒服务端预算／20秒客户端完整期限；测试使用默认60秒绝对期限、客户端额外5秒收尾且不被心跳重置，及时透传头和 start。取消沿 fetch／reader、各 Gateway、Backend 及上游连接结清，含首内容前及非流式等待；批量最多3个在途、停止不派发剩余项，预览64KiB，不新增 cancel API 或持久队列。

## 7. 集中验证真实行为与总延迟

- [x] 7.1 运行短而有判别力的配置／协议／引用定向检查：跨层小覆盖、两种继承顺序、跨协议完整线格式替换、显式空块与恢复继承、错误协议块拒绝、等值覆盖、删除露出下层、主模型与 fallback 同 Provider，以及缺失／歧义引用拒绝；测试始终带进程外超时和内存保护。
  - 本轮 `model-configuration-contracts-04` 39 项、`model-selection-policy-final-02` 14 项通过；核对 `model-origin-unit-03` 67 项和 `model-origin-backend-03` 27 项的前后及当前源码指纹全部一致，复用其两阶段继承、线格式整块替换、等值覆盖、来源 CRUD／CAS 与缺失引用证据，不重复运行。各批均保留外部期限、内存保护及原始输出。
- [x] 7.2 经实际 Gateway／Workspace／adapter 核最终协议、模型名、参数优先级、末次 null 省略与零值；验证 extra_body／别名冲突网络前拒绝及未知扩展 null 保留。同 Provider 不同 Model／连接与 fallback 的私有密文不跨源，缺来源历史保持证据，不以直接调用 helper 代替真实入口。
  - `model-gateway-protocol-eof-browser-02` 的 37 项通过：真实 Gateway／独立 Workspace／adapter 三协议正文核对 Provider 默认与 Model 覆盖的优先级、零值、末次 null 省略和扩展 false／null；两个冲突请求均 422，总上游请求数精确核对为 26（推理 18），拒绝未触网。归属与缺来源的既有正式 Job／双 Gateway 证据见 3.4、4.2、5.3、7.6，不改写历史来源。严格路径装具揭露 Anthropic 测试输入的重复 `/v1`，已统一修正输入，未新增错误路径兼容；第 01 批失败原件保留。
- [x] 7.3 执行真实 Workbench 浏览器场景：无会话配置服务并添加模型、Settings／JSONC 同草稿与 CAS 冲突、恢复继承、六 Thread 选择隔离、远端同名模型和密钥归属、最近／收藏／隐藏、窄宽度及极端拖拽；验证全目录与 Agent 候选分离、同 Provider 非候选零执行、偏好取交集、切换 Agent 与显式 fallback 重排。测试使用规定的隔离工作区和产物目录。
  - 同名模型／密钥的双 Gateway 页面验收见第 5.3 项，正式结果位于 `out/tests/integration/clients/web/test_model_target_isolation/runs/run-0d7d41f80344/nodes/node-f53437207664/artifacts/`，命令、源码指纹与退出码位于同一 `model_configuration_closure/artifacts/model-target-browser-02`。第 01 次因测试未授予联邦管理权限而返回 403；已改为由隔离 B 的正式用户 JSONC 显式授权 A，并等待真实 watcher 提交，没有放宽产品权限。其余页面验收见后续布局批次。
  - `model-layout-browser-05` 经真实 Gateway／Workspace／Workbench 通过，正式结果为 `run-03dc909d59df` 的 `result.json` 和六份布局截图：明暗主题、原生分隔线两端、320px 编辑器组、460×650 窗口及尺寸恢复均保持地址输入完整和底部动作可达，标题不换行压住正文并保留完整悬停信息。修复仅调整原 Settings 最小宽度及标题收缩，沿原 SplitView、TOC 和工具栏；六 Thread、候选与偏好、目录规模及 42 次显式上游请求仍完整通过。无会话创建、同草稿／CAS／恢复继承沿 5.1、5.2、7.4、7.5 的真实页面证据；非候选拒绝和显式 fallback 顺序的 `model-selection-policy-final-02` 源码指纹仍完全一致。布局第 01／02 批的装具错误和裁切原件保留，第 03 批通过后截图发现窄标题问题；第 04 批在布局前的候选握手被外部超时终止，已给该握手增加原期限内的有界检查和阶段记录，不将它归因于标题修复或宣称其不再可能发生。
- [x] 7.4 验证共享密钥复用、轮换、缺失、删除、创建成功／绑定失败及创建响应丢失按 ID 只读恢复；更新／删除未知遇并发不假报成功，零自动重发。临时 Key 替换失效旧引用不解析旧值，Model 覆盖／绑定不符零上游请求；测试中轮换保留原观察 revision，回执或显式刷新后标过期，临时 Key 变化废弃旧结果。取消后 vault／配置／缓存／日志无秘密，普通读取不全解密或轮询。
  - 第 4.2、4.3、5.4 项的真实执行、Workbench 创建／更新／删除及临时凭据检查共同覆盖；执行验收另核工作区 `.boxteam` 持久内容不含轮换前后正文。`secret-execution-http-03` 对最终从实际 HTTP 认证观测生成的非秘密 revision 产物复验通过；证据保留在同一目录的 `secret-execution-*`、`secret-recovery-browser-02`、`secret-probe-contract-01` 和 `secret-probe-ui-01`，未新增秘密缓存、自动重放或轮询。
- [x] 7.5 经浏览器验证连接编辑只自动发现一次、显式刷新、关闭自动获取、迟到响应隔离、目录不完整不删模型及手填；本页计数不变而另一来源已保存 pending／active 时旧基线零上游调用，重读后明确重测，回执带真实采用版本。记录请求数，普通打开／焦点变化／保存零探测，草稿测试零保存且 active／pending 不混用。
  - `discovery-active-browser-04` 经真实 Gateway／Workspace／Workbench 通过，保留原目录、取消和 pending 场景，并补齐另一来源直接 active：原正式预检／CAS 写入后，旧页面发现和推理分别 409；观察版本刷新零发现／推理且本页计数不变，显式重测回执与目录采用同一来源基线。共 22 个真实上游请求、14 个推理，精确核对 active／pending 请求头、缺省输出限制及工作区来源原文不变；未绕过原 writer 或放宽错误断言。
- [x] 7.6 门控上游验证真实 POST SSE start／delta／result 顺序、同一观察版本、提前展示、单片段成功、发头前后错误及无终态 EOF；首内容延迟超过15秒仍按测试期限完成。验证首内容前／非流式取消及批量停止的真实断连／请求数、零重放；每种现有协议的拒绝流式 fixture 同时验证快速测试与正式 Job 的真实封存准入、wire preimage／stream 字段及消息／reasoning／工具／usage，不以测试豁免代替正式执行。
  - `model-probe-http-final-01` 的 42 项通过，三协议快速测试与正式 Job 共用同一真实 HTTP 上游：拒绝流式只发一次，显式非流式成功，Job 工具循环及封存 wire/hash 保持。`model-gateway-protocol-eof-browser-02` 的 37 项复验完整页面提前展示、首内容超过 15 秒、取消、批量停止及三协议请求；新增 EOF 场景只截掉一次真实 Gateway 正文的 result，页面明确失败且零 POST 重放，这是传输截断替身，不冒称真实服务故障。Job 证据为真实 ASGI 业务入口与 TCP 上游，独立 Backend／Gateway 与浏览器接线由本轮页面场景验证。
  - `model-owned-client-02` 的 31 项通过，覆盖三协议同步／异步真实 TCP、完整消息、私有来源、取消，以及三协议正式 Job 工具循环／封存 preimage/hash；新增显式无认证在进程另有 OpenAI／Anthropic Key 时仍不发送认证头。唯一装具错误是扩充认证头观察结构后漏迁轮换用例的 fixture 名，已迁齐该消费者；仅补验的 `model-owned-client-rotation-03` 通过，原失败证据保留，不将第 02 批整批写成通过。回放共享／免代理／匿名实例共用唯一 owner，关闭实例后下一实例继续命中；Debug 产品入口的 `model-debug-http-04` 通过，HTTP 403 的 `model-reload-http-05` 通过，原 Provider 身份、真实上游状态及完整诊断仍被核验。SSE 接线见本项新增页面批次，页面总预算仍由 7.7 独立验收。
- [x] 7.7 用三层来源、20×10模型与六编辑器 fixture 记录至少20个暖样本及首次读取，核完整 API p95≤500ms、max≤1500ms、首次≤3000ms、设置和 Composer 可操作每次≤2000ms；探测／测试的软件内部沿暖预算、外部等待单列，内容到可见≤250ms，保留规模／原始时长／request_id／源码指纹／命令／退出码，不删慢样本取绿。
  - `out/tests/temp/2026/11/17/model_configuration_closure/artifacts/model-picker-scale-02` 通过；正式结果为 `out/tests/integration/clients/web/test_model_picker/runs/run-91abee17fddf/nodes/node-50a73b15420c/artifacts/result.json`。真实三层、20×10目录及六编辑器，各 20 个暖样本：Agent／来源目录／完整来源正文 p95 为 41.2／51.8／152.9ms，max 为 42.6／62.0／411.7ms；首次读取均≤135ms，最大正文 297735 字节；Composer 最大可编辑耗时 1095ms，设置 1367ms。共享命名密钥由正式 Gateway 创建，来源未被展示操作修改，零模型推理／目录探测；沿原 Perf／浏览器观察保留全部样本。探测软件内部预算与内容到可见的独立量化仍未完成，本项不提前勾选。
  - `model-probe-budget-browser-05` 通过，正式原件为 `run-d8dd4dd1aa90` 的 `result.json`／`probe-samples.json`：同一规模的 20 个暖发现／测试样本 p95 为 114.6／129.4ms，max 为 120.0／525.1ms；21 段正文使用同一浏览器时钟，按真实 request_id 对应原 Settings 面板，原始字节收到至 DOM 布局可见并经过两帧的最大值 42.6ms。没有以不同进程时间戳相减或过滤慢样本；回环上游立即响应，既有慢首内容 16 秒场景单列。普通读取、设置和 Composer 预算继续通过。首次模型测试仍为 3954.3ms，代码保留冷启动优化 TODO，本项不因暖检查通过而勾选；前四次观测装具失败与原始输出均保留。
  - 最终 `model-layout-browser-05` 再核暖发现／测试 p95 为 135.0／142.3ms、max 为 137.0／161.2ms；普通读取和内容可见预算继续通过。首次模型测试 3749.1ms，原样保留，本项继续未完成。
  - `model-cold-no-wait-browser-01` 经相同完整 Gateway／Workspace／Workbench 场景通过（1 passed／76.25秒），正式原件为 `run-d2d9197fa4da` 的 `result.json`、42份请求样本及六份布局截图。首次模型测试2996.9ms、首次发现132.5ms，20个暖发现／测试p95为146.6／155.1ms、max为150.3／201.3ms；普通API、设置／Composer及内容可见预算均通过，无页面错误。剖析确认首次SDK加载链中的模型元数据前台等待可删除：沿原缓存／内置目录立即返回，后台继续刷新，后续请求才采用；移除该等待参数及重复锁入口，未增加预热、模型请求、重试或缓存。门控与真实冷进程HTTP共15项通过，Ruff／JS语法／diff检查通过，前端产品源码及正式构建指纹未变。准确命令、前后源码指纹、退出码与剖析原件见 `out/tests/temp/2026/11/17/model_configuration_closure/artifacts/model-{metadata-no-wait-tests-01,cold-no-wait-browser-01,cold-profile-normal-01,cold-profile-local-only-01}.{json,log}`。本次首次样本距3秒仅约3ms，SDK类型导入仍是性能长尾，不据单样本承诺繁忙机器稳定达标；保留之前的慢样本，未重复选快值。
- [x] 7.8 对完整受影响源码集中执行静态分析、真实前端类型检查、协议检查及 `bun run --cwd src/clients/web build`；复用同批已完成构建，未通过先归因，不默认跑全仓长测或改环境。
  - 本轮显示名贯穿 Agent DTO、原生 Quick Pick 与 Composer；`model-names-unit-01`（8 项）、`model-names-backend-unit-01`（13 项）、`model-names-backend-integration-01`（11 项）、`model-names-types-01`、`model-names-protocol-check-01` 和 `model-names-build-01` 均通过。对应命令、源码指纹和退出码位于同一 `model_configuration_closure/artifacts/`；这证明当前切片，不替代尚未迁齐的其他消费者检查。
  - 当前最终源码的 `model-final-protocol-check-02`、`model-final-types-02` 及 `model-final-build-02` 通过；类型与完整构建使用同一实际 sourceTransform `fc8954c90fe6dea1f4c411dd73fdfe5f1913b5ca`。72 文件后端静态检查 `model-final-backend-static-02` 退出 1，49 条诊断逐条按文件、规则及对应源语句与 HEAD 对照完全相同，证据为 `model-final-backend-static-line-baseline-03.json`；不把既存诊断写成检查全绿，也未关闭规则或扩大无关重构。已迁消费者的 28 文件定向 Ruff 检查仍为通过。
  - 布局收口后的 `model-layout-types-02` 和 `model-layout-build-02` 再次通过（212 份实际转换源码及完整 Web／Workbench 产物）；最终页面由该新产物提供。`model-final-probe-static-04`、`model-layout-syntax-04` 通过；只改测试等待后未重复无关构建。

## 8. 删除旧链与交付复审

- [x] 8.1 对已核验发布契约确需的转换，沿原配置／数据库 owner 实现最小必要迁移及备份／失败恢复测试；无支持基线不制造开发迁移，用户数据和既有迁移保持，报告未验证升级范围。
  - 2026-11-17 再核 `bun run release:policy list` 仍为 `[]`，用户已裁定按首次发布处理。本变更未增加开发中间态转换，原配置／SQL 迁移保留；隔离场景直接使用当前 Provider／Model 及会话结构，正式旧版模型配置／凭据升级不在已验证范围内。
- [x] 8.2 在全部消费者和测试迁齐后删除旧平铺 Provider 运行读写、仅 Provider ID 的模型选择、旧请求覆盖字段及数组密钥占位专用特例；全仓检查无悬空调用，不删除其他实体所需机制。
  - 当前 `app/src/configs/tests/docs/proto` 检索仅保留正式协议 reserved 名和固定旧源码生成器中的旧模型字段；数组旧输入仅在上面的具名历史／项目样本中。原 no-proxy 专用构造器及旧热迁移测试入口已物理下线，调用方统一到实例 client 和当前配置链；未保留兼容 alias。全部相关 28 文件静态检查归 `model-consumer-closure-static-05`；未运行的业务／跨平台验收仍由对应任务保留，不以清理完成替代验收。
- [x] 8.3 按最小充分实现复审，删除重复解析、冗余包装、第二份参数状态、兼容分支、越界自动探测与额外持久化；核对已实施 spec、实际验证和剩余限制，产品验收前不提前勾选未完成任务或归档。
  - 复核原来源合并／ModelReference、凭据作用域、正式构造／finalizer、一次性探测和原 Settings writer：参数与来源正文仍由各唯一 owner 持有，列表不导入 SDK 或解密全目录；临时观察只在原表单内存，未增加自动重试、轮询、历史扫描或额外账本。旧模型选择字段检索仅剩 Responses 推理摘要项身份的无关同名字段。布局直接修改同一原生约束，不增加尺寸 observer 或平行状态；严格 HTTP 路径装具不再手填协议掩盖错误目标。7.7 当前首次样本的性能余量风险、既有 Ruff 诊断和双 Gateway 的 SSH 替身边界继续明确保留，本变更仍未归档。
  - 2026-11-19 收口复验：核对旧收据发现 `ModelConnectionProbe`、Settings 实体及页面驱动指纹已有变化，因此复用当前源码正式构建重跑 `test_model_picker.py`，1 passed／75.27秒，原件 `out/tests/integration/clients/web/test_model_picker/runs/run-908a72e35035/nodes/node-50a73b15420c/artifacts/`，完整输出和精确命令见 `out/tests/temp/2026/11/19/context_history_markers/artifacts/provider-current-page{.log,-receipt.json}`。六会话隔离、三层20×10目录、偏好、设置定位、42次显式目录／推理请求及布局场景保持；发现／测试20个暖样本p95为120.9／142.9ms，首次测试2991.2ms仍贴近3秒门限，不宣称冷启动余量充足。当前Web真实类型检查及完整79扩展构建退出0；本轮仅调整规范，无额外产品修改。主规范同步稳定键、结构化模型引用、Gateway密钥及临时POST SSE合同，移除数组、明文配置与旧Provider选择要求，保留未涉及的Skill合同及原验收限制后归档。
