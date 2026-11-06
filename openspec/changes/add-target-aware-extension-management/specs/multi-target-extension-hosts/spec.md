## Purpose

定义 Web 与 Electron 共用工作台中的原版扩展安装、Web 宿主与多后端目标 Node 宿主，以及贡献和 API 的精确路由，使同屏多个 Gateway 工作区可以使用各自扩展，且不把浏览器或 Electron main 变成远端插件执行环境。

## ADDED Requirements

### Requirement: 扩展安装必须使用原版入口及明确兼容能力

系统 SHALL 复用原扩展视图、详情、搜索与 VSIX 安装交互，支持经验证的可配置目录和明确本地 VSIX 来源。安装前 SHALL 按 manifest 的 extensionKind、browser／main、engines、信任条件、版本、目标平台、依赖及实际宿主能力判断可用性；目录未配置／失败及能力不支持须可辨认，MUST NOT 承诺任意市场或扩展均兼容。安装、激活和实际功能可用 SHALL 分别报告；要求本机 UI Node 语义但当前运行面不具备时，不得冒充 workspace 扩展放到任意远端。

#### Scenario: 扩展需要目标不具备的 Node 或原生能力
- **WHEN** 用户在纯 Web 宿主选择仅有 Node 入口的 VSIX，且没有可用目标宿主
- **THEN** 明确说明所需能力和可选择的合法目标，不安装后伪报已可运行，也不在 Electron main 自动执行

#### Scenario: 目标侧 VSIX 安装与并发回执
- **WHEN** 用户通过已认证管理入口，以稳定目标及其挂接工作区的文件 URI 安装 VSIX，且另一安装先于本次 HTTP 回执完成
- **THEN** 原生目标按独立安装权限解析来源并交原安装器处理，本次回执中的安装版本与完整库存来自同一串行发布操作；其它目标不变，已有版本目录保留，安装不被表述为运行版本切换，断流不重放安装

### Requirement: Web 与目标侧 Node 宿主必须分别归属

browser 扩展 SHALL 在 Workbench 原 Web 宿主执行，需要 Node 的扩展 SHALL 在明确本地／远端后端目标侧宿主执行；Node 宿主不得由 Gateway 请求进程、Python Agent 或 Electron main 执行。Web 与 Electron SHALL 共用 renderer 贡献及目标协议；桌面仅承担已约定的窗口、生命周期及 profile。

#### Scenario: 浏览器与 Electron 连接同一远端目标
- **WHEN** 两种客户端分别激活 B 上的 Node 扩展
- **THEN** 代码在 B 的目标侧执行，两者各有自己的客户端运行视图；客户端机器、Gateway 请求进程及 Electron main 没有另起该扩展的本地副本

### Requirement: 多目标宿主必须保持明确工作区视图和代际

一个 Node 宿主 SHALL 绑定一个客户端会话、一个稳定原生目标及明确挂接的同目标工作区集合，集合可使用原 multi-root 语义。其它目标的 workspace MUST NOT 隐式进入该宿主视图。全局活动工作区或单一 remoteAuthority 的切换不能替代多目标身份。工作区集合变化及宿主重启 SHALL 通过明确生命周期和 generation 更新，旧 generation 不得接受新调用或发布迟到结果。

认证握手 SHALL 返回目标平台、原 mount 中的有序根映射及目标原生 profile／日志初始化位置；根映射保留资源 URI 与目标 file URI 的不同职责，不成为第二份权威登记。原生位置只在目标机器解释。workspaceState 的稳定视图身份 SHALL 包含原生目标、明确 view 和有序工作区集合，不依赖客户端会话、generation 或临时路由。

#### Scenario: 同屏 A 与 B 的同名工作区
- **WHEN** 一个窗口同时打开 A1、A2、B1，而 A 宿主只挂接 A1/A2
- **THEN** A 扩展看到的 workspace view 只有 A1/A2，B 扩展仅见其挂接集合，切换编辑器焦点不会修改任一宿主身份或泄露另一目标文件

#### Scenario: 重连消费目标生成的初始化位置
- **WHEN** 客户端为同一明确目标和工作区视图重新建立认证通道
- **THEN** 新 generation 沿原协议使用目标生成的根与 profile 位置，同一视图的持久状态身份保持；根集合不同的视图不得借用该身份，Relay 和客户端不把目标 file URI 解释为自己的文件

### Requirement: 内部扩展身份必须支持同一公开 ID 的多目标实例

系统 SHALL 在原扩展服务内以公开 extension ID 与稳定安装目标区别内部实例，运行身份进一步带 view／generation，并迁齐 description registry、enablement、activation、running location、依赖和卸载消费者。公开 manifest／API ID MUST 保持不变；仅增加 Remote kind 字段、忽略重复 ID 或借另一目标同名依赖均不满足此要求。不同目标的版本可分别运行；窗口级声明冲突 SHALL 在启用前按原贡献语义诊断并要求选择可共存版本或不同窗口，不能静默覆盖。

extensions.all／getExtension／exports SHALL 解析该 host 的合法实例集，不能按公开 ID 返回其它目标运行对象。

#### Scenario: 同扩展不同版本跨目标独立激活
- **WHEN** A/B 的同 ID 扩展使用不同版本但具有可共存声明，各自依赖同目标组件，随后 A 更新或停用
- **THEN** 两实例曾真实独立激活，A 操作只撤销 A 的实例与依赖引用，B 的版本、贡献和运行保持不变

### Requirement: Node 宿主文档与编辑器同步必须按视图投影

初始模型、dirty buffer、textDocuments、visibleTextEditors、activeTextEditor、文档／编辑器／诊断／watcher 事件 SHALL 按 Node host 明确 workspace view 及资源归属发送，不能广播全窗口模型。焦点在其它 view 时该 host 的 active editor SHALL 为 undefined；版本、取消及 generation 须贯穿请求和结果。跨资源 WorkspaceEdit SHALL 在副作用前核验全部资源归属、映射与版本，拒绝错误目标，不声称新跨目标原子事务。此 API 视图约束不等于 Node 进程的文件系统沙箱。

直接 `workspace.applyEdit` SHALL 在恢复 DTO 与进入原 BulkEditService 前核验整批 `resource`、`oldResource` 和 `newResource`，合法首项不能先于非法后项生效。该入口错误 SHALL 保留原日志与 `false` 返回合同。宿主代际关闭 SHALL 取消原编辑 token；取消后尚未完成任何资源时返回未应用，已经完成的资源 SHALL 保留原结果，不能用外层关闭标志伪装撤销已发生的副作用。文件操作能力缺失时 SHALL 明确拒绝，不以目标 Node 原生文件访问旁路工作区文件 owner。

TabGroups SHALL 复用原编辑器组布局，仅投影属于当前宿主的完整 EditorInput；组内增量索引基于投影后的标签顺序，保留原 Tab／TabGroup 的运行态对象身份。其它目标的活动标签 SHALL 表现为该组无 activeTab，实际 activeTabGroup 与 viewColumn 仍由窗口布局决定。混合资源 Diff／Merge／MultiDiff 的所有实际文件端点均属该视图时才可见，不能只展示部分文件却允许操作完整输入。尚无目标归属的无文件标签不进入 Node 视图，Web 原窗口语义保持。关闭组 SHALL 仅关闭该宿主的标签，组中仍有其它目标文件时不得移除真实组；关闭、移动及迟到事件不得越过所属代际或改投其它资源。

文档语言 Provider SHALL 在原注册表的候选评分与独占排序前核对宿主视图；目标 Provider 的 scheme、绝对 glob 和 RelativePattern 按目标原生文件位置匹配，继续复用原 selector／glob 合同，不能仅在错发 RPC 后拒绝 URI。多文档操作的次要文档 SHALL 使用同一注册项的归属及评分，不另存 selector 绕过目标限制。Web 原窗口评分、Provider 对象及取消协议保持。

#### Scenario: A 与 B 的未保存文档同时打开
- **WHEN** A/B 文件均有不同 dirty 内容，两个扩展枚举文档／可见编辑器并监听变化，用户在二者间切焦点
- **THEN** 每 host 只得到自己 view 的模型、内容和事件，非所属 view 焦点表现为无 active editor；错误目标编辑在产生副作用前被拒绝，迟到旧代际结果不发布

#### Scenario: 混合编辑器组的标签事件与关闭
- **WHEN** 同一真实编辑器组交错打开 A/B 文件，随后移动、切焦点或由 A 扩展关闭自己的 TabGroup
- **THEN** A/B 分别收到自己标签的连续索引和增量，未关闭标签的 API 对象身份保持；A 只能关闭自己的完整输入，B 文件及仍非空的真实编辑器组保留，宿主关闭后不再发送标签模型或继续处理后继关闭

#### Scenario: 直接编辑的整批准入与代际关闭
- **WHEN** A 的直接编辑先包含合法文本／文件项，后包含非所属资源；或编辑在预览等待及真实写入完成后的回执等待中遇到宿主关闭
- **THEN** 非所属批次在任何首项写入前拒绝；预览中关闭产生零写入并返回未应用，已经完成写入的回执保持真实结果，后续新编辑拒绝；原空批次、预览过滤与 Web 编辑范围保持

#### Scenario: 同语言 Provider 按目标原生位置匹配
- **WHEN** A/B 同时打开同语言文件，各自 Provider 使用语言、file scheme、绝对 glob 或 RelativePattern 注册，并交替请求结果或执行多文档高亮
- **THEN** 仅所属目标的 Provider 参与候选，模式匹配目标真实位置；外部目标的 exclusive Provider 不压制自身候选，多文档操作不向该 Provider 发送其它目标文档，切焦点不改变已发起目标

#### Scenario: 工作区符号查询限定完整宿主视图
- **WHEN** A 的符号 Provider 绑定 A1/A2，B 绑定 B1，用户明确选择工作区范围或 A 扩展在 B 获得焦点时发起公开符号查询
- **THEN** 完整选择 A1/A2 才调用 A，单选 A1 时在调用前说明缺少完整视图，不查询 A2 后再过滤；A 扩展的查询只进入原 A 宿主，结果与解析保留同一 Provider、缓存身份和 typed URI，取消或注销不发布迟到结果；原无 scope 的窗口查询及 Web 窗口级能力保持

### Requirement: 扩展启用范围与版本激活必须区别于安装位置

VSIX 安装位置 SHALL 与原 profile／workspace view 的启用状态分别表达；同一 multi-root view 不得伪报每文件夹独立运行隔离，需要不同启用集的业务工作区须使用不同明确宿主视图。版本更新、停用与卸载 SHALL 沿原宿主 reload／重启及引用规则，保留未结算旧进程／操作所需制品；安装新版本不得伪报运行实例已切换，操作失败不得改变其它目标的配置或版本。

#### Scenario: 共享安装但使用不同扩展启用集
- **WHEN** A1/A2 共享已安装版本但要求不同启用状态，且 A1 的旧宿主仍运行时安装新版
- **THEN** 用独立明确视图表达不同启用集，显示新版安装与 A1 待 reload 的区别，A2/B 的状态不被隐式修改；有引用旧制品不被删除

### Requirement: 同一扩展贡献必须按发起目标分派

扩展 SHALL 继续使用原 manifest、命令 ID、菜单／快捷键及 activation 合同，内部注册须保留真实宿主及目标来源。同一扩展在多个目标注册同一命令时 SHALL 根据发起编辑器目标分派；全局入口存在多个合法目标时明确选择并冻结。资源 Provider SHALL 按资源稳定归属路由，不按最后注册覆盖或显示名匹配。

每个 UI command ID SHALL 由单个窗口分派入口持有按来源登记的合法实例；扩展内调用优先继承原 host／view，activation 不向所有目标广播同名事件。TreeView／WebviewView resolver、setContext 与菜单状态 SHALL 保留 host／view 归属及明确展示绑定；dispose 按来源和 generation 撤销，只有最后来源消失才移除共享入口。静态贡献只登记一次，不重复注册互相覆盖。

#### Scenario: 同一命令 ID 在 A 与 B 同时注册
- **WHEN** A/B 安装相同扩展，用户从 B1 编辑器执行命令后立即切到 A1
- **THEN** 只调用 B 宿主并读取 B1 资源，不因切焦点、注册顺序或同名文件改投 A；无发起目标的同一命令先选择目标

#### Scenario: 扩展内调用与其它目标撤销贡献
- **WHEN** 窗口焦点在 A，B 扩展调用同 ID 命令并提供同 ID TreeView／WebviewView，随后 A reload 并撤销其贡献
- **THEN** 命令只进入 B，视图与上下文按明确目标绑定；撤销 A 不删除 B handler、resolver 或状态，B 的菜单不被 A 的 setContext 覆盖

### Requirement: 扩展 API 必须使用实际目标服务并公布能力边界

系统 SHALL 为文档／语言、文件与 watcher、工作区与配置、终端／任务／调试、Webview／端口、依赖及存储公布实际支持能力。支持的 API SHALL 保留原扩展 API 语义并访问真实目标；缺少能力须明确拒绝，MUST NOT 使用空成功、本地 IndexedDB 同名文件、浏览器 localhost 或其它宿主替代。权限与能力读取不得触发 Agent 或隐式连接其它目标。

目标 Node 扩展取得的文件 URI／fsPath SHALL 可供该目标原生文件访问，跨宿主传递须沿唯一映射恢复原工作台稳定资源身份；不得向 Node 暴露只在浏览器 VFS 有意义的假路径或全局替换任意字符串。

嵌套根 SHALL 沿原 folder 最长匹配规则；同一本机路径对应多个业务身份时须由已有请求／文档上下文消歧，无唯一结果则明确拒绝，不能任选根或另建路径注册表。

#### Scenario: 远端扩展打开终端并读取配置
- **WHEN** B 扩展通过受支持 API 打开终端、读取 B1 配置及文件，随后请求一个未支持的调试能力
- **THEN** 前三者由 B 的真实服务处理，未支持能力明确失败；没有在 A、浏览器或 Electron main 创建替代终端或读取同名资源

#### Scenario: Node 扩展原生路径与编辑器资源往返
- **WHEN** B 的真实扩展分别使用 Node 文件 API 与扩展文件 API 读取 workspace 文件，再返回该文档位置
- **THEN** 两次均读取 B 的真实内容，位置映射回 B 的同一编辑器身份；A 同名路径及浏览器内部虚拟根不被读取

### Requirement: 宿主恢复与持久存储必须保持目标隔离

宿主 SHALL 惰性创建并按有界客户端租约回收；断开、崩溃、后端重启及权限失效须停止新准入、撤销旧代际贡献并报告实际原因，不自动重放未知副作用。安装记录及持久状态不随宿主关闭删除。扩展全局状态 SHALL 按 profile／原生目标／扩展隔离，工作区状态进一步按稳定工作区视图区分，不以临时客户端 ID／endpoint 作为持久身份；凭据留实际消费侧。

MainThreadStorage／ExtensionStorageService 等原存储 owner SHALL 迁齐上述身份、持久写入及变更通知，不能只改调用方 key。Node 的 globalStorageUri／storageUri／logUri／extensionUri SHALL 对应目标本机有效资源，包目录与可写状态分开；同身份客户端并发沿原存储写入／通知语义，不伪报逐字段事务合并或共享瞬时回调。

#### Scenario: 两个客户端使用同一安装后分别关闭
- **WHEN** 两个客户端在 B 激活同一版本扩展，关闭其中一个并重启另一个
- **THEN** 关闭者的回调／工作区视图按租约回收，不撤销另一客户端贡献；稳定持久状态可恢复，A 的状态和秘密不被读取

目标 Node 的跨设备 `setKeysForSync` 尚未支持时 SHALL 在原公开 API 明确拒绝；同 profile 客户端的存储变更通知及重启恢复不等同跨设备同步。Web 原同步能力保持，后续接入须迁齐原同步读写消费者，不得仅保存声明后报告成功。

目标侧秘密 owner 尚未接通时，Node 的 SecretStorage 四类操作 SHALL 在秘密进入浏览器 RPC 前明确拒绝；主线程也不得读取、修改或向该宿主广播 Web profile 的秘密。此限制不表示目标秘密支持已完成，后续实现必须在实际消费侧保存秘密，Web 原有 API 保持。

宿主初始化中的文件根 SHALL 复用原文件 owner 的规范 display 根，与 Workbench 文件模型使用同一身份；Gateway 归属由认证 binding 与 FileService URI 的 authority 承载，不得因重复编码 Gateway 而形成无法匹配的第二种根。

#### Scenario: 存储路径与同身份客户端同步
- **WHEN** 同 profile 的 A/B 扩展写各自 globalState 和真实 storageUri，B 的另一客户端读取并重启
- **THEN** 目标文件真实可写，A/B 不串值；B 的已确认持久结果按原通知和恢复语义可见，临时 host ID 变化不丢失状态

### Requirement: 扩展管理必须通过原生目标管理权限

远端安装、更新、卸载与 Node host 管理 SHALL 由原生目标校验相应能力，使用稳定登记和真实路由。失效目标、route 被其它目标复用、权限撤销与 API 版本不兼容须明确拒绝，不落到当前活动目标。实际安装／版本及引用 SHALL 由目标安装服务持有，Relay 与前端不得形成第二事实源。

#### Scenario: 宿主地址被复用
- **WHEN** 已安装目标 B 的旧连接地址被另一个目标接管，用户从历史入口激活 B 扩展
- **THEN** 按稳定身份重新核验并拒绝不匹配连接，不向新目标发送扩展代码、凭据或命令

#### Scenario: 实时宿主发现不触发启动
- **WHEN** 客户端对明确目标及workspace集合读取宿主状态，目标缺少专用runtime、平台未支持或正在排空
- **THEN** 由原生Gateway按宿主管理权限读取Backend唯一owner，返回实际稳定身份与具名通道不可用原因，不启动Backend或宿主；离线和路由失效明确失败，不以登记镜像冒充实时状态，通道可用也不代表完整扩展API已可用

### Requirement: 发行产物必须独立提供工作台资源与可验证宿主链

Web 与 Electron 发行产物 SHALL 包含其所需静态资源与扩展资源定位，不依赖开发服务器或源码目录。目标侧 Node 宿主 SHALL 使用与固定上游及平台匹配的实际 runtime／组件，不借 Electron 内置 Node 假充远端能力。真实扩展激活及文件／命令操作、重启状态恢复必须分别通过产品入口验证，进程启动不能作为功能完成证明。

#### Scenario: 打包 Electron 在源码目录外启动
- **WHEN** 关闭开发服务器，将桌面产物移到独立目录后连接 B 并激活测试扩展
- **THEN** 原界面资源、Web 扩展及目标 Node 操作均按真实支持能力工作；重启恢复稳定配置及状态，没有读取源码路径或启用额外原生 IPC
