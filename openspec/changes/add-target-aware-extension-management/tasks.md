本清单按 capability 与完整纵向链组织，编号用于依赖和证据定位，不代表所有工作必须串行。实施项仅按已完成的闭包勾选，宽项的局部证据不代表整项完成。每项勾选需对应真实源码／协议和适用验证；源码盘点、进程启动或原版接口存在不能代替产品行为证明。

## 1. 既有 owner 与公共目标合同

- [ ] 1.1 清点原扩展／MCP／设置 UI、源配置、所有直接与间接调用、生成协议及测试，列明权威写入／读取 owner、单链路迁移与删除清单，不改变他人已有任务证据。
- [ ] 1.2 在现有协议 owner 明确安装目标、配置范围、宿主视图／generation、能力和操作状态的 typed 合同；稳定 Gateway／Backend／workspace 身份与临时 route 分开，不新建通用插件对象体系。
- [ ] 1.3 迁齐目标管理 API、host 通道协商、proto／OpenAPI／生成客户端和消费者；验证实际序列化、未知类型／版本拒绝与单请求 request_id，全链真实类型检查先于产品验收。
- [ ] 1.4 在原联邦 policy 登记安装／宿主管理能力及 configuration_read／configuration_write 与 schema，迁齐默认拒绝／认证主体规则和调用方；覆盖脱敏读取、订阅披露、预检／提交／pending 管理和独立重启权限，验证撤权、Relay 不提权、只有发现权限时无目标写入，不另建权限系统。
  - 配置能力已沿原 policy／schema 接入 Gateway 自有配置路由与两种 workspace 代理 mount；安装／宿主管理能力和实网目标验收仍未完成，整项保持未勾选。
- [ ] 1.5 实现绑定稳定目标的能力／状态发现及实际路由，区分离线镜像和实时状态；验证同名、route 变化／重用、停止和权限撤销，无隐式启动或本地 fallback。
- [ ] 1.6 清点各功能将使用的既有 URI 与字段；在原配置／联邦／MCP owner 补本稿已明确需要的 source 读写、私有输入引用、受管 launch binding、管理能力正式 delta／schema／消费者；确需新增 URI 时补原寻址 owner delta，不在前端拼 grammar。

## 2. MCP 目录与目标安装

- [ ] 2.1 沿原 gallery 服务合同接入可配置 MCP 来源，验证实际协议、分页、取消及未配置／失败／无匹配；与 VSIX 来源分开，查询不下载、不启动或扩展来源范围。
- [ ] 2.2 实现目录条目、手动定义及 HTTP 服务的同一候选计划，冻结来源／entry／manifest digest／确切 variant／目标平台／版本与参数；验证同名不同源、variant 消失、多同类候选和 SSE 明确拒绝，无首项 fallback，HTTP 不虚报包下载。
- [ ] 2.3 在目标侧建立唯一安装记录与真实制品发布链，复用生态工具／隔离环境、锁定依赖及不可变镜像 digest；核 launch 实际使用发布路径／generation，不能把原版动态 npx／uvx 配置视为安装。缺 runtime／安装能力明确失败，保留既有显式动态命令，不自动全局安装系统环境。
- [ ] 2.4 实现有界进度、日志、意图指纹／幂等操作、取消及中断查询，暂存验证后发布；核同键异参数冲突、断流／重启不重放未知脚本，安装、配置与 ready 分阶段报告。
- [ ] 2.5 实现同安装域精确版本复用、工作区引用、更新失败保留旧版及物理卸载检查；制品引用绑定原 connection generation，沿原退休／监督释放；验证 A1/A2、并发同意图、旧调用／cleanup_failed／结果未知保留和有引用拒绝卸载。

## 3. MCP 与现有 Agent 唯一链路

- [ ] 3.1 在原 MCP 管理服务 seam 消费目标配置、安装及原 MCP owner 的完整对象，迁齐旧 React 连接入口；移除该运行面重复 client、enabled/status 与目录，不复制原列表／详情。
- [ ] 3.2 配置计划通过原 workspace source／candidate／CAS 提交，补受管 launch binding／私有输入引用 schema 与解析消费者；在原 MCP 凭据 owner 扩展按 workspace／server／输入键版本及来源绑定的私有写入与引用，不能把 OAuth store 当完整 input secret 实现。核 A1/A2 secret 实际调用、重启、换地址不借用、正文／DTO／事件／日志脱敏，保留既存配置与 OAuth。
- [ ] 3.3 接入连接操作、OAuth、目录通知和工具调用原生命周期；验证目录 revision／generation、热替换、封存资格、排空及结果未知合同，无第二套资源租约或逻辑 session 固定功能。
- [ ] 3.4 支持受验证 VSIX 动态 definition Provider，保留来源实例／host generation／collection／原定义、provide／启动前 resolve／change／dispose／取消语义，仍明确选择 workspace 并交项目 owner 启用；验跨目标同 ID、离线、迟到 resolve、配置 unavailable，拒绝 A 本机动态定义到 B 执行，静态可迁移模板须重新校验且不搬秘密，不在 Extension Host 另启项目 Agent MCP client。
- [ ] 3.5 在本地 A1/A2 与远端 B1 的真实 Thread 调用独有测试工具，核同名 server、凭据、授权等待、原 tool_call_id 和实际目标；包安装成功不能替代 Agent 真调用。

## 4. 原版扩展管理与 Web 宿主

- [ ] 4.1 复用原扩展视图、详情和安装服务，接入经验证目录及用户明确 VSIX；安装前校验 manifest、版本、依赖、平台、所需 API／运行位置，区分安装与激活。
- [ ] 4.2 将纯声明式及 browser 扩展接入原 Web Extension Host／Workbench profile，验证真实命令和 UI 贡献；Node-only 扩展没有合法目标时明确不可用。
- [ ] 4.3 将安装选择器与目标分组接入原 Quick Input，冻结发起身份与版本；验证切 Tab、目录失败、目标离线不改投，不抢占默认 Ctrl+P 查询范围。
- [ ] 4.4 统一 VSIX 安装与目标安装服务的操作／版本／引用事实，保持 Web profile 资源与目标制品各自存储；实现原 profile／workspace view 启用、版本更新／停用／卸载和 reload，分清新版安装与运行激活，保留旧实例引用制品，不伪造动态卸载支持。

## 5. 多目标 Node Extension Host 闭包

- [ ] 5.1 清点原 Remote 及 extension ID 单例假设：description registry、enablement、RunningLocation／Tracker、HostManager／Factory、activation、依赖、MainThread／ExtHost RPC、贡献／卸载及全部消费者／测试，确定唯一扩展服务内的多目标接入点。
- [ ] 5.2 在现有 sourceTransform 与扩展服务闭包中实现稳定目标／客户端／明确 workspace view 的 RunningLocation 和 host 分派；以真实 A/B Node 扩展原型验证协议，不仅修改枚举或轮换全局 remoteAuthority。
- [ ] 5.3 在明确后端目标侧接入匹配上游的 Node runtime／host 组件及认证 RPC 通道，复用原启动和 activation 协议；不放到 Gateway 请求进程、Python Agent 或 Electron main。
- [ ] 5.4 绑定同目标明确 workspace 集合与原 multi-root 事件，分离各客户端活动态；不同 VSIX 启用集使用不同明确视图，不伪报同 host 的 folder 级隔离。验证 A1/A2 不包含 B1、视图变更有明确生命周期、一个客户端不覆盖另一个。
- [ ] 5.5 迁齐相同扩展 command ID 的来源记录与目标分派，保留 manifest／ID、菜单条件与快捷键；有发起目标直接路由，无上下文且多候选时明确选择，切焦点不改投。
- [ ] 5.6 实现宿主 generation、迟到 RPC fencing、惰性启动、有界断线回收与后端生命周期排空；真实崩溃／重启不重放未知副作用，宿主退出不删除安装或稳定存储。
- [ ] 5.7 迁齐公开 extension ID 与内部安装／运行实例身份，extensions.all／getExtension／exports 和依赖只解析该 host 合法实例；验证 A/B 同 ID 不同版本真实激活，更新／停用 A 不改变 B；静态声明冲突明确诊断并要求合法版本／窗口选择，不忽略重复注册或更名公开 ID。

## 6. 扩展 API 与状态的逐类验收

- [ ] 6.1 完成 design D8 能力矩阵与机器可查询诊断，按实际服务声明支持／拒绝；未支持 API 不返回空成功，不把类型存在记为可用。
- [ ] 6.2 迁齐文档／语言／符号／搜索 Provider 的目标注册、原 URI identity、模型同步与取消，沿现有 SearchService；明确过滤初始／dirty textDocuments、visible／active editors、诊断／watcher 和所有事件，跨资源编辑预检全部归属及版本。验 A/B 同开 dirty 文件并切焦点，非所属 active 为 undefined，无内容泄露或迟到发布。
- [ ] 6.3 接通 host workspace view 的 fs／watcher／configuration 与目标 owner，复用原 typed URI transformation；真实扩展分别经 Node 文件 API 与 workspace.fs 读目标独有文件并返回可打开位置，fsPath 在目标真实有效。验嵌套根原 folder 匹配、重复本机路径身份不足时明确拒绝，不把任意字符串全局替换、不建第二根绑定，不访问浏览器 IndexedDB 同名根或中继文件系统。
- [ ] 6.4 接通实际支持的 terminal／tasks／debug／process API 到目标运行服务，核目标 PATH／平台；不支持项明确错误，不在本地或 Electron 创建替代进程。
- [ ] 6.5 在原窗口 command dispatcher、TreeView／WebviewView resolver、setContext／菜单及资源通道实现按 host／view 的运行注册与展示绑定，静态声明一次登记；验 B 在 A 焦点调用、A dispose 不撤销 B、同 ID view 正确切目标、CSP／端口／迟到消息隔离，无绕过权限资源通道。
- [ ] 6.6 迁齐原 MainThreadStorage／ExtensionStorageService 等权威存储的目标隔离、globalState／workspaceState 稳定身份与变更通知；补目标本机有效 storageUri／globalStorageUri／logUri／extensionUri。验证真实文件写入、两客户端并发／重启和 A/B 隔离，不虚构事务合并，秘密留消费端。
- [ ] 6.7 核扩展依赖、原生模块与 proposed API 的真实兼容条件，保留明确不支持原因；用至少一个真实 Web 与两个目标的 Node 测试扩展逐类证明，不只验 mock handler。

## 7. 目标感知设置编辑

- [ ] 7.1 在原 Settings Editor／命令接入目标及来源层选择，持续展示 scope、目标、可写能力与原发起上下文，切导航不改投未提交内容。
- [ ] 7.2 在原 Gateway／Workspace 配置 owner 补现有 REST 缺失的合法来源层能力、脱敏正文、schema、完整 effective／遮蔽读取及 source writer，迁齐协议和消费者；区分 SQLite 物化与文件回写，无 writer 层明确只读，不以摘要或有限 runtime PATCH 冒充完整能力。
  - 2026-11-04 Workspace 后端切片：原配置 API 提供按 source_key 的脱敏 JSONC、schema、各层正文、最终来源／遮蔽及 active 状态；仅工作区来源层提供 writer，其余层明确只读。原 proto／descriptor／OpenAPI／SSE 生成物已迁齐，未登记来源 URI 保持为空。Gateway 来源 writer、用户级 source fan-out 与设置界面尚未完成，本项不勾选。
- [ ] 7.3 业务配置表单与 JSONC 共用原候选预检、CAS、幂等提交和新增来源 writer，支持删除选中层覆盖、格式保留、冲突重取与编辑内容保留；禁用通用文件保存旁路，不把脱敏占位写回秘密。
  - 2026-11-04 Workspace 后端切片：预检复用原有效配置构建且不导入来源、不写文件或改变 readiness；保存核文件／来源层／active CAS，移除 JSONC 键即删除该层覆盖。秘密占位只在原字段及对象绑定还原，保留原秘密 token、注释和格式；普通正文前缀不触发占位协议。原 pending 行在文件替换前登记幂等意图，watcher／重试只认领匹配的原候选，pending_restart 等状态不重复执行资源副作用；有效配置未变化时不提升 active、不新增事件。表单及编辑器消费者尚未完成，本项不勾选。
- [ ] 7.4 接入原配置 applied／deferred／restart 状态及 pending 管理入口，核真实健康证明；未登记字段不宣称热应用，不以保存成功代替运行生效。
- [ ] 7.5 实现用户 Workspace source 影响预览与原 fan-out 汇总，保留逐 workspace 候选、版本和结果；验 B1 成功／B2 冲突／B3 停止追赶，不跨到 A 或伪造全局事务。
- [ ] 7.6 远端设置透传到原生 owner 判权与提交，断流后查询原幂等提交；本地只保留原连接登记和显示投影，不增加可生效远端配置副本。
  - 2026-11-04 双 Gateway／双 Backend 真实 socket HTTP：2 passed／128.88 秒，覆盖同 UUID 不同物理工作区、目标默认拒绝／精确主体读写／撤权、无权限零下游、预检零文件写入、同键并发只一次 applied、原键查询／重试及旧 CAS 409；只修改目标 B，A 保持原状态，request_id 与转发目标归属正确。复用原受控本机 SSH 执行／隧道替身，未验证真实 SSH、Windows 或设置界面，本项不勾选。完整命令、源码指纹、原 HTTP 响应及静态检查见 `out/tests/temp/configuration_federation_network_20261104/artifacts/network-final-evidence-1793813123212470975.json`；网络测试文件 Ruff／Pyright 均零诊断。
- [ ] 7.7 将 Workbench 设置、会话设置、Gateway 用户视图和 registry 操作分别交原 owner，设置 UI 只导航合法对象；凭据沿原私有入口，不以 runtime candidate 包办所有状态。
- [ ] 7.8 新增字段确需版本变化时补同族 schema、默认值及官方预览／备份／可恢复迁移；保留现有 JSONC 层语义，不自动重命名或清空用户配置。
- [ ] 7.9 在原 Code OSS 配置服务迁齐 profile local／目标 remote user／稳定 view／folder／语言覆盖与 setting scope，按 host/view 路由原配置模型，经已登记目标 FileService／writer 访问 multi-root 原 `.code-workspace`；不轮换全窗口 workspace，扩展键不写业务 JSONC。验 get／inspect／update／change payload 投影、原缺省推导、非法资源拒绝和 A1/A2/B1 不随焦点改投。
- [ ] 7.10 设置搜索与 @modified／@ext／@lang 绑定冻结域／目标／来源层及真实注册 schema，验同 key 不同值、切焦点和旧查询取消，不将业务配置伪装成支持所有扩展过滤维度。
- [x] 7.11 落实 D3 权限表：pending retry／discard／resolve／失败记录核配置 write 和原证明／fencing，状态返回另核 read；实际 managed_backend_restart 核 managed_workspace_admin，消费候选再核 write。验配置权限不授予重启，Gateway restart-development 仍 local-only，不新增远端 Gateway 重启能力。
  - 2026-11-04：生产 ASGI 路由与受控上游验证 45 项通过，原 runtime 组 248 项通过，Gateway 序列化契约与认证迁移单例各 1 项通过。撤权后使用公开 `error`／`message` 事件，停止第二条配置披露、关闭上游并释放路由引用；原 controller 在 retry／drain／进程动作前拒绝缺少配置 write 的候选消费。该证据不代表真实网络或真实 SSH 验收。
  - 原始命令、逐文件 SHA 与退出码：`out/tests/temp/configuration_federation_permissions_20261104/artifacts/configuration-permissions-final-evidence-1793811673988170281.json`。Ruff 42 条与实测改前逐项相同；Pyright 生产组 70 条与实测改前逐项相同，main 另 1 条位于未改 handler 且未独跑其改前基线，新增 ASGI 测试无类型诊断。扩大代理回归尚被旧 `backend_workspace_id` fixture 阻断；配置扩大检查及迁后 validation 检查超时，后者仅观察到 46 个通过点，完整回归未完成。

## 8. 界面、快速入口与单链路迁移

- [ ] 8.1 原扩展／MCP 视图按此工作台及目标分组，详情显示安装／启用范围、来源版本、连接与日志；主窗口底部仍按工作区，右侧仍按会话，不维护重复状态卡片。
- [ ] 8.2 将合法操作与设置定位贡献到原 Action／Command／Opener，复用 `>`、`view `、`ext `、`mcpr ` 和帮助的真实能力；不因注册入口而承诺整个市场或全部 Node API 已可用。
- [ ] 8.3 按迁移清单迁齐旧 API、UI、间接调用与测试后物理删除同义实现和死命令，核无第二 MCP client、安装账本、有效设置或扩展服务。
- [ ] 8.4 迁移稳定目标格式及来源记录，核旧记录拒绝／官方迁移，保留 profile、草稿、凭据和引用；回退源码不自动反向执行已提交外部操作。

## 9. 真实产品与发行验收

- [ ] 9.1 为三个 spec 的全部场景建立证据映射，使用本地 A1/A2 与真实远端 Gateway B1/B2，设置同名对象和独有内容；先最小有判别力闭包，独立 fixture 才并行，不默认全套串行测试。
- [ ] 9.2 通过实际 Gateway 与原 Workbench 浏览器验证初始化 API、原扩展／MCP／设置交互、命令、作用域、权限与 request_id；保留目标侧进程、网络和原始状态证据。
- [ ] 9.3 用真实 MCP 工具、真实 Web／Node 扩展完成调用／文件／配置／终端及不支持能力断言；覆盖并发、取消、冲突、断流、迟到结果和重启，不以进程启动代替业务验收。
- [ ] 9.4 在源码目录外且关闭开发服务器的 Electron 打包产物验证静态资源、相同目标链、设置与扩展状态重启恢复；目标侧运行时独立发行可核，不增加 main/preload 原生能力。
- [ ] 9.5 每批完成相关静态分析、真实类型检查、协议生成一致性及受影响构建；涉及浏览器前端完成 `bun run --cwd src/clients/web build`。测试统一使用现有 runner 或进程外超时／内存保护，复用同源码已有构建证据。
  - 2026-11-04 Workspace 来源编辑切片：来源 API 恢复批 29 passed，启动／MCP／原故障合同批 22 passed，合法 pending 启动补验 1 passed，配置与旧库迁移单元批 128 passed；各批有重叠，不汇总相加。Buf 格式／lint／build／breaking、协议生成一致性、OpenAPI 导出及 Web 构建通过。Web 实际 tsc 退出 2，完整 HEAD 解包基线与当前均为相同 28 条诊断、无新增；新生产文件及 API／协议 Pyright 零诊断，其余生产组按真实 HEAD 对照无新增，调用方扩大类型检查被资源保护终止，未完成。
  - 扩大协议／reload 合同批为 81 passed 后遇旧 ValueError 文案预期失败，完整 HEAD 单例同样失败。发行 SQL 单例先遇既有 Gateway schema 18 断言（实际及 HEAD 迁移均为 19），尚未执行到已迁齐的 Workspace schema 24 断言，不记作发行验收通过。本批最终证据及 39 个文件指纹见 `out/tests/temp/workspace_config_source_edit_20261104/artifacts/workspace-source-edit-final-evidence-20261104T174359459564Z.json`；Ruff 最终与原 HEAD 对照无新增，仅合并嵌套条件的修订按短路语义等价核验复用行为证据。不将本后端切片扩为整个设置模块、真实 Web 交互或原生目标验收。
- [ ] 9.6 保留准确命令、源码指纹、原始输出、退出码、未验证阶段及真实兼容矩阵，按正式／临时目录规范落盘；交付前检查无双轨、静默失败、越权、无关改动和虚假完成，再按实际证据勾选。
