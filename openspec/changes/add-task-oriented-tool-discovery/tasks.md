## 1. 调用链与集成边界核对

- [x] 1.1 全仓检索完整 schema、tool.args 指纹、目录发布、初始／当前目标解析及其直接和间接测试，记录需要同批迁移的权威写入与消费者。
- [x] 1.2 核对进行中上下文、itemized、多工作区及日志 change 的实际落地状态，按 design 的集成边界定位唯一 owner；配置 delta 合并不得覆盖日志变更。
- [x] 1.3 确认新模块归属、公开 typed 合同、现有持久存储与路径解析入口；新源码子目录补齐四部分 AGENTS.md，不新增万能 registry／独立 writer。

## 2. 完整 schema 与校验纵向迁移

- [x] 2.1 统一 MCP 和本地扩展公开 schema 导出、规范化及方言验证；目录、binding、hash、指引和校验消费同一完整契约。
- [x] 2.2 将目录 revision／schema hash 从 properties 摘要迁移到完整 schema，版本化受影响 binding；明确历史可读但不可恢复执行的错误路径。
- [x] 2.3 删除信封无条件拒绝额外字段及字典校验后裁剪合法参数的路径，保留本地 Pydantic 工具既有默认值／转换语义。
- [x] 2.4 用真实发现／信封入口验证 required-only、additionalProperties-only、$defs、嵌套、枚举、组合和非法字段；确认错误与原 tool_call_id 配对且非法请求未到 Server。
- [x] 2.5 验证允许附加字段的参数完整到达真实 MCP handler，保留既有本地扩展行为，移除所有旧摘要身份消费者并完成受影响静态分析。

## 3. 通用 search_tools 与上下文集成

- [x] 3.1 注册唯一 search_tools 入口和 query/tool_ids 互斥参数模型，接入现有 MCP、本地扩展与连接控制目录；不新增 MCP 专用入口或第二发现链路。
- [x] 3.2 先将每目标完整定义 facet 与父目录 members／lineage 原子发布到既有 Registry，并由 activation／Saver 冻结正文，再实现冻结快照上的确定性任务排名、精确 ID 结果、逐项错误和绑定目录／策略／查询的 cursor；禁止 live 同名补洞。
- [x] 3.3 实现候选数、最终字节／行数上限和完整 schema 保真；大定义复用 ToolOutputStore／artifact，以 inline_complete 或 needs_read＋引用／摘要／provenance 表达，不新增 schema 库或读取工具；不支持引用、物化失败及引用也超预算时明确报错。
- [x] 3.4 接入 ToolPolicyResolver 完整结果及统一 ToolMetadata，分离软 model_visible 与硬 model_visibility_locked；发现、精确查询、导航和恢复均服从 model_hidden，执行禁用不能经发现绕过；区分已知未授权与未知目录。
- [x] 3.5 将默认全量摘要改为有界能力导航，保留用户显式 model_visible 自动注入及 Skill 调用；固定 Provider description 不枚举动态目录。
- [x] 3.6 将发现结果和定义 provenance 接入既有 ToolResult／资源存储及 CSM/Saver 恢复，压缩后按当前硬隐藏策略恢复必要完整定义，已有正文不重复注入，全部 definition facet 不自动进入模型正文。
- [x] 3.7 验证首次搜索前与进程重启后的冻结正文、搜索→内联／artifact 读取→信封执行真实链路、ToolOutputMiddleware 裁剪、物化失败、分页冲突、软隐藏／硬隐藏／撤权、压缩恢复、删除提示和 turn/model_call 边界；核对真实序列化请求的 ToolSet／epoch 行为并完成静态分析。

## 4. MCP 执行 handle 与连接代际收敛

- [x] 4.1 在唯一 MCP owner 内建立每 Server 所属生命周期 task，统一创建／关闭 SDK session、完整 relist、通知及过期结果 fencing。
- [ ] 4.2 分离 catalog revision、connection generation 和 invocation lease，冻结精确执行引用；跨 execution／Turn 持有复用 ExternalResourceLeaseLedger 的 mcp_connection 与 typed resource-use，owner 内短期 handle 保留不建第二账本；同名同 schema 新连接不得接管旧调用。
- [x] 4.3 将 Agent 长期捕获工具对象及 initial/live 两段 resolver 全量迁移至 owner 解析，物理删除旧解析路径。
- [x] 4.4 实现有界的已封存调用执行资格及 HITL 等待保留，区分普通替换、禁用、断开和最新权限撤销；退役后不授予新的旧代际资格。
- [x] 4.5 统一真实 MCP 结果、isError、structuredContent、媒体／artifact、进度和取消处理；必要时整体替换 LangChain MCP 适配路径并删除旧实现。
  - 已完成结果保真边界：信封复用 BaseTool 结果与错误策略，SDK 保留业务错误／结构化内容／图片／音频，ToolNode 不再将标准媒体 stringify，canonical 与冷恢复保留原正文和 artifact；SSE 仍为派生文本。后继请求不经 AI 正文过滤，Chat 媒体按调用身份送入数据消息，Responses 封存与直接历史共用纯投影。真实本地 HTTP 的两种协议与同步／异步共 4 项验收通过，核对网络正文、请求 hash 和冷恢复；Gateway 产品验收另核并发成功／失败及 3 次封存请求。
  - 进度、取消／断线的 outcome-unknown 已按下方调用生命周期切片验收。真实外部模型的媒体协议支持仍属于第8节产品门，不以模型回放代替真实 Provider 验证。结果切片的准确命令、源码指纹、原始输出及退出码见 `out/tests/temp/mcp-result-preservation/artifacts/`。
  - 验证：结果链回归 `result-regression-018` 176 passed，Provider 投影 `provider-unified-031` 81 passed，真实 HTTP 同步／异步 `http-media-unified-032` 4 passed，Gateway 产品 `product-unified-final-035` 1 passed，公共封存／重试／前缀／跨协议回退 `wire-regression-036` 24 passed。Ruff 与严格 OpenSpec 校验通过；新增图适配模块 Pyright 零错误，受影响旧模块与完整 HEAD 基线相比无新增诊断。
- [ ] 4.6 验证同 schema 重连、旧通知、HITL 跨替换、过期 handle、执行撤权、取消、outcome-unknown 和持久 ledger／短期 handle 的持有释放；验证同 Backend 多工作区同名 Server 隔离及静态分析。

## 5. 本地连接控制与授权流程

- [x] 5.1 通过现有本地扩展注册 connections.manage 的 list/connect/status/disconnect；贯通首次需授权时的 workspace 启动、owner publication 和必需资源冻结，使同一目录保持本地控制及其它就绪 Server 可达；使用稳定工作区／Server identity 与既有工具 operation 幂等身份，不吞掉未知授权或协议错误。
- [x] 5.2 实现独立的连接／授权操作状态、查询和 deadline；选择现有领域持久化入口及受限凭据存储，不写入导航 catalog 或全局会话目录。
- [x] 5.3 为受支持 MCP HTTP 授权接入标准 SDK／协议及 state/PKCE 校验；保持 stdio env／HTTP credential reference 现有行为，未知方式明确报错。
- [x] 5.4 将 Server 授权与上游应用授权分别路由到实际 owner；复用远端已有管理工具，不假设任意 Server 有应用目录或 OAuth 能力。
- [x] 5.5 在用户授权后真实核验 token／协议初始化／完整目录，再报告 ready；断开撤销新准入并核实资源关闭，失败保留可复查阶段。
- [x] 5.6 接入产品授权入口与只读状态 API，经 Gateway 透明代理；凭据／短期授权能力不进入模型、日志、SSE 或会话快照。
- [x] 5.7 从真实工作区启动及 Agent 入口验证首次 OAuth、多个 Server 仅一个未授权、本地 connect 可达与未知目录不虚构；验证重复 connect、错误 callback、无支持流程的 401、授权成功但 initialize/relist 失败、过期、断开失败和凭据输出边界；完成静态分析。

## 6. 持久等待、精确通知与恢复

- [x] 6.1 复用既有 typed/versioned BackgroundTask handler/store 登记精确 thread 等待、deadline 和 continuation；观察等待使用原子 mailbox effect，不重复 connect／token 交换／业务副作用，引用按统一会话路径保存，不建第二事实源。
- [x] 6.2 实现登记后重读终态，在现有控制存储事务中原子提交 checkpoint、稳定 ID 邮箱消息和 checkpoint 内待投递引用；以可恢复投递阶段衔接 RuntimeSourceDispatcher，同幂等 key 重放并结算，不将 emit／collect 当作 Job 准入，不混用邮箱与 wakeup ack。
- [x] 6.3 原调用先提交配对等待结果并结束，经既有内部唤醒／队列按 after_turn 准入独立 internal execution，turn_id=None 并冻结自身新目录；按 operation 去重，重查权限／删除 fence／deadline，不改写原 Turn 或伪装 retry，只恢复仍有效意图。
- [x] 6.4 实现短 status 等待的 child scope 期限，以及长等待取消／过期；取消某 thread 等待不默认断开共享连接。
- [x] 6.5 实现进程重启对账和无法确认状态；不得恢复旧 socket、重放结果未知外部写入或伪造用户 Turn。
- [x] 6.6 验证登记前已完成、邮箱提交后 dispatcher 提交前崩溃、dispatcher 接受后投递结算前崩溃、重复 callback、两 thread 等待同连接、用户输入交错、Session 删除／取消／deadline 和进程重启后的原 thread 冷重建；确认观察恢复不重放授权副作用并完成静态分析。
- [x] 6.7 等待展示、取消和对账接入既有 BackgroundTaskResourceProvider；验证运行中仍遵守 idle unload blocker，不另建授权任务清单或未定义挂起状态。

## 7. MCP 配置受控热替换

- [x] 7.1 在既有 Workspace JSONC 配置族和 schema 内补齐必要授权引用及监督参数，遵守既有覆盖层和新增参数命名；无必要不增加参数，不存储凭据明文。
- [x] 7.2 将变更按 Server 准备新连接，验证命名冲突、完整分页和权限；多 Server 候选全部就绪才进入统一切换。
- [x] 7.3 接入现有候选 applying journal／claim／受控准入屏障／active promotion，实现配置、目录及执行准入共同可观察的提交边界；补齐 candidate_applier 返回后才 commit 的窗口，不将两个独立赋值视为共同事务，不新增配置事实源。
- [x] 7.4 实现允许并行实例的先建后换与单实例 stdio 的停后再启，分别处理旧服务保留、apply_failed、recovery_required 和切换后清理失败。
- [x] 7.5 验收逐项安全替换后更新相应配置 policy、诊断与 applied_paths/deferred_paths；保留未知／不支持字段和混合候选的真实重启分类。
- [x] 7.6 验证低优先级遮蔽、多 Server 部分失败、配置与模型调用交错、切换中崩溃、旧连接排空、共享 Backend 工作区隔离和配置 SSE replay；完成静态分析。

## 8. Web 与真实产品链路验收

- [x] 8.1 在主窗口会话流程展示产品授权入口、等待／失败／激活状态；工作区连接管理使用底部面板或既有工作区设置，状态来自后端完整对象。
- [x] 8.2 完成真实前端类型检查及 `bun run --cwd src/clients/web build`；只修改 Web 客户端，不预写 Electron／移动适配。
- [x] 8.3 沿实际 Gateway、目标工作区和页面初始化链路执行浏览器操作，验证任务搜索→连接授权→完整定义→真实 MCP 执行；授权需测试 OAuth/MCP Server 实际接收请求，不能只 mock helper 或检查健康接口。
- [ ] 8.4 验证 model_call 与 turn 下均能通过有效 continuation 和 after_turn 独立内部执行自动恢复，新执行冻结新目录且原 Turn 不变，无需用户再发继续；验证未过准入时的等待事实、拒绝／过期／断开错误可见、原 thread 恢复和远程 owner 回调可达性，缺少环境时记录具体未验证范围。
- [ ] 8.5 验证同轮多个信封调用各自确认、取消和结果配对；记录完整定义输入 token、首次调用成功率、发现轮次和真实 Provider 可用时的 cached tokens，不把 mock 当缓存收益证据。

## 9. 回归证据与交付检查

- [ ] 9.1 每个代码切片迁齐声明、写入方、消费者与测试后集中验证；优先相关短检查，再按共享合同和故障结果扩大范围。
- [ ] 9.2 所有测试使用 test:matrix 或进程外 timeout／内存保护；正式产物镜像 tests 路径，临时浏览器与诊断仅放 out/tests/temp/<task_name>/，禁止根目录测试工作区。
- [ ] 9.3 保留准确命令、源码指纹、原始输出、退出码、构建／实际业务阶段和未验证范围；不把 CLI 规划校验、健康接口或局部通过当实现完成。
- [ ] 9.4 审查重复目录／dispatch／writer、旧 schema 指纹、live 同名回退、静默字段丢弃、宽泛异常、隐式授权／重试、无界等待和无关改动，清理当前范围旧实现。
- [ ] 9.5 对照全部新增／修改规格场景核验并运行 OpenSpec 严格校验；仅在实际实现和验收完成时勾选任务，不把规划修订或结构校验当作功能完成。
- [ ] 9.6 规格同步／归档前确认 unify-product-runtime-logging 已先同步主规格并归档；重新读取最新主规格，核对两个共享 MODIFIED Requirement 保留 Gateway logging.runtime、仅日志热应用及混合 Gateway 候选场景，再同步／归档本变更。前置未完成或正文漂移时先修订 delta，禁止逆序覆盖，不改动日志 change 的文件。

### 3.1／5.1 本地连接控制的 Agent 入口验收（2026-10-27）

- `connections.manage` 沿既有本地 factory、有效策略、完整定义 facet、冻结及信封调用注册，复用原工作区连接 service。list／connect／status／disconnect 不接收 OAuth 参数或凭据；操作身份来自受信 Session／Thread／model-call／provider tool-call，管理路径指向实际工作区的受保护状态 API，不伪造授权页面。默认配置与 schema 同步增加稳定工具 ID，不升配置或数据库版本。
- 正式 `integration-mcp-connection-control` 经真实 Gateway、Backend、Session 消息、Agent、HTTP OAuth 与同一 MCP handler 验证：首次未授权仍能发现本地控制与就绪兄弟工具，未知远端没有伪造定义；短 status 不取消连接，模型提交 code 被真实参数校验拒绝，兄弟 Server 收到完整参数，disconnect 核实关闭并取消原等待。九次 model_stream 请求保留逐调用配对、真实 operation 身份、同 request_id 和无真实凭据／授权地址；不代表真实 Provider 或浏览器验收。
- `product-complete-005` 为1 passed／86.44秒；完整暂存 tree `81de5bee04053883634b78a18088114f25f287d8` 的隔离 `isolated-product-008` 为1 passed／94.17秒，两次范围重叠不累计。隔离源码不含其它未提交的 fork／task 修改，全部4537个 tracked blob 核验无漂移后删除临时源码，正式产物移至 `final-source-008-formal-output/` 保留。
- 相关装配、目录、策略、信封及配置回归 `regression-final-006` 为115 passed／11.19秒；修正旧手工图装配测试遗漏 search_tools，完整 HEAD 的 `baseline-policy-004` 已复现同一失败，保留全集相等断言。初轮产品测试漏必填 Provider 配置与误把 MCP 文本块当字符串的失败原件保留，后者改为核对完整块回执，未改产品行为。
- Ruff通过；四个核心模块 Pyright零诊断，扩大七文件为8条，与完整 HEAD 基线相同、零新增。准确命令、源码指纹、raw、退出码、类型对照与 AST 结构审查在 `out/tests/temp/mcp-connection-control/artifacts/`；无新增源码目录或第二调用／存储链路。OpenSpec strict 校验通过，无Web源码变化。关闭3.1／5.1，当前25/52；授权 UI、完整长等待自动继续及其它未勾验收仍未完成，团队／协作继续TODO。

### 3.6 活动定义恢复验收（2026-10-26）

- canonical 发现结果与 sealed 来源共同派生活动定义，正文及 provenance 沿原 activation 恢复；不新增 CSM 控制行。普通续读不重复注入，压缩后恢复完整定义，变更提供完整替换或删除事实，当前硬隐藏与执行禁用继续生效。fork 复制原发现依赖但不扩大可见前缀，来源登记期间 canonical/view 推进则明确冲突。
- `integration-extension-discovery` 128 passed；既有 `restore-seal-regression-068` 13 passed 的受测生产代码未变，复用原事务回归。新增真实 Runner 验收通过发现、压缩、冷读、fork 后继执行及正式 `edit_and_continue` 回退，1 passed／154.58秒；回退到发现前不恢复后来的定义。模型使用现有 model_stream 资产，不代表真实 Provider 或 Web 验收。
- 准确命令、源码指纹及原始输出保存在 `out/tests/temp/task-oriented-tool-discovery/artifacts/restore-*` 和 `out/tests/temp/tool-definition-recovery/artifacts/verify-20261026-01/`。受影响 Ruff 通过，新增模块与两份测试 Pyright（显式使用 `.venv/bin/python`）零错误；已有扩大类型基线为99条／当前80条且无新增，保留 `restore-type-comparison-064.json`，不宣称全仓类型通过。仅勾选3.6，连接控制与其余综合验收保持未完成。

### 4.1 Server 生命周期 task 验收（2026-10-26）

- SDK 创建、initialize 与退出集中到每 Server 的原 task，LifetimeScope 持有关闭／排空回调；移除外层共享 AsyncExitStack。完整 relist 和原子 publication 仍归原目录 owner，session 子任务失败明确失效目录、保留诊断，关闭失败保留 scope 并拒绝重开。启动与释放同时失败保留两份原始原因；启动取消排空正在 initialize 的 session。
- 同一 Owner 重启时连接 generation 继续推进，不随关闭改写旧代际；旧通知和过期 relist 不影响新目录。此处只是进程内生命周期 fencing，未宣称完成4.2的持久资源／调用资格或4.3的执行 resolver 迁移。
- 最终 `session-lifecycle-regression-final-078` 为136 passed／89.67秒，源码前后无变动；包括真实 stdio 从不同 caller task 启动、刷新、信封执行及关闭，以及 AnyIO 多 Server、取消、SDK reader 故障、启动／关闭双失败和旧通知验收。`session-lifecycle-static-complete-079` Ruff及相关模块／新测试Pyright零错误。命令、指纹、raw及退出码保存在现有 `out/tests/temp/task-oriented-tool-discovery/artifacts/`；不代表OAuth、热替换、真实Provider或Web验收。

### 4.3 精确连接执行解析验收（2026-10-26）

- 删除 Agent 初始 MCP 对象捕获与 live 同名回退，统一由 Owner 核对原连接实例／代际、运行状态和完整 schema；信封不再保留未使用的初始工具映射。相同语义目录不等于相同执行身份，activation 捕获逐项验证成员；最新执行权限仍在调用点检查。
- binding／dispatch 与 lineage 升为 v3，复用现有 member revision 保存 connection_ref，不改数据库结构。v2 完整定义的历史读取与执行校验分离：新增真实 publisher→freeze/seal→发现→压缩→冷 Saver 测试，恢复正文和原 provenance、拒绝旧执行、保留原件；不从旧语义 revision 推断连接。
- `discovery-history-final-029` 为147 passed／95.70秒（正式 `integration-extension-discovery`，含真实 stdio）；`invoker-history-final-030` 为50 passed。受影响 Ruff通过；5个核心模块 Pyright零错误，Agent factory 的3条错误已由完整 HEAD archive 复现，新增诊断位置为空。证据含准确命令、指纹、raw和退出码，保存在 `out/tests/temp/mcp-execution-identity/artifacts/`；两个完整源码快照已核对全部4509个 tracked blob无变化后清理，只保留正式测试输出。
- TODO（性能长尾／完整产品门）：正式 Session／消息入口的调试用例在当前源码与隔离 index 快照均未在120轮等待内完成，分别149.90／153.54秒失败；已执行 Skill、发现及多次真实 Node 调试，日志观察到多次 preparation 相隔约15–17秒。保留 `envelope-product-012`、`staged-product-env-022` 与对应正式输出，不延长业务断言或把局部动作成功当完整验收通过；后续需人工分析该工作负载的准备与恢复耗时。
- 本次仅勾4.3；4.2／4.4的持久 ledger、HITL有界资格与正常替换排空，以及4.6和完整产品门继续未完成。

### 4.5 调用进度、取消与结果未知验收（2026-10-26）

- stdio／Streamable HTTP 统一使用SDK transport及同一个可取消session，移除第三方create_session连接分路。按原RPC ID发送有界取消通知；通知投递不等于远端停止。数值进度复用原Activity，Server自由message不进入公开流；SDK callback写入失败交还原调用。typed未知回执保留不可自动重试、原调用及artifact，局部超时和执行取消共用同一回执。
- 实时终态、request carrier、canonical attempt和冷checkpoint均保留unknown／execution_confirmed=false；显式metadata冲突拒绝。真实产品入口发现并修复codec只按ToolMessage.status推断failure的问题，保留现有P／A两种身份，不合并投影与attempt。
- 最终正式矩阵 `matrix-lifecycle-020` 156 passed／101.85秒，含真实stdio／HTTP原请求取消、并发后继调用、断线与进度写入失败。结果载体回归 `result-carrier-final-014` 153 passed／45秒，最终Gateway产品 `product-frozen-path-021` 2 passed／86.43秒，经过Session消息入口、真实Agent和stdio Server，逐项核公开Activity、并发业务结果、三次模型请求hash、未知回执与冷恢复；模型响应复用model_stream，不代表外部Provider或主Web验收。各组有重叠，不累计用例数。
- 保留失败原件：018修正测试从Job读取不存在turn_id；019完成全部公开事件与恢复断言后，暴露artifacts_dir随当前pytest节点动态变化，最终夹具冻结Server启动时使用的真实收据路径。没有放宽调用身份、顺序、unknown或hash断言。
- `static-delivery-022` Ruff通过，`type-comparison-final-016` 的10个受影响模块仍为89条既有Pyright诊断，与完整HEAD基线相比零新增；新增invocation模块零诊断。`spec-delivery-023`严格OpenSpec校验通过。证据含命令、源码指纹、raw和退出码，保存在 `out/tests/temp/mcp-invocation-lifecycle/artifacts/`。4510个tracked blob全部核验未变后已删除完整临时baseline及依赖symlink，保留integrity报告；未删除正式测试产物。
- 仅完成4.5；4.2／4.4的持久连接lease、封存资格和HITL跨替换，以及4.6综合验收保持未完成，短期SDK调用状态不是持久租约。


### 4.2 持久连接占用切片验收（2026-10-26）

- 原 MCP Owner 接入工作区唯一 ExternalResourceLeaseLedger，以精确 connection_ref 登记 workspace 范围的 mcp_connection；调用经同一执行目标上下文验证原连接、schema 与 typed resource-use，再写入原 operation 占用。不保存第二套目录、调用历史或持久 lease；原公开裸 handle resolver 已移入该准入链内部，直接和间接调用方整体迁齐。
- 受信 Session／Thread、emitting model-call 与原 tool-call 生成稳定 operation 身份；holder 归原 operation，Turn 收尾不能代替结果核实。SDK 收到真实结果／协议错误才确认等待完成；已确认结果和未发送的调用释放本次占用，取消、超时、断线与无法核实的结果保留 reconcile_required，同一 operation 拒绝自动重放。独立字典调用即使没有 ToolMessage artifact，也不会把未知回执误判为已确认。
- SDK 原生命周期 task 交付关闭证据，只有核实关闭才登记 stopped；关闭失败保留 unknown 及释放错误。重启只核对 MCP 领域的旧资源，不改变终端等其它 owner 的事实，不恢复旧 socket、不把旧调用迁投相同 schema 的新连接。持久等待的主动继续持有声明、已封存资格及 HITL 跨替换、有界排空仍未完成，因此4.2／4.4／4.6继续保留未勾选。
- 完整暂存 tree 隔离验收：`mcp-lease-isolated-111` 中正式 integration-extension-discovery 为166 passed／109.49秒，信封、资源账本、执行恢复及真实请求准备为95 passed／58.35秒，6个核心模块／新增测试 Pyright零诊断，受影响 Ruff通过。最终 `mcp-lease-isolated-product-114` 为2 passed／95.45秒，经真实 Gateway、Session消息入口、Agent与stdio Server核对并发成功／业务错误、未知结果、实际软件operation归属、磁盘账本及冷checkpoint；模型继续复用model_stream，不代表真实外部Provider、OAuth或Web验收。
- 保留111的未配置真实密钥启动错误、112的网页embedding遗留Provider引用、113的夹具工具声明联合形状处理错误原件；最终夹具只登记回放Provider，并使用既有无embedding网页模式，未放宽调用配对、身份、顺序、hash或unknown断言。111与最终114的源码tree只差该产品测试夹具，115逐blob核对最终index与受测tree一致；各次完整archive已核验4516个tracked blob后删除，只保留命令、指纹、raw、退出码、integrity及正式输出。
- 扩大型检查109为20个文件，完整HEAD基线176条／当前176条，零新增诊断；该轮6条新Ruff格式诊断已由110及隔离最终检查修正。不宣称全仓类型通过。全部原件位于 `out/tests/temp/csm-kind-boundary/artifacts/mcp-lease-*`、`types-mcp-lease-109/`、`staged-mcp-lease-111/` 和 `staged-mcp-lease-product-11*/`。

### 4.4 有界封存资格切片验收（2026-10-26）

- 模型发送前从 Saver 冻结的 activation 取得原连接 handle，经唯一 ExternalResourceLeaseLedger 登记资格；响应后绑定真实 model-call，只保留实际输出的信封调用身份。执行核对 Session／Thread／model-call／operation，保留 HITL edit 在原封存目录内选择目标的行为。资格与实际调用分别结算，并发兄弟各自完成后释放共同资格；未知实际操作不会被资格到期或关闭代为结清。
- 同一 MCP 配置族新增 `supervision.sealed_call_timeout_seconds`，默认1800秒、范围1–86400秒，从模型发送前取得 handle 开始覆盖生成与人工等待；相同资格不可重新发放或重试续期。到期释放短期引用并拒绝后续执行；重启仅释放失去 handle 的 MCP 资格，不改动其它资源 kind 或虚报外部停止。lease JSON 增加可选 expires_at，不改变 SQL 结构或 schema 版本，仍明确拒绝未知字段。
- 最终暂存 tree `c7e9db4366e7dc3cefdfa8835554186705399d96` 经完整 archive 隔离验收：正式 `integration-extension-discovery` 190 passed／115.75秒，相关合同、同步执行、账本、配置、执行恢复及真实 CSM 请求准备154 passed／78.90秒，Gateway→Session消息→Agent→真实stdio Server产品验收2 passed／95.64秒。三组范围有重叠，不累计；产品逐项核对真实资格、实际operation、Session／Thread、并发结果、进度、三份请求hash、断线unknown及冷恢复，模型响应复用既有model_stream，不代表OAuth、真实外部Provider或Web验收。
- 保留121的共享协议调用方缺少资格、122的资格与实际占用混入断言，以及131的同步CSM空目录向已关闭事件循环登记timer等失败原件；迁齐真实调用方、分别验证两类占用，并修正空目录不登记timer，未放宽配对、归属、schema错误或hash断言。126为25 passed，129为86 passed，132为77 passed，133为9 passed，最终134集中复验。
- 受影响Ruff与严格OpenSpec校验通过；最终扩大Pyright完整HEAD基线147条／当前133条，零新增诊断，不宣称全仓类型通过。准确命令、源码指纹、raw、退出码及类型比较位于 `out/tests/temp/csm-kind-boundary/artifacts/staged-mcp-reservation-134/`；4517个tracked blob核验未变后已删除完整临时source，保留正式测试产物与integrity证据。
- 本切片验收时4.2／4.4／4.6继续未勾选：正常配置热替换后的旧runtime保留、退役排空及HITL跨替换依赖7.x共同发布屏障。后续7.x切片已接通正常替换与退役排空，未用资格提前结算由下方canonical结果切片补齐；持久等待、完整HITL产品恢复、授权／连接控制和完整产品门仍未完成。

### 7.3 共同发布基础切片验收（2026-10-26）

- 候选应用器整体迁为原shadow scope内的资源准备与同步激活回调，删除应用器提前生效再提交的旧合同；Workspace生产调用方及直接／间接测试全部迁齐。applying journal／claim先于候选资源准备建立，取消及准备失败走同一回收／claim释放链路，SQLite CAS、配置快照、shadow及运行时激活使用同一同步发布边界。ConfigService读取、MCP Owner、Registry和调用准入复用同一工作区发布锁，兄弟工作区不共享锁；网络准备及旧scope排空均在锁外。
- 准备失败但回收未确认的scope继续由原owner持有，下次候选必须先排空，不丢失资源引用；提交后的激活／排空错误保留真实新active。显式候选准备不能因JCS将1与1.0视为同一值而被跳过。生产代码净减少，未增加配置事实源或兼容分路。
- 暂存tree `c3fb3bcc1d535afcec6083b5e3f96d7509f73c80`完整archive隔离验收：配置、shadow、策略和API合同191 passed／238.37秒，正式integration-extension-discovery 190 passed／117.56秒；真实双工作区编译、请求、失败释放等前4项通过，并核对同工作区共享锁及跨工作区独立锁。新增5项候选准备／取消／CAS／线程读取行为先于此轮通过，23项scope／adapter回归亦通过，范围有重叠不累计。
- 保留完整失败原件：136在120秒保护期限内完成88项后退出124，141扩大到必要完整配置范围后全部通过；139的3处mixin类型声明问题及141的TYPE_CHECKING import排序问题均已修正。141的3项Goal关闭测试仍在既有ready.wait的8秒业务期限失败，随后SIGINT收取堆栈并登记interruption原件，第四个同组参数未执行。完整HEAD `c2ff15f75`的同一cancelled-active-paused用例亦失败于相同期限（142，45.90秒）；保留原代码TODO及全部期限断言，不宣称完整挂载套件通过，性能长尾按既有范围待测量优化。
- 142另暴露未接模型回放的取消收尾夹具依赖DEEPSEEK_V41_FLASH_API_KEY；相关挂载测试改用既有inprocess_model_replay，未改凭据环境、业务断言或实现假模型。最终tree `4e0498c3fdb10584633a5fa9c14cfd98c2183d60`的剩余取消收尾、数据隔离及各工作区pending消费4 passed／40.44秒（143）。它与141的源码差异只有TYPE_CHECKING import排序和这3个测试的夹具接线，复用未受影响的191／190项结果，不重复长跑。
- 143的15个文件Pyright完整HEAD基线174条／当前152条，零新增诊断；包括整个config_service目录的Ruff基线27条／当前27条，零新增，严格OpenSpec校验通过，不宣称全仓静态检查全绿。原始命令、退出码、源码指纹、类型比较和失败／中断说明保存在 `out/tests/temp/csm-kind-boundary/artifacts/staged-config-publication-141/`、`config-publication-mounts-142/` 和 `staged-config-publication-final-143/`；各完整archive的4517个tracked blob均核验未变后清理，只保留正式输出和证据。
- 交付复查进一步覆盖准备失败后、等待原scope回收期间被取消：owner继续持有closing scope，配置store记录包含取消的原异常组，不丢失释放任务。最终受测tree `cf453d576194779f675ea292cd5eb910e8447448` 的145隔离验收为40 passed／42.44秒，涵盖该新场景和候选、CAS、已提交发布、读取与scope回归；16个文件类型检查及扩大Ruff与完整HEAD相比仍零新增诊断，严格OpenSpec校验通过。该轮仅扩展异常处理并补回归，复用未受影响的191／190项和真实挂载结果；完整source及baseline各4517个tracked blob核验未变后清理，原件保存在同目录 `staged-config-publication-cancel-145/`。
- 7.3继续未勾选，进度仍15/52：该提交只完成原配置链的准备／共同发布基础，尚未接入逐Server候选、完整MCP准入fence与崩溃／替换对账。7.2／7.4的正常替换、单实例停启和退役排空，以及7.5／7.6验收完成前，保留/mcp现有restart_workspace策略；不将本切片当作MCP热替换交付。

### 7.2 HTTP 候选整体替换验收（2026-10-26）

- 原 Owner 只准备受影响的 HTTP Server，初始化、完整目录、命名和候选工具策略验证后，经既有配置发布锁共同提交。原 relist 锁覆盖准备到提交／候选回收，命名冲突同时检查未变 Server，避免规范化名称覆盖及检查后 relist 漂移；网络准备不持有 publication 锁。正常替换保留已取得的原连接资格，禁用／移除同时撤销更早退役代际的后续准入；退役关闭只根据原 ExternalResourceLeaseLedger 排空，不结清结果未知操作。
- turn-bound 原执行首次模型发送前取得精确 handle 和原期限，后续工具循环只继承该资格；原 execution scope 关闭时释放，不向其它执行发放旧资格、不续期。model-call 资格与实际外部操作仍分别结算。配置 shadow 只回收未提交候选，后续配置变化不能关闭仍 active 的连接。
- HTTP Server 路径按真实受支持能力开放热应用；stdio、监督参数及其它需重启字段的混合候选仍整体 pending_restart。状态的 applied_paths／deferred_paths 从当前 candidate／attempt 的原 outbox 读取，不复制状态；动态判定 stdio 待重启时事件正确标记 restart_workspace。
- 完整暂存 tree `d55d8579197bdabae6442eb4696f3b25d4c12170` archive 验收：正式 integration-mcp-config-replacement 为1 passed／64.39秒，真实双挂载、watcher、Runner 和 HTTP MCP handler 核对旧 Turn 仍请求原连接、新 Turn 请求新连接、多 Server 候选部分失败、stdio 混合整体延后及兄弟工作区隔离；模型复用 model_stream，没有另一套 SSE 实现。integration-extension-discovery 为201 passed／118.80秒，覆盖命名冲突、候选间 relist、原 task 关闭、通知 fencing、资格期限与退役排空。完整配置／事件回归另为112 passed／156.95秒，源码后续仅修改 MCP Owner 候选串行与对应测试，未改变该配置链路。
- 修改范围 Ruff exit0；12文件 Pyright 基线84条／当前73条，零新增诊断。完整 config_service 目录 Ruff 基线27条／当前27条，未改既有异常合同凑绿。前次产品测试因配置 API 请求漏认证头得到401，已修正测试并完成上述真实后续阶段；失败原件保留。准确命令、tree／基线、raw、退出码及类型比较位于 `out/tests/temp/mcp-http-publication/artifacts/verify-001/`、`verify-002/`；上轮199项等原件仍位于 `out/tests/temp/csm-kind-boundary/artifacts/http-candidate-inheritance-150/`。
- 仅关闭7.2，当前16/52。7.3的进程崩溃／切换恢复核验、7.4的单实例stdio停启和配置／活动诊断中的退役清理失败、7.6的完整SSE replay等仍未完成；本地连接控制、OAuth、持久等待和真实Web授权入口继续按原任务推进，不将局部产品集成当成完整验收。

### 7.3 进程退出与共同提交恢复验收（2026-10-26）

- 在真实 Backend 的候选准备完成后，通过 OS 硬退出覆盖 promotion 前、SQLite promotion 已提交而内存尚未激活两个窗口。前者保留旧 active 及未提交 journal，后者保留新 active、committed journal 和唯一 applied 事件；重启建立新连接，不恢复旧 socket 或重放业务工具。
- 实测发现并修复：lease 尚未到期的立即重启会永久停在 applying；promotion 已提交的遗留 claim 又因没有 applying 候选而无法清理。启动对账现在按原持久期限等待、保留有效 fencing，过期后复用原 store 和 reload 流水线建立新的验证／提交 attempt；原未提交 journal 保持 recovery_required。已提交 claim 的到期回收独立于 applying 候选，不改动数据库结构或版本。
- 正式 integration-mcp-config-recovery 为3 passed／146.94秒，覆盖立即／到期重启、原 claim 未被提前抢占、真实 HTTP 新目录与同 request_id、从原游标两次 SSE 续读、唯一 applied 事件及零业务调用重放；相关配置、apply、事件及真实 stdio／HTTP 调用生命周期140 passed／166.54秒，原双挂载 watcher／Runner 热切换1 passed／51.49秒。均使用完整 Git tree archive、进程外超时和4GiB保护。
- 修改范围 Ruff通过；5文件 Pyright 基线57条／当前18条，完整 config_service 等扩大 Ruff 基线27条／当前27条，均零新增诊断，不宣称全仓静态检查全绿。严格 OpenSpec 校验通过。受测tree、准确命令、raw、退出码及比较位于 `out/tests/temp/mcp-config-recovery/artifacts/verify-005/result.json`。
- 保留probe-001的测试配置文件缺失、probe-002的已提交遗留claim、probe-003的立即重启不对账，以及verify-004的全部原件。连续场景还暴露第三方SSE库保留上一测试Server的退出标志；共用HTTP夹具显式重置该测试进程状态后完成上述验收，未改协议或放宽业务断言。
- 关闭7.3，当前17/52。7.4的单实例stdio停启及退役清理诊断仍待实现；7.5／7.6保留未勾选，直到相应替换范围全部验收。OAuth、连接控制、持久等待和真实Gateway／Web链路继续按原任务推进。

### 2026-10-26 7.4 单实例 stdio 停后再启切片

- 已有 stdio Server 的替换／禁用／移除沿唯一 Owner 停后再启；新 stdio 与 HTTP 候选仍可先准备。停止前由原配置 recorder 核实 apply claim 身份、fencing 和期限并写入精确连接停止意图，同一发布锁内撤销原准入、发布不可用和请求原 task 关闭；核实关闭后才启动候选，不保留 stdio 一律重启的旧分支或旧 applier 签名。可先验证的其它 Server 优先准备，失败时避免无必要地破坏旧 stdio。
- 候选失败且旧实例已停止时，原 journal／配置状态明确 recovery_required，旧 active 不伪称已经恢复；同一 watcher 可在用户修复配置后重新验证并提交。监督参数等需重启字段仍使整个混合候选 pending_restart，applied_paths／deferred_paths 继续来自原配置事件。
- 最终暂存 tree `8c98890ef422b274492e000034a1b2d05a1f8ccf` 的完整 archive 验收：真实单实例 OS 锁、stdio SDK、双挂载、watcher、Runner 与 model_stream 产品集成1 passed／56.22秒，核对停止前 journal、原实例退出先于新实例启动、相同 schema 的新 connection_ref、失败及修复后两次业务收据和兄弟工作区隔离；原 HTTP 热切换1 passed／50.82秒，真实 OS 崩溃恢复3 passed／146.33秒，正式 integration-extension-discovery 203 passed／115.84秒，配置／shadow／策略／apply／事件与 API 合同204 passed／233.21秒。过期 claim 拒绝破坏动作的定向用例亦通过，已包含在204项中，各组不累加。
- 修改范围 Ruff 零诊断；10文件 Pyright 的33个诊断均在 HEAD 基线复现，零新增，检查本身 exit1；整个 config_service 的 Ruff 基线／当前均27个既有诊断，零新增。准确命令、源码tree、raw、实际退出码和正式产物保存在 `out/tests/temp/mcp-stdio-publication/artifacts/verify-008/`；先前 verify-007 的单个新增测试类型诊断原件保留，完成显式类型收窄后才通过本轮对比。最终仅追加 OpenSpec 设计、场景与本收据，源码副本逐 blob 核验后清理。
- 7.4继续未勾选，进度仍17/52：异步退役清理失败尚未接入产品配置／活动诊断；当前原 LifetimeScope 显式报告且保留失败引用。非合作 stdio 的关闭超时、stdio 停机中的 OS 崩溃、OAuth、连接控制、持久等待与完整 Gateway／Web 授权不由本切片证明，按各自任务继续推进；团队／协作专项仍保持 TODO。


### 2026-10-26 7.4 退役清理诊断闭合

- 原 retirement 组件持有唯一退役 runtime／阶段，配置 reload-status 通过只读端口投影 `mcp_cleanup`，与配置状态在同一发布锁下读取；独立表达 draining／closing／cleanup_failed、释放核实和异常类型。关闭失败发布原 resource.state 合同的 release_failed，保留原 scope 失败引用及完整诊断；已提交配置／applied 事件不被倒写，同 Server 未核实释放时拒绝冲突替换，其它 Server 可以继续更新。超时后实际完成关闭时，诊断如实标记已核实释放并保留原超时。没有新增持久表、配置参数或后台状态机。
- 完整暂存 tree `d0f1fb600fdf23f7d53fad269c432ca5efa80d88`：真实 Backend／HTTP MCP／文件 watcher／配置 API 的故障注入1 passed／19.80秒，核对新目录、原 applied 事件、清理失败通知、request_id、拒绝冲突及 active 原件不变；正式 integration-extension-discovery 205 passed／119.70秒，配置／API／合同142 passed／236.74秒。前期定向15项已包含在205项内，不相加；stdio停启及旧HTTP切换的具名产品证据沿上一收据按未变依赖复用。
- 9文件Pyright基线／当前均13项、Ruff均30项，零新增；全config_service Ruff均27项，检查exit1不称全仓静态全绿。compileall、Buf格式／依赖／相对真实HEAD breaking、前端真实tsc和Web build均通过。002首次遗漏隔离副本的既有Web依赖链接而类型检查失败，另有一项新Python类型诊断及导入格式；003链接已有依赖并修正后才通过，不安装或修改用户环境。源码／原命令／raw／退出码与架构自查在 `out/tests/temp/mcp-retirement-diagnostics/artifacts/` 的 verify-002／verify-003／regression-004／architecture-005；本轮仅追加规划、修正schema陈旧注释和CSM收据日期，不改变受测业务源码。
- 关闭7.4，当前18/52。7.5／7.6仍按各自逐项验收；非合作真实stdio关闭、stdio停止窗口OS崩溃、OAuth、连接控制、持久等待与Gateway／Web授权尚未由本轮证明，团队／协作专项继续TODO。

### 4.4 canonical结果结算与原执行到期边界验收（2026-10-26）

- 原执行的canonical提交观察入口组合MCP owner和原后台消息ack，迁齐唯一StepExecutionPorts构造与Runner消费者；删除只通知后台消息的旧接线。完整结果按精确Session／Thread／model-call／provider tool-call结算未来资格，跳过partial／incomplete，重复回执不影响兄弟调用，未将结果未知或正在执行的实际operation视为完成。
- 真实产品测试先复现turn策略禁用后整个Job失败；修正为只撤销相应工具资格，原execution仍能解释失败。到期释放原工具／runtime引用，在原scope存活期间保留到期身份，后继模型调用不重新计时。扩大回归发现具体schema错误被笼统资格错误覆盖，已恢复原错误原因并保留全部原断言。
- 最终`discovery-reasons-final-002` 209 passed／114.72秒，`product-reasons-final-002`真实双工作区watcher、Runner、model_stream及HTTP MCP 3 passed／92.81秒，覆盖原连接替换、两种策略禁用、canonical配对、下一模型请求前释放和Server未收到请求。此前`product-final-001`的5项结果／并发／断线unknown／冷恢复亦通过，范围有重叠不累计；模型回放不代表真实Provider或Web验收。
- `unit-002`为85 passed／1 failed；唯一system_time周期消息用例已在完整HEAD archive中用`baseline-background-001`复现同一失败，与本切片提交观察链无关，未修改产品行为或放宽断言。Ruff通过；最终Pyright与完整HEAD基线均28条，零新增诊断，不宣称全仓类型全绿。准确命令、前后源码指纹、raw、退出码和比较位于`out/tests/temp/mcp-qualification-settlement/artifacts/`。所有最终运行源码前后不变；4522个HEAD blob核验一致后删除临时baseline源码，保留正式产物及完整证据。
- 4.2／4.4／4.6保持未勾选：当前产品没有完整HITL resume API/UI，手工保留资格与执行测试不能替代该产品门；持久等待主动持有、OAuth及完整Web／真实Provider验收仍未完成。结构未变，不提升schema版本，不引入第二账本。

### 7.5 退役准入撤销保持失败事实（2026-10-26）

- 新断言先复现撤销准入将cleanup_failed／error_type重置为draining／None；原退役组件改为只替换admits_sealed，保留已核实阶段、原期限和错误类型。失败诊断及同Server冲突屏障不因撤权消失，其他Server仍可更新。
- `out/tests/temp/mcp-retirement-revocation/artifacts/`保留red-001原始失败、green-002的39 passed／7.23秒和static-002的Ruff／Pyright零错误；准确命令、指纹及退出码完整保留，源码前后未变。不将组件单元验收扩大为尚未实现的产品disconnect验收，7.5综合字段分类与7.6仍未完成。

### 7.5 连接字段显式分类与真实替换验收（2026-10-26）

- 唯一配置策略的`*`只匹配一层、尾部`**`显式递归；迁齐全部既有Workspace／Gateway规则及schema manifest，保持非MCP生效范围。Server根规则仅允许增删，已验收的enabled／transport／command／args／env／cwd／url／headers逐项登记，env／headers键保留JSON Pointer转义。未知子字段和监督路径不借根规则热生效，混合候选仍整体pending_restart；不新增配置字段、版本或存储。
- `product-http-final-002`真实watcher／双挂载／Runner／HTTP MCP为4 passed／136.72秒：Server真实校验环境变量展开后的Authorization，核新增、换URL及凭据引用、禁用、删除、再启用的新代际和兄弟工作区隔离；无效字段保留原active及事件、明确invalid_config。原测试继续覆盖多Server部分失败、sealed请求交错、两种目录策略禁用和混合候选完整deferred_paths。
- `product-stdio-final-002` 1 passed／73.77秒：实际command由Python改为uv、同时替换args／env／cwd，Server记录真实工作目录、单实例锁、PID及停止journal；保留坏schema后的recovery_required和修复执行，再真实切为HTTP并经Agent执行，逐项核applied_paths。`recovery-sse-final-005` 4 passed／159.45秒覆盖HTTP提升前后真实OS退出、原claim有效／到期后重启、原active重建与两次真实HTTP SSE续读；`http-protocol-regression-004` 9 passed／11.20秒确认共享夹具仍保留取消、进度、unknown及原调用配对。
- `config-regression-003`配置基础设施、ConfigService、API合同及Gateway诊断／验收矩阵263 passed／280.40秒，含低优先级遮蔽、来源、pending及日志既有行为。25项策略短检查另通过；范围有重叠不累加。初次60秒组合检查完成44项后保护退出124，完整范围扩大至必要预算后通过；HTTP初次失败是新测试误把schema拒绝当作已进入候选状态，依据既有invalid_config合同改为严格核原active／事件不变，未改产品状态机或放宽原断言。
- Ruff通过，5文件Pyright完整HEAD基线8项／当前4项，零新增，保留共享协议测试的4项既有诊断；单一default_policy类型及重复规则测试的非法scope字面量已修正。命令、前后源码指纹、原始输出、退出码、类型对照和结构检查位于`out/tests/temp/mcp-config-policy/artifacts/`。最终验证均无源码漂移；无Web源码变化，未重跑Web构建或外部Provider。
- 关闭7.5，当前19/52。7.6继续未勾选：非合作真实stdio关闭期限、stdio停止窗口中的OS崩溃尚须专门验证；OAuth、连接控制、持久等待、HITL完整产品恢复和Gateway／Web授权仍按原任务推进，团队／协作保持TODO。

### 7.6 stdio 强制关闭与停止窗口崩溃验收（2026-10-26）

- 单实例进程资产统一到原 MCP 测试目录，继续复用同一 schema_server handler。真实进程收到 stdin EOF 后仍存活、记录并忽略 SIGTERM，由官方 SDK 升级 SIGKILL；10秒期限内核实旧 PID 消失和锁释放后才启动新实例。1秒期限明确 recovery_required、旧 active 与原 journal 保留，原 scope 继续持有实际关闭；关闭完成前后均不偷偷启动失败候选，用户修正配置后由同一 watcher 重验提交。没有新增生产终止协议或另一套 MCP Server 实现。
- 在原 scope.close 实际完成后、候选实例尚未启动时 OS 硬退出；只观察产品生成的停止意图、journal、pending 和 claim，不预写关键状态。重启先按旧 active 重建新连接，未到期 claim 的 fencing 保持，随后沿原到期对账重验提交候选；原 attempt 保持 recovery_required，唯一 applied 事件，三次真实启动均持单实例锁且无业务工具重放。配置快照 healthy 与 MCP 目录 readiness 分别核对：破坏动作后目录 unavailable，不能把仍有效的配置快照等同于在线连接。
- 正式 `integration-mcp-stdio-recovery` 为3 passed／87.37秒，外层收据 `recovery-matrix-final-004` exit0／91.56秒，源码前后无漂移。原单实例 watcher／Runner／model_stream 替换、失败修复及 stdio→HTTP 产品回归 `replacement-final-002` 为1 passed／73.14秒；HTTP promotion 前后崩溃、claim 冷恢复、清理失败及 SSE replay `http-recovery-regression-002` 为4 passed／159.99秒，均 exit0、源码前后无漂移。最终对共享 report 的唯一后续修改是增加只读 catalog_status，正式 stdio 套件覆盖该新增字段；不重复未受影响的 HTTP 长检查。
- 低优先级遮蔽、多Server部分失败、模型交错、旧资格排空和双工作区隔离沿7.2—7.5及4.4的具名证据按未变产品依赖复用。四文件 Ruff／Pyright 最终均零诊断，严格 OpenSpec 校验通过；原始命令、源码指纹、raw、退出码及失败现场在 `out/tests/temp/mcp-stdio-faults/artifacts/`。保留初轮 Pyright 入口缺失、类型收窄、导入格式与新崩溃断言混淆配置 healthy／目录 readiness 的失败原件，修正测试合同后才完成最终正式验收。没有改动数据库或Web源码；Windows Job Object、OAuth、完整HITL、持久等待和Gateway／Web授权不由本切片证明。
- 关闭7.6，当前20/52；连接管理／授权进入下一纵向切片，团队／协作专项继续TODO。

### 5.6 前置：失败连接的原目录 API 状态（2026-10-26）

- 真实1秒stdio关闭期限失败后，原GET `/mcp/servers` 实测500：无通知能力查询访问已关闭session，DTO也只接受ready／disabled。原刷新选择统一跳过不在线runtime，typed snapshot与DTO接受实际failed，公开proto登记现有string的三种语义；不新增读取链路或吞掉在线连接的协议错误。新单测核失败alpha不被访问、在线beta仍完整刷新。
- `red-001` 保留真实500及原request_id；`product-final-002`正式stdio矩阵3 passed／87.78秒，核不可用期间HTTP返回failed、原工具描述与同request_id，旧进程／锁仍由原scope持有，后续修复及崩溃恢复原断言不变。`unit-final-002` 39 passed／4.78秒，`http-diagnostics-final-002` 1 passed／14.20秒，保留真实stdio协议错误的503、安全错误、诊断原文和同入口恢复。
- Ruff及5文件Pyright均零诊断，Buf格式／lint／build及相对真实HEAD `b590c163c` 的breaking检查通过，Web真实tsc及build通过。首次误用Bun直接执行tsc在4GiB保护下触发JSC MemoryExhaustion；改为项目的 `bun run --cwd src/clients/web tsc --noEmit` 入口后25.52秒通过，未放宽内存保护或修改依赖／环境。无UI行为修改，未将构建当作浏览器授权验收。
- 两份OpenAPI由仅含本边界的完整暂存tree导出，不混入其他会话尚未提交的fork／task字段；同时补齐HEAD已提交但旧快照遗漏的directory_uri及mcp_cleanup。逐项JSON差异留在openapi-delta.json。4524个tree blob核验，仅两份预期生成快照变化后清理临时源码；原命令、源码指纹、raw、实际退出码及完整正式产物保留在 `out/tests/temp/mcp-server-readiness/artifacts/` 与同名正式测试路径。严格OpenSpec校验通过。
- 5.6仍未勾选，进度20/52：本切片只闭合原状态出口，独立连接操作、OAuth、授权UI、持久等待及其Gateway／Web产品门继续推进；团队／协作TODO不变。

### 5.1／5.3 前置：首次 OAuth 启动与官方协议边界（2026-10-27）

- 可选 OAuth 配置沿原 Workspace JSONC 族解析；缺省保留原 stdio env／HTTP headers 链，禁止 OAuth 覆盖显式 Authorization。官方 Provider 承担发现、DCR、PKCE、state、交换及刷新，产品在注册／token 副作用前核对资源、issuer、HTTPS／loopback 与 S256；未知401不作待授权成功。首次无用户回调只保留已确认的 authorization_required，异常组混入清理失败仍拒绝启动。唯一目录不虚构远端工具，保持本地扩展和已就绪兄弟 Server 可发布。
- SDK TokenStorage 凭据按工作区／Server／endpoint／回调隔离，复用既有跨平台私有权限与原子文件原语；重启恢复原绝对期限及非默认 token endpoint，不重复 DCR、不延长凭据。OAuth 专属日志和异常链不能携带 state、token 或交换响应正文。没有新增状态库／schema 迁移、第二 MCP 适配器或自制 OAuth 交换协议。
- 正式 `integration-mcp-oauth-startup` 的 `matrix-002` 为10 passed／20.53秒：真实 HTTP OAuth／同一 schema_server MCP、SDK PKCE与冷刷新、错误state、token失败、无metadata、issuer／PKCE拒绝、授权成功但目录非法及同名不同endpoint；真实 Backend 启动核authorization_required／ready、无未知工具、200与同request_id。`discovery-regression-001` 为220 passed／114.60秒，保留原目录、策略、信封、SDK生命周期、冻结／恢复及stdio／HTTP取消进度。两组准确命令、前后指纹、raw和实际退出码在 `out/tests/temp/mcp-oauth-startup/artifacts/`，源码无漂移。
- 10文件Ruff／Pyright零诊断，Buf格式／lint／build及相对真实HEAD e8a66258f 的breaking通过；Web真实tsc及build通过。两份OpenAPI只增加该状态枚举，由完整暂存tree ce5df2655b2dba23bee3f45a622de9591ff3897e 导出，4527个源码blob核验后清理副本，不混入他人fork／task改动。保留首轮凭据文件计数混入前一场景、误用不存在的pyright入口、Pydantic URL类型直接比较导致错误拒绝及类型诊断的原件；修正后才完成最终验证，没有放宽issuer或业务断言。
- 5.1—5.7和7.1仍按完整任务保留未勾选，进度20/52。当前证明初次启动及官方协议适配，不证明尚待接入的connections.manage、持久操作、实际产品callback／UI、HITL、长等待恢复或Gateway／Web授权；团队／协作专项继续TODO。未做Windows实机或外部Provider验收。

### 5.2／5.3／5.5：持久连接操作与受保护回调（2026-10-27）

- 唯一 WorkspaceStateStore 升级15，SQL资产新增连接操作和幂等 alias。实际 SDK session、完整目录和执行代际继续归原 MCP Owner，启动、配置替换和显式连接共用创建入口；授权等待不持有目录锁。持久重放、短查询、300秒deadline、取消、配置指纹 fencing、断开及重启对账沿同一 service，无第二数据库或工具 dispatcher。
- 新增受保护操作／状态／取消／授权读取／POST callback API；URL仅用户管理读取且no-store，code／state只在内存交给官方SDK。错误state、重复callback、私有body校验和安全阶段错误不泄漏交换正文。token交换不代表ready，必须继续initialize和完整目录验证；断开即使广播失败仍核实原scope关闭。运行时断开不改JSONC，重启按已启用配置重建。
- 正式 `integration-mcp-connection-operations`：7 passed／75.82秒，包含真实Backend及HTTP OAuth，验证首次授权、兄弟Server、intent／alias终态重放、错误／重复callback、短观察不取消、运行时断开、OS级退出／重启不重放交换、token／目录失败、期限与迟到回调、配置移除fencing及清理失败。关键状态均由操作产品入口生成；旧14库升级及唯一active／终态fence另有定向5 passed（与其它范围有重叠，不累计）。
- 相关 MCP／WorkspaceStateStore／请求校验／首次授权回归236 passed／100.78秒；Ruff通过，7个核心文件Pyright零诊断，最终期限逻辑另行Ruff／Pyright通过。container／workspace_lifecycle／http_validation的现有诊断经完整HEAD `8af6e181f` archive复现，对比新增诊断为空；基线4529个blob核验并清理源码副本。
- Buf格式、lint、build及对真实HEAD的breaking检查通过；Web真实tsc与build通过。OpenAPI从仅含本切片的暂存tree `acfee8c5f0c3c073ef2f80147b7becfb00d8bc97` 导出，只新增上述MCP路径和DTO；4533个源码blob核验后已清理导出副本，未混入并行fork/task字段。最终timer仅改变操作运行态，不改变导出合同。
- 最终完整暂存tree `c42656df0d0c1f24ef7ac5e9cf011c0a9e6e2fa5` archive隔离复验：同一正式连接操作套件7 passed／77.81秒；核对全部4535个blob未变化后移除源码副本，原正式测试输出保留在 `artifacts/final-source/out/tests/`。该树不含其它agent尚未提交的fork／task改动，不依赖其实现。
- 准确命令、源码指纹、raw、退出码、类型基线对比及结构索引位于 `out/tests/temp/mcp-connection-operations/artifacts/`；正式测试输出保留在各测试同名目录。当前进度23/52。5.1／5.6／5.7、7.1及后续Agent控制、产品UI、Gateway／Web授权、精确thread后台等待／自动继续门仍未完成；不把Backend API或SDK协议夹具当作这些验收。团队／协作TODO不变，未做Windows或真实外部Provider验收。


### 2026-10-27 MCP 精确 thread 持久等待与独立继续

- `connect`／`status` 仅在显式传入 continuation 时登记原 thread 的 typed/versioned BackgroundTask；稳定身份绑定 operation，同一等待不覆盖不同意图。原成功 ToolResult 与 task 匹配后才观察连接，失败／取消结果不产生继续资格。canonical locator 排除 request projection carrier，旧布尔查询及全部调用方已物理迁齐。
- 观察步骤在原控制事务原子保存 checkpoint、稳定邮箱消息及待投递引用，下一步骤用原 RuntimeSourceDispatcher 幂等提交 after_turn internal execution、turn_id=None；新执行冻结新目录，旧 Turn 不变。投递前重查当前权限、连接、原执行、agent 与 deadline；已提交 wakeup 的恢复复用身份。后台任务用独立 Context，不继承创建者的 pinned config、执行准入和 delta sink，原 Gateway／request 身份显式恢复。
- 真实 Gateway→Session 消息→Agent→OAuth／MCP 验证：`product-010` 的 turn、model_call、邮箱提交后崩溃及双 thread 独立取消四场景通过；该轮唯一失败是重启后查询已完成旧 Job 的内存 API（404）。`restart-accepted-017` 修正为退出前确认 Job 完成、重启后核持久 execution 终态，1 passed／80.38秒；未放宽执行数量、唯一 wakeup、Turn 归属、实际业务收据或逐请求 hash 断言。Backend 与 Gateway 使用同一隔离控制目录，各场景保存独立进程日志。最终完整暂存 tree `0956bc52a34a156227af13e5815c2e70ddfff538` archive 隔离复验为5 passed／399.31秒；同树后台／恢复／资源清理72 passed／28.06秒。全部4562个 tracked 文件核对无漂移后删除源码副本，正式输出保留在 `isolated-details-019/formal-output/`，不依赖其它会话未提交的 task／fork 改动。
- `unit-and-recovery-012` 57 passed／29.76秒，含 handler、原子邮箱、真实进程退出、精确索引正文零读取与增长量、schema 拒绝及真实授权期限；`control-regression-010` 19 passed／102.78秒；`observer-final-015` 1 passed／2.53秒验证观察期限、取消不影响共享授权、登记前终态和交换仅一次。组间有重叠，不累加计数。完整 HEAD archive 复现旧句柄快照断言错误，资源清理断言改查权威持久 deleted 状态。
- 14个生产文件 Ruff 0诊断；Pyright HEAD基线30／当前29，零新增诊断，不宣称全仓类型通过。受影响测试 Ruff、差异检查及 OpenSpec strict validation 通过。准确命令、源码指纹、raw、退出码、基线与结构统计在 `out/tests/temp/mcp-persistent-wait/artifacts/`。结构审查确认新目录四部分 AGENTS 齐全，既有大容器只增装配，未新增第二目录／任务库或网络授权 writer。
- 当前31/52。6.6仍未关闭：完整产品的真实 child 冷重建、用户输入交错、Session 删除及全过程 deadline 等门继续补齐；本轮不代替授权 Web UI、远端可达性、完整 HITL 或外部 Provider 验收。团队／协作专项继续 TODO。


### 2026-10-27 首次禁用目标的冻结目录事实

- 首次发布前执行禁用曾令 publisher 删除 members／definition facets，实际冷读精确查询错误返回 tool-not-found。新定向用例 `red-disabled-001` 通过真实发布、freeze/seal 和冷 Saver 复现后，唯一 publisher 改为保存完整已知注册事实；指引、搜索、恢复及执行点继续独立按有效策略裁决，不增加目录、writer 或 live 补洞。
- 初始禁用的本地／MCP 目标、硬隐藏与未知目标明确区分；禁用／隐藏结果不包含定义，任务搜索和导航继续排除。既有选择关闭／重新开启测试改为核对成员事实保留、导航切换、重新开启后原快照恢复，旧封存正文不重写。
- `policy-regression-002` 63 passed／76.11秒覆盖完整目录／搜索／指引／定义恢复；`guard-003` 20 passed／3.26秒保持信封执行撤权与配对。`product-003` 1 passed／51.69秒由真实 Gateway→Session→Agent 搜索并尝试禁用目标，核对三个身份错误、原 tool_call_id 配对、远端零请求及全部3份实际请求 preimage/hash。模型使用现有 model_stream，不新增 SSE 桩。
- 生产 publisher Pyright 零诊断，受影响文件 Ruff、OpenSpec strict validation 与矩阵加载通过；命令、指纹、raw、退出码及复现证据在 `out/tests/temp/mcp-discovery-policy/artifacts/`。3.4整体仍未勾选：已知未授权目录的冻结 readiness／连接需求与未知目录区别继续处理，不以本切片代替其验收；团队／协作 TODO 不变。
- 完整暂存 tree `fbd4d6d9b101d9cb90270af17a828649b0fbde77` archive 隔离复验：真实 Gateway 产品用例1 passed／52.67秒；搜索、目录、指引、恢复和信封回归83 passed／81.75秒。4563个 tracked 文件全部无漂移，源码副本已删除，正式输出保留于 `isolated-details-006/formal-output/`；该树不含其它会话的 task／fork 改动。


### 3.4／7.1 已知未就绪目录与配置合同验收（2026-10-27）

- 唯一 publication 保留已验证的不可变定义；断开及 SDK 再授权只发布目录专用成员，不保留执行工具。搜索、导航及压缩恢复明确连接需求，旧成员不能取得执行资格；兄弟 Server 保持可用。定义未变时语义 revision 不变，binding／cursor 独立约束 readiness。历史 v2 继续按原合同读取定义，不能据此取得 v3 执行资格。
- 最终完整暂存 tree `115f0728ca6417ee4c56ddaacbe8eea0078ab9fd` 的 `isolated-details-014`：Ruff通过、8个生产模块Pyright零诊断、OpenSpec strict通过，相关搜索／恢复／binding／信封72 passed／76.37秒；新真实Gateway→Agent→OAuth／MCP场景通过，核对已知待授权、未知目录、原调用配对、零未授权业务请求、兄弟真实执行及4份请求hash。初始禁用场景在Backend启动前因46499端口占用失败，未进入业务逻辑；端口随后已释放，未终止未知进程，同树 `isolated-details-016` 单独补验1 passed／54.39秒，退出码0。
- 较早tree `6f1c441e0a7669942bee2f838e3a37a0a40db826` 的012为整个MCP单元及信封196 passed／103.69秒；产品组合报告13 passed／417.56秒，但进程随后触及420秒外部保护，实际退出124，不将其记为整组成功。最终v2读取修正由014覆盖。012／014／016各4563个tracked文件核验无漂移，源码副本已清理，正式输出、raw、命令及实际退出码保留于 `out/tests/temp/mcp-discovery-policy/artifacts/`。
- 现有 Workspace JSONC／schema 已支持 OAuth redirect_uri、env／header凭据引用及 sealed_call_timeout_seconds；沿既有覆盖层和未知字段拒绝，无需增加参数。`known-config-015` 76 passed／18.31秒覆盖安装、配置验证、策略和MCP配置。
- 架构复查未新增源码目录或第二writer；MCP目录18个直接Python文件。既有catalog_owner增长至1136行，保留大文件残余风险，不在此切片扩展结构重构。关闭3.4／7.1，当前33/52；完整HITL、Web授权及其余未勾场景继续推进，团队／协作专项TODO不变。


### 5.4 Server 与上游应用授权归属（2026-10-27）

- 本地 connections.manage 的冻结定义明确只管理 Server 访问授权，上游应用沿统一 search_tools／信封使用该 Server 已发布的管理能力；不增加应用注册表、授权代理或第二dispatcher。未知管理目标不补造定义，非法本地应用操作按真实schema拒绝；远端isError与structuredContent保留实际层级及状态，等待用户不被描述为已授权。
- `isolated-details-017` 经真实Gateway→Session消息→Agent→同一HTTP MCP handler验证：远端查询报告upstream_app授权需求、已发布管理工具返回waiting_user，本地不支持操作配对失败，Server仍ready，只有两次预期远端业务请求，未知目标无schema。冷canonical核对5个独立attempt的顺序／成败／结构化正文，6份真实请求与封存hash一致。model_stream仅提供模型响应，不证明外部应用OAuth或真实Provider能力。
- 产品1 passed／76.97秒，实际退出0；受影响Ruff、生产connections模块Pyright零诊断、OpenSpec strict通过。完整暂存tree、4563文件无漂移校验、raw、准确命令及正式输出保留于 `out/tests/temp/mcp-discovery-policy/artifacts/isolated-details-017/`，源码副本已清理。修正design中断开删除成员的旧表述，与已验收的目录专用成员合同一致。关闭5.4，当前34/52；Web及完整授权入口继续未完成。


### 2026-10-27 Web 授权与工作区连接管理

- 会话内提供非敏感管理入口，主窗口底部面板按工作区显示 MCP；完整操作对象来自原 owner，失败回读，切换目标撤销旧请求。共用标签组件替换各面板重复实现，具名用户的标签经后端保存并在刷新后恢复。
- 独立回调页清除 query，禁止缓存及 Referer；只向同源原弹窗回传 code/state，原页面固定原 Gateway 目标与操作提交私有 POST。关闭、到期、目标切换及不可达回调均明确显示未确认。Gateway 静态访问日志仅移除该具名回调的 query，不改普通日志。
- `web-product-032` 经真实 Gateway／Web／Agent／OAuth／MCP 为1 passed，84.74秒，实际退出0且源码无漂移：只交换一次，兄弟连接保持ready，授权后取得完整schema并真实执行；核对6份实际请求的preimage/hash、canonical归属及凭据不进入模型、会话、日志、Referer或浏览器存储。本轮回复按该轮真实用户输入所在Turn定位，不依赖历史回复数量。模型响应沿用model_stream，不证明真实Provider或跨Gateway授权。
- `ui-state-031` 19通过；`web-types-031`、`web-build-031`和`ui-python-static-031`通过，源码无漂移。`ui-types-python-023`的6文件Pyright零诊断，受测Python未变；`ui-backend-operations-027`中连接操作8项通过，静态UI的4项通过／1项失败源于测试日志捕获；该断言修正后的`ui-backend-unit-029`为5通过。共享协议资产、首次多Server启动、未知401、错误callback、交换后初始化／目录失败、到期、断开失败及Agent入口复用5.1—5.5的具名证据，不累计重叠用例数。
- 命令、源码指纹、raw、实际退出码和失败原件保存在 `out/tests/temp/mcp-discovery-policy/artifacts/`；正式浏览器现场在同名测试输出下的 `run-425c64d795c3`。032误用Bun不支持的语法检查入口而执行到缺少fixture，未启动浏览器；033改用Bun build仅解析脚本并通过，临时解析输出已删除。新增两个源码目录的AGENTS齐全，相关Python AST可解析；已有大型DTO和测试父目录未在本切片扩展重构。
- 关闭5.6／5.7／8.1—8.3，当前39/52。完整HITL、child冷恢复／用户输入交错／删除期限、跨Gateway授权及其余未勾项继续推进；团队／协作专项TODO不变。

### 6.6 原 child 授权等待冷恢复（2026-10-27）

- 真实 Session 消息生成 main 历史，经公开 source-point／fork-context 创建同 Session child；child 登记授权等待并结束原执行。邮箱已提交、dispatcher 尚未接受时强杀 Backend，重启后原 child 冷编译并完成独立 internal execution，turn_id=None；main 无额外执行或唤醒，继承 assembly hash 不变，新五份请求逐一核对 owner、preimage/hash、工具配对及真实 MCP 回执，OAuth 仅交换一次。模型继续复用 model_stream。
- `child-wait-product-034` 新场景1通过；仅含 HEAD 与该测试文件的完整 Git 快照 `isolated-child-wait-037` 中 child 通过，组合另一个 main 场景失败于新增测试提前读取空 rollout，尚未进入业务。限定仅 child 读取继承基线后，`isolated-child-wait-038` main 场景1通过／71.79秒，实际退出0；child 分支业务断言未变，复用037证据。038 Ruff、Pyright零诊断及OpenSpec strict通过；037沿同树036复用静态结果，036筛选引号错误未执行产品测试的原件保留。
- 命令、tree、源码指纹、raw、实际退出码与正式输出保存在 `out/tests/temp/mcp-discovery-policy/artifacts/`；035—038源码副本均核验无漂移后清理。6.6仍未勾选，用户输入交错、Session删除及全过程deadline等产品门继续未完成；团队／协作TODO不变。

### 6.6 授权等待准入组合收口（2026-10-27）

- 新增独立文件/工作区的`integration-mcp-wait-admission`，复用原Gateway/Agent装具、同一OAuth/MCP handler与model_stream资产，不拼装第二套SSE。真实用户消息仍在生成时，授权完成通知可持久接受，但after_turn内部执行必须等待用户执行终结；三份execution仅两份用户Turn，六份真实请求逐一核对原owner、preimage/hash、发现定义与工具结果配对。
- 原活动后台任务继续阻挡删除；用户通过资源控制明确取消后，经导航operation删除并核实settled，再完成工作区授权，不重建Session或模型调用。另以管理API先完成授权、Agent再通过status登记continuation，验证终态重读不会丢通知，原操作仅一次token交换，独立内部执行无伪用户Turn。
- `product-admission-001`中用户输入场景通过，删除场景最初未遵循活动后台任务的既有删除限制，整次退出1；修正为明确取消后删除。005两项失败源于驱动将短status变化误当终态，原输出明确为verifying/catalog；保留成功断言，改为有界等待真实succeeded。最终`product-blockers-008`为2 passed/114.34秒，退出0。001之后的变动只涉及这两项及期限场景，用户输入分支与共享计划行为未变，复用其通过证据。
- `product-deadline-009`为1 passed/343.51秒、退出0，使用原产品300秒期限，不改时钟或持久状态。通知提交后保留294.06秒原预算，截止后0.82秒内观察到deadline_exceeded结算，零内部wakeup/新execution/业务调用，操作仍succeeded且连接ready；完整测量位于正式产物的`wait-deadline.json`。
- 最终010 Pyright与011 Ruff均零诊断，012等待单测14 passed，013矩阵注册校验通过；本轮无产品API/前端/协议变化，不重复Web构建。各运行保留进程外硬期限、4GiB保护、准确命令、逐文件指纹、raw及实际退出码，源码前后相同，位于`out/tests/temp/mcp-wait-admission/artifacts/runs/`；正式输出位于`out/tests/integration/backend/agents/test_mcp_wait_admission/`。最初002因未使用既有临时pyright环境而无法启动，004及最终010以项目解释器完成，未改依赖或用户环境。
- 结合上方原main/child硬重启、重复callback和双等待者取消证据关闭6.6，当前41/52。完整HITL、跨执行typed持续占用、跨Gateway授权及真实Provider指标仍按原未勾项推进；团队／协作TODO不变。

### 3.7 大定义的普通读取与完整发现验收（2026-10-27）

- 新产品场景经真实Gateway→Session消息→Agent搜索，在4096字节预算内取得needs_read引用；下一轮使用实际返回的read_path调用普通read_file，拼回模型所见完整正文核对字节数、SHA-256、完整$defs/enum/required/additionalProperties及原provenance，再经既有信封取得真实MCP回执。搜索本身零业务调用；五份实际请求逐一核owner、preimage/hash，Provider ToolSet及prefix epoch保持不变，不新增schema读取或SSE实现。
- 仅含HEAD及本测试的完整tree `isolated-large-definition-042`：产品1通过／69.95秒，实际退出0，Ruff、Pyright零诊断及OpenSpec strict通过。同tree的`isolated-discovery-regression-043`为55通过／72.49秒，覆盖首次搜索前冻结、live修改/删除与冷Saver、分页冲突、软/硬隐藏、撤权、真实输出中间件及物化失败、恢复定义/删除事实和turn/model_call边界。3.6的真实Runner压缩→冷恢复→fork→回退与本change5.x/8.3的内联定义和真实执行证据按未变依赖复用，不将单元冷读冒充进程或浏览器验收。
- 保留039的原测试payload类型诊断、040的PrefixEpoch类型诊断与041的schema预期失败原件；LangChain公开tool_call_schema确实补入Server工具描述，修正为完整公开契约相等断言，未放宽schema内容、正文hash或调用配对。042/043与失败副本均核验tracked文件无漂移后清理源码，正式输出、准确命令和退出码在 `out/tests/temp/mcp-discovery-policy/artifacts/`。关闭3.7，当前40/52；完整HITL、持久等待剩余场景及跨Gateway授权仍未完成，真实Provider缓存收益不由回放证明。

### 2026-10-27 审批恢复的 pending-write 控制合同

- Saver 按官方 WRITES_IDX_MAP 分离可更新控制槽与普通不可变写入，消除 interrupt 与恢复输出争用 index 0。未改变 SQLite 结构或 schema 版本；普通重试正文冲突继续明确拒绝。
- `pending-final-011` 为4 passed／5.51秒，包含同步／异步、冷读、普通写入冲突和既有损坏检查；最终类型／Ruff检查及新测试复验 `pending-final-static-014` 退出0。命令、逐文件指纹、raw与实际退出码保留在 `out/tests/temp/mcp-tool-approval/artifacts/runs/`。新合同纳入两项既有历史存储矩阵。完整审批和HITL跨替换仍未勾选。


### 4.4 原图审批与资格释放闭合（2026-10-27）

- 审批从已提交 checkpoint interrupts 投影，经原 AgentControlInbox 接受决定，以官方 Command 恢复同一 Job／execution／图；内部执行复用同一入口，不建立审批库或新挂起状态。直接工具与信封目标统一中间件，删除旧实现；私有执行决定不改写原 AIMessage、canonical 调用或封存请求，graph revision 升为16。拒绝、直接应答及不存在的编辑目标沿原 canonical writer 结算；实际编辑目标用于执行与输出归档，结果仍归原调用。
- 修复取消后的 model-call 资格泄漏：原 owner 按 execution scope 统一释放待用资格，到期不续期，已发送且结果未知的 operation 仍保留独立占用。真实 Gateway／Session／Agent／HTTP MCP 全部9场景通过（582.35秒），覆盖同 schema 替换下 approve／edit／respond＋拒绝、直接工具改为另一已注册目标或未知目标、真实60秒到期、主动断开、撤权和取消；核对远端零请求或唯一原连接收据、原 call ID、三份请求 hash、迟到409与磁盘资格结算。正式矩阵保护期限按测量扩大至720秒，未修改业务期限。
- 隔离 tree `afcd5bb100f744cbd1a8bab6dde720d62894a0f7` 的真实 Chromium／Gateway／Agent 验收1 passed／72.33秒，页面发消息、刷新同一检查点、编辑及拒绝均通过，真实 MCP 仅收到预期请求且无 pageerror；Web tsc、构建与严格 OpenSpec 校验通过。该 tree 核心回归203 passed，8项新增工具返回形态测试因局部 Command 类型注解求值失败；修正为模块导入后，完整 archive `51b31e56ff449ebd8ce990ae09165e175dc36315` 的13项审批测试通过，未修改产品行为或断言。
- 补齐固定信封本身要求审批时的精确投影；完整 archive `93d1c1d2236f56cdd2322787b720df442472f9ee` 的审批／runner 21 passed／3.91秒，覆盖同步／异步、直接结果／Command／Command列表及两个相同参数调用的独立 ID。最终仅修正新增测试的 RunnableConfig 注解／import 格式，扩大静态检查为28个文件：完整 HEAD 基线147条／当前143条 Pyright诊断，Ruff基线／当前均6条，零新增，不宣称全仓静态全绿。各组范围重叠，不累计用例数。
- 命令、tree／逐文件指纹、raw、准确退出码、失败原件与正式产物保留在 `out/tests/temp/mcp-tool-approval/artifacts/`。保留边界场景首次失败、测试断开字段错误、读文件参数错误、隔离副本依赖链接缺失等原件；未放宽身份、顺序、期限、结果未知或 hash 断言。
- 关闭4.4，当前42/52。4.2的跨执行持续持有、4.6的综合冷恢复／隔离、8.4的跨 Gateway 授权和8.5的真实 Provider token／缓存指标仍按原未勾项推进；本批回放与本地 HTTP 不证明真实 Provider 收益或旧 socket 可恢复。团队／协作 TODO 不变。
