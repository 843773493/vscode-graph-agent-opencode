## Context

动机见 proposal。统一寻址已拥有本地 grammar/resolver，但当前 Skill 固定入口形态、整体百分号拒绝和工作区文件工具的路径解释无法构成完整导航合同。附件与工具输出仍生成 `boxteam-session://`。公共根/编码的唯一规范归统一寻址 change；Skill 激活归 context change，通信目标归现有 Session change。

## Goals / Non-Goals

**Goals:** 一个文件 owner 消费一个 canonical target；本地/远端相同操作合同；普通文件无需逐项注册；失败明确体现真实写入效果。

**Non-Goals:** 不将终端、浏览器、团队变成文件系统，不新建跨端执行平台、通用任务账本、文件快照库或事件订阅。首批迁移既有文本补丁的新增/修改/删除/同根文件移动；新增通用目录 mutation、二进制通用写入和跨端复制独立定义。

## Decisions

### 1. 一个内部目标，按资源形态分派

read/write 的文件目标、ls/glob/grep 的根目标和 apply_patch 的根目标均接受相对路径、绝对路径或资源 URI，引用统一寻址 owner「普通文件入口必须保留原生路径输入并统一目标解析」；查询条件与目标分开，patch 内部文件名仍只允许相对已固定根的路径。先固定目标 Gateway/机器和工作区或选定目录，再由目标端统一解释输入并交同一文件 owner 执行；相对路径不能借用 source/relay 进程当前目录，绝对路径按目标平台（含 Windows 盘符）解释。工作区内路径可归一为已有 URI，工作区外路径按既有能力/授权处理，不得偷偷拼入工作区、转到本地同名文件或要求先注册工作区。

原生路径是具名请求输入，不能因此进入内部持久资源引用、普通响应或跨 Gateway 资源投影；无已有 URI 模板的外部目标沿既有 owner/API，不自造 kind 或第二操作链。拒绝万能目录映射：领域对象不暴露磁盘树，Skill 目录读取不调用 CSM。终端/shell 保留原生路径语义，Gateway 原生目录选择/注册的管理边界不限制普通文件绝对路径输入。

未注册目录的文件树浏览、编辑与快捷目录保留，由统一寻址 owner 先补齐规范引用，消费者同批迁响应、持久化与重开，不私建模板、不借用工作区身份、不以注册为访问前提。真实 UI 验收需核指定目录内容、编辑实际文件、重开快捷目录和工作区注册表不变，不能用改为只读或删除目录入口来消除真实路径字段。

2026-10-20 本地切片已删除 `workspace_tool_paths.py::_absolute_to_workspace_relative()` 及其 `path.lstrip("/")` 歧义回退。`core/path_utils.py::resolve_native_file_path` 是目标机器原生路径的统一解释入口，显式绑定根；Web 文件 owner 按既有能力允许外部绝对路径，当前模型文件/调试工具保持工作区能力，外部目标明确拒绝。Workspace files URI 使用公共 grammar、只读既有工作区身份，再交同一文件 owner 和内部操作链；解析不创建身份。补丁内部文件名明确限制为固定根下的相对路径，不能从普通文件目标合同推导绝对文件名。`backend_virtual_to_workspace_relative` 等转换仅消费内部 backend 输出；原虚拟根断言已与真实用户绝对路径测试分开。完整 URI 输出、技能导航、远端执行和会话产物仍按后续任务迁齐，旧 `boxteam-session://` 的生成/消费统一在第5组完成后删除。

### 2. 实际 owner 就地执行，路由不是持有数据

工作区文件操作由目标工作区 owner 执行；Gateway/inline 技能由各根所属 owner 提供内容能力，不让 Workspace 读取对端真实路径。复用 federation channel、认证和有界星型路由，分别登记文件操作授权，保留既有 send/read/wait grant 的边界。固定 target 与单次 channel lease 分开；断线不可重新裸 ID 选目标。选择沿现有路由扩展，避免另一套远程文件服务或全局业务数据库。

模型只读工具现在由同一 ResourceNavigation 在任何本机文件解析前检查目标归属：远端 authority 或另一个 Workspace 交已配置 Gateway 的已注册 Workspace 服务入口 `/api/gateway/resource-navigation/workspaces/{source_workspace_id}`，不经激活工作区选择、不直连对端 backend。该入口验证本机服务凭据及来源 Workspace 注册，不要求或伪造浏览器 Cookie；公开导航入口仍保留原用户登录要求。省略 authority 的其它 Workspace 固定为本次调用 Gateway，再传完整 URI；Gateway 继续用既有 typed ResourceNavigationGrantTarget/read grant 和星型 channel 到实际 owner，通信 grant 不能作为文件目标的签名替身。Gateway 与 Workspace 侧共用同一响应归属校验，核根形态、scope/kind、子路径及每个匹配对应的文件节点，并返回可重开的限定 URI。同步/异步传输各自使用独立 HTTP client，不建立观察/activation 状态。

联邦 deadline 取请求预算与既有联邦上限的较小值，不能在进入联邦时重新获得固定长预算；source 工具和导航 owner 的总期限继续包含解析、传输及最终序列化。错误保留原文件执行、寻址与联邦域及同一 request_id，不把目标拒绝改为本机回退。只读工具沿 read grant 执行；条件文本修改使用下述独立 grant，会话产物继续按第5组迁移。

2026-10-20 条件文本传输直接复用 `/api/gateway/federation/relay`、原 SSH channel 和星型路由。`TextMutationGrantTarget` 以闭集字段绑定固定 Gateway、Workspace files URI 和 `mutation_kind=text`，只能签发 `file_write`；原 send/read/wait/reply/fork target 不能借用该能力。签名同时覆盖正文和预期版本，每个实际路由/执行点读取最新策略；默认值沿既有联邦策略，显式 read allow 不隐含 write allow。source Workspace 必须已注册且属于来源 Gateway，目标 Gateway 只将请求透明交固定工作区后端，联邦 HTTP 凭据不能直接访问公开文本修改接口绕过 grant。

create/edit/patch 传输 DTO 与公开 proto 同步，封套只接受固定 Workspace files URI；普通工具原生输入仍由其明确目标 owner 解释。目标后端调用容器内同一个 `WorkspaceTextMutationOwner`，复用原字节 preflight、单文件发布和观察 journal，不复制补丁执行链。目标原件与后件经归属、逐路径预期版本及回执身份校验后，保存到 source 所属会话 journal；SessionChanges 不读取 source 同名文件。请求上限1 MiB，回执上限8 MiB；超限在目标 preflight 可确认时零写入，否则诚实返回 unknown。目标失联、丢响应或畸形回执均保留原目标和前置版本，不猜 diff、不自动重放。原生目录根远端写入、远端原生路径输入及 Windows 验收继续由对应任务闭包处理，取消确认仍保留具名 TODO；线程取消等待不代表文件发布已停止。

### 3. 轻量读取，不伪装历史快照

目录/搜索返回 URI、shape、可用操作、相对显示名及有界匹配。使用显式 limits/deadline 和与目标/查询绑定的 opaque continuation，普通可变视图披露变化，无法继续时明确失败，不承诺固定历史分页。只为实际读取/条件写入计算原始 byte SHA-256；不使用 mtime 冒充内容版本，不为搜索建逐文件 ResourceIdentity。上下文追踪继续进入原 source owner。

只读导航的首个切片在共享 `ResourceNavigation` owner 内设置单次总期限、扫描节点、单文件字节和序列化响应字节上限。目录和路径搜索返回 `view=live`；续页绑定 Gateway、规范 URI、操作、查询和限制，校验已消费前缀的节点及 stat metadata，变化时明确拒绝。此摘要只用于检测可变视图变化，不是内容 revision 或历史快照。扫描超限要求缩小范围；条目或正文预算截断会披露原因，目录/搜索可续页时返回 cursor。HTTP 层还核验包含请求回执的最终响应，不能容纳时返回文件执行域的 `output_limit_exceeded`。递归扫描通过 `scandir` 在排序前限制枚举量，不登记来源或 activation；普通模型工具、内容搜索和跨 Gateway 全程预算仍待后续任务接通。

模型 `ls` 首先迁入该共享 owner，工具返回同一有界导航 JSON，包含节点 URI、`entries`、`truncated`、`truncation_reason`、`next_cursor` 和 `view=live`。相对路径与原生绝对路径复用已装配的 `WorkspaceFilePaths`；URI 交公共 grammar/resolver。Container 将唯一导航 owner 经 runtime provider、Agent factory 和 middleware 传入，不按 thread 重建。同步和异步工具共用本地文件执行内核；同步 HTTP 使用独立同步 client，异步 HTTP 使用独立异步 client，普通 Gateway 导航不建立来源观察 state，也不借用长期 watcher 的事件循环或连接。工具期限覆盖输入解析及结果交付；原生阻塞 I/O 不能被取消强制中断，超期结果不会伪报成功。

工具声明包含 `filesystem-ls:bounded-uri-navigation`，图绑定遵守既有精确匹配合同，不保留旧 ls 回退。文件 URI 可继续交共享 read_file，Skill 目录 URI 可继续 ls；glob/grep 和写入迁移仍待各自切片完成。

后续 `read_file` 切片将普通文件读取交同一 `ResourceNavigation`，物理删除旧 DeepAgents 普通读取调用。公开请求的 `read_format=text|auto` 默认严格文本，auto 仅识别既有受支持媒体扩展名，不把未知非 UTF-8 字节猜成媒体；结果公布 `content_encoding`、`media_type`、`mime_type`。文本保留原始 UTF-8/BOM/换行，支持一索引 `line_offset` 与 `max_lines`，逐层披露行数、文件字节或响应字节截断；字节窗口包含跳过的行，起点超过窗口时明确要求增加预算。媒体始终返回完整 base64，原件或含媒体 block 的最终工具回执超过预算均明确失败，不生成残缺媒体。

导航的既有默认字节预算不变，显式单次读取上限为32 MiB、响应上限48 MiB；模型读取默认分别为16/24 MiB，并以10秒总期限覆盖解析、目标执行和工具投影。格式化后的行号、截断提示及媒体 metadata 计入最终工具响应。普通 Skill URI 的 ls/read 不激活来源；提示词撤销普通读取禁令，`skill_load` 仍独占激活。现有工作区读取规则对相对/绝对/URI输入一致生效，附件和工具输出仍交独立原业务 owner，旧产物地址的全量迁移继续归第5组。此次工具面登记为 graph revision 9，加入 `filesystem-read:bounded-uri-media`；旧 selector 明确拒绝，未保留旧图回退。

普通模型 `glob/grep` 随后迁入相同导航 owner，删除 DeepAgents 普通 glob 调用、本机 rg 子进程及其无界输出解析。已有 `search` 操作显式区分 path/glob/content，默认仍是原路径包含查询；glob 模式只匹配文件，grep 为固定字符串按匹配行分页，单文件目标也能搜索。匹配公布规范 URI、一索引行列、片段起始列及 UTF-8 字节有界片段；同一文件跨页仍绑定原目标、查询和限制，前缀变化明确拒绝，不建立持久搜索快照。已知媒体和含 NUL 二进制跳过，未知非 UTF-8 文本、文件字节超限和扫描超限明确失败，不能冒充无匹配。内容递归沿既有规则排除 .git/node_modules/.boxteam；工具结果过滤既有拒绝读取规则，并核最终 ToolMessage 字节预算。grep 的 count 只计本页匹配行，截断和 cursor 明确披露。

搜索工具面登记为 graph revision10，旧 selector 仍明确拒绝。同步/异步共用搜索实现时保留 callable 类型标注，确保真实 LangGraph runtime 注入和装配可用。旧会话产物搜索仍交原 SessionArtifactBackend，待第5组生成/消费全迁后物理删除。wcmatch 已是固定 DeepAgents 依赖；登记直接依赖时根包动态 metadata 因 node_modules 重复嵌套路径失败，保留具名 TODO，未为此修改环境或构建配置。多页重复扫描优化继续按用户要求延期。

读取版本切片将完整原始文件字节的 `sha256:<hex>` 作为公开 `content_hash` 返回，并写入文本 read_file 的模型可见正文。BOM、CRLF、空文件及媒体均按原字节计算；行数或展示响应截断仍可提供完整原件版本，字节窗口未读全则不提供版本。读取前后核对 inode、mode、size、mtime 与 ctime，发现变化明确报 `file_changed`；这是读取时的变化检测，不承诺阻止读取后的外部并发。hash 和提示计入最终工具回执预算，不注册持久快照。工具能力面登记为 graph revision11 的 `filesystem-read:raw-byte-hash`，旧 revision10 明确拒绝。后续写入必须使用该版本或明确的 expected_absent，不能从执行时当前内容补造调用者预期。

### 4. 条件写入与多文件发布结果

write/edit/patch 的内容前置使用精确原始 byte `sha256:<hex>` 或该 owner 已有等价 revision；创建使用 expected_absent。owner 在一个根内串行自己管理的 mutation，先校验所有 preimage/能力/patch、准备结果，再在每次发布前重新核当前 preimage；同目录临时文件经完整写入和原子替换发布，失败回收自己的句柄和未发布临时文件。

既有 `*** Delete File` 和 `*** Move to` 同步接入该 owner。删除校验原文件的预期内容版本；移动的 source 和 destination 必须同根，分别绑定 source 版本及 destination 的 expected_absent 或明确预期版本，不能无条件覆盖目的文件。preflight 校验所有受影响路径，包括移动的两个端点。移动目的发布与源删除是两个可观察步骤，不伪称两路径原子事务；结果逐路径记录存在状态、已核 hash 和执行知识边界，目的已发布但源仍在时不得报移动完成。目录、二进制一般节点的 move/remove 与递归操作不由文本补丁能力推导。实施须复用既有 add/delete、hints/move 正式用例，保留补丁输入和内容/changeset断言，只迁目标和条件写入合同。

普通操作系统不提供针对任意外部写入者的跨文件 CAS/事务。owner 串行化只约束本服务的写入者；外部修改需前置重核和发布后核实，不能宣称关闭所有外部竞争窗口。能力不足的目标明确拒绝相应保证，不添加假 CAS。单文件不暴露半截正文；跨文件不假装全局原子事务。遇到失败立即停止，逐文件报告 published/not_published/not_attempted/unknown 与验证到的 hash；不以自动反向补丁覆盖后来写入。对于要求更强外部 CAS 的场景，另设可验证机制后再公布能力。

2026-10-20 目标执行入口继续收敛为容器装配的 `WorkspaceTextMutationOwner`，通过 runtime provider 同时交 write/edit middleware 与 apply_patch。工具只把原公开字段投影为具名 create/edit/patch 请求，并传入本次权限检查；精确替换和创建规则位于文件 owner，V4A 仅准备整批路径与内容，不再自行创建工作区执行上下文。独立工具测试也显式装配同一个 owner，不保留可选参数构建的旧入口。此结构切片不代表远端授权、传输或取消验收完成。

### 5. 执行结果与寻址拒绝分开

后续变更记录切片将 journal 扩为文件 owner 在锁内采集的原件及最终观察到的后件，SessionChanges 不再在延迟事件消费时重读当前文件，也不解析实际宿主路径。它仅按规范文件 URI 聚合和保存 owner 提供的内容；后件不可读或非UTF-8时，在工具逐路径回执和journal中保留观察错误与未知效果，不从本机同名文件猜造diff。changeset、审查请求和审查回执中的 file_path 统一承载文件 URI；移除旧绝对/虚拟路径混用的定位回退。原内容、操作类型、调用/执行/Turn身份及审查行为不变，不迁移旧开发记录、不留双读。这为后续远端写入记录提供正确消费边界，不能代替远端执行验收。

2026-10-20 普通 write_file/edit_file 与 apply_patch 已统一到 `workspace_files/text_mutation.py`，旧 DeepAgents 普通写入调用与补丁私有 journal 已删除。write_file 只做显式 absent 创建，edit_file 使用先前 read_file 的原字节 hash 做精确替换；字符串不唯一须显式 replace_all。三者均在同一短锁内校验全部文件的写权限与预期版本、捕获原件、准备结果后发布；Web 保存继续共用该锁及底层发布原语，保留其同步 expected_revision 合同。SessionChanges 仅从该锁内 journal 获取原件和 published/unknown 路径，失败不伪报成功；有会话的 journal 由统一解析器存入所属会话 changes/text_mutations，独立工具测试仅使用其显式工作区缓存。原字节/BOM/换行保留，普通响应仅含 URI。工具能力面登记 graph revision13，旧图明确拒绝；远端 mutation、取消确认和丢响应仍按独立任务验收。

2026-10-20 条件补丁切片已将原执行与自动回滚链物理替换为共享 mutation lock、原字节 preimage 和单文件发布原语。`expected_versions` 必填且精确覆盖补丁所有相对路径，移动包含源和目的；每项取先前读取的 `sha256:<hex>` 或明确 `absent`。固定根支持工作区相对路径、目标原生绝对路径和 Workspace files URI，补丁内仍只允许根下相对文件名；整批先校验版本、文本、路径别名、祖先冲突和现有父目录，再写 journal 和逐路径发布。新文件使用独占创建，已有文件原子替换，删除后核实不存在；保留 BOM、纯 CRLF 与原文件 mode。失败停止后续发布，不恢复已发布文件；最终回执给出 `published/not_published/not_attempted/unknown`、预期/目标/实际原字节版本，`retryable=false`。相同字节的外部竞争及发布后恢复原件不能冒充已确认发布或确定未执行。事件流即使收到失败回执，也把已发布或未知路径交原 SessionChanges owner，未尝试路径不进入 changeset；旧 `verification` 第二回执和反向恢复实现已删除。异步工具保留原事件循环上的生命周期回调，仅文件执行交工作线程，避免同步 StructuredTool 在另一个事件循环等待工具开始身份。远端 write/edit/patch 随后通过上述同一 owner 与独立 file_write grant 接通；取消确认继续后续实施。

寻址三个错误域引用统一 owner；文件执行原因只在 resource-file-operations spec 登记。确定拒绝和 preflight 失败为未执行；发布完成才为完成；中途失败为部分；无法观察实际效果为未知。调用 cancel 与确认停止分开，source deadline 不抹去目标可能的写入。未知结果先对原目标核实，禁止自动重放。复用现有工具调用身份记录诊断；不预设所有文件操作需要新 operation_id 或 durable coordinator。

### 6. 会话产物仍是业务文件引用

附件与工具结果在 session 模板下公开单文件 URI（模板唯一登记于寻址 owner），保留原业务 ID 和 owner。当前工具输出物理文件名为tool_<digest>.txt，不能把它当成通用身份段；没有独立result ID时由原owner基于现有稳定tool_call_id派生/公布规范引用，物理文件名留在owner内部，不新增通用身份注册库。迁移清点生成器、store/parser、middleware、大输出引用、消息附件、Provider 和 Web/历史 projection，完成后删除旧 scheme 常量/分支。现有附件二进制读取通过原接口保留，不把它扩成通用文件写入能力。不迁旧开发生成数据、不暴露会话私有目录。

现有 `read_file` 经 workspace filesystem backend 返回图片媒体 block，Provider 侧已有 `project_tool_message_content`；Web 的附件 content 接口经 `AttachmentBlobStore.read/read_thumbnail` 返回原件或缩略图。附件的这两条消费者统一从资源 URI 分派到原附件 owner，不能让普通文本的 UTF-8 要求废除既有媒体读取。`original`/`thumbnail` 保留原 owner 参数，URI 只描述同一附件身份；不新增 attachment 工具、URI query/view 解析器或绕过 owner 的 SessionArtifactBackend 私有目录映射。迁移后 workspace 文件读取同样保留既有受支持媒体能力，仍归其工作区文件 owner；首批新增写入仍仅文本。

## Risks / Trade-offs

- 文件名编码与现有身份 profile 混用 → 公共 grammar 先交完整构造/解析往返与拒绝矩阵，所有消费者只引用，不再加 parser。
- 远端路由虽正确但文件仍在 source 执行 → 两端同名不同内容/标记文件验收，验证远端修改及 source 不变。
- 原生绝对路径被当作虚拟根重定向 → 用外部实际文件和工作区内同名对照文件，从读写入口核实实际对象与零误写；覆盖相对/绝对/URI 同文件、不同 relay 当前目录以及 Windows 目标机器解释。
- 外部编辑器竞争/进程崩溃 → 诚实披露 CAS 能力边界和部分/未知效果，以原目标内容核实，不伪造回滚。
- 改进 Skill 导航误增 activation → 读取前后对 catalog/source-control/activation 记录逐项断言不变，显式 skill_load 才进入来源事务。

## Migration Plan

1. 先完成统一寻址的根、路径编码和能力登记；现有 context 规范删除通用读取禁令但保留唯一激活 owner。新增任务不因规划已写而勾选。
2. 先落一条技能目录只读纵向链路，再通过同一合同实现目标端读取/搜索，验证真实远端与固定目标。
3. 接工作区源码根和条件写入/补丁；同批迁齐三种目标输入及用户/内部虚拟路径边界，删除绝对路径歧义回退；验证实际目标、外部路径不重定向、冲突、preflight 零写入、部分发布、断线未知及重开核实。
4. 平移会话产物全部直接和间接消费者、协议与测试；删除旧解析链与 scheme，开发中间数据按已授权清理。
5. 按 tasks 逐项验收。公开破坏性改动同闭包一次交付，不保留旧链兼容回退；若候选失败则不发布该候选，已发生的目标写入按真实 owner 结果处理。

未注册目录引用及控制面根持久化例外只消费统一寻址 owner「原生目录根绑定必须由目标 Gateway 唯一持有」；目标端复用既有文件 owner。本 change 已开始本地原生路径解析切片，当前证据只覆盖 tasks 登记的行为，不代表全部文件工具迁移完成。
