## ADDED Requirements

### Requirement: 复制引用由统一寻址 owner 明确 Gateway 归属

Gateway 资源地址解析 SHALL 在保留原 `uri`、文件根及定位字段语义的同时，返回包含明确 Gateway 的 `reference_uri`，供复制和跨工作区／Gateway 的草稿引用消费。两者 SHALL 使用同一个资源 URI grammar 构造与解析；客户端只反转已有内部编辑器投影，不拼装公开 URI 模板。该字段不表示资源存在、可读、已授权或已固定版本，引用不自动变成上传附件或激活上下文来源。

#### Scenario: 本机简称与远端来源的复制

- **WHEN** 用户复制本机简称或带明确远端 Gateway 的资源
- **THEN** 返回的引用携带其原 Gateway，接收方不会把简称解释成自己的本机资源；原非法 URI 仍由同一 grammar 拒绝

#### Scenario: 远端后端返回相对于自身的简称

- **WHEN** 编辑器已有明确来源 Gateway，而原后端资源地址使用无 authority 简称
- **THEN** 地址解析分别核对发起 Gateway 身份和明确来源归属，按来源 Gateway 解释该简称并生成完整引用；URI 的显式 Gateway 与来源冲突时拒绝，不改投发起机器

### Requirement: 联邦上下文查询复用原来源与分页

工作区搜索 SHALL 提供独立 `context/content` Provider，委托原上下文查询 owner，保留 `effective_context`、`session_catalog`、`session_information` 来源、literal／regex、大小写、字符预算及原 revision／cursor。省略来源仍为有效上下文。该类别 MUST NOT 用 canonical 消息正文、文件或日志搜索替代；精确单资源读取及 inventory 继续沿原接口。

联邦请求 SHALL 将来源和字符预算作为 typed 领域选项传递，由明确 owner 生成工作区上下文资源身份和 URI，不接受调用方把另一工作区资源塞入该分支。命中 SHALL 保留原资源身份、规范 URI、来源、record_index、码点区间和独立版本；分支保留原页续点及受限说明，不合成跨库版本或全局正文偏移。Gateway 仍只处理范围与调度，不读取上下文存储。

#### Scenario: 无浏览器搜索有效上下文

- **WHEN** 工具或脚本在明确工作区集合查询产品执行产生的有效上下文
- **THEN** 同一 Gateway 调度返回各 owner 的原上下文页，可沿原资源读取入口按版本精确读取，未选工作区不参与查询

#### Scenario: 上下文续页与来源变化

- **WHEN** 沿原范围续查，或把旧 cursor 用于不同来源／查询／版本
- **THEN** 前者保留原 owner 的分页结果，后者按原合同明确拒绝，不悄悄重查或换成普通消息搜索

### Requirement: 上下文非法正则由原查询 owner 在读取前拒绝

上下文查询 owner SHALL 在读取候选、会话或正文前编译显式正则，零候选工作区同样拒绝非法表达式。直接上下文 HTTP 与统一搜索 HTTP SHALL 返回 400，保留 `owner=session_context_query`、`code=invalid_regex`、`reason=invalid_search_pattern` 和正则解析器的详细错误；联邦搜索分支 SHALL 保留相同归属、代码、原因及原 request_id，并明确不可重试，不将输入错误改写为 `owner_rejected` 或目标不可达。

精确资源的上下文 relay SHALL 以原 owner detail 传递该拒绝；客户端仅在原 request_id、owner、code 和 reason 均匹配时接受该具名错误。Agent 工具 SHALL 返回错误状态与结构化原诊断，不依赖自然语言判断错误类别。合法正则、原 cursor 与 revision 冲突合同不变。

#### Scenario: 空上下文库存仍拒绝非法正则

- **WHEN** 没有会话的工作区收到非法正则查询
- **THEN** 原 owner 在开始读取库存前拒绝；直接 HTTP、联邦 SSE 和 Gateway inventory JSON relay 均保留原诊断及请求身份，不交付结果页或提供无效输入重试

### Requirement: Gateway 上下文搜索采用唯一分支合同

Gateway inventory 范围的 `search_context` SHALL 经既有上下文 `read` grant 传递类型化 `GatewayContextSearchRequest`，在目标 Gateway 调用与普通资源搜索相同的范围解析、调度、期限和输出准入；跨 Gateway 时保留原 origin、目标、通道及授权约束，不转投发起 Gateway 或活动工作区。该授权只接受 `context/content`，MUST NOT 据此执行文件、消息或其它类别查询。

该入口 SHALL 返回包含原 `scope/page/branch/end` 事件的 `events` 对象。首次省略 `search_scope` 表示明确 inventory 所属 Gateway 的全部登记工作区；显式集合、发起 owner 和冻结范围继续使用共享范围合同。续查 SHALL 传回冻结 `search_scope` 和所需分支 `continuations`；Gateway 范围不再接受旧全局 `cursor/revision`，也不合成跨库版本、总命中数或正文偏移。旧上下文 relay 的 `search` 分支不再承载 Gateway 聚合，调用方须使用 `resource_search`；精确 Workspace／Session 的原请求与返回结构保持不变。

Gateway 搜索的 `max_chars` SHALL 表示每个 owner 原上下文页的字符预算，总命中和事件字节另受统一联邦配额约束。JSON relay 与 SSE SHALL 复用相同的整页准入、原续点和终态预留；客户端复用同一事件顺序、归属及唯一终态校验，不另实现聚合或正文匹配。原 Gateway inventory 读取和精确资源读取 SHALL 继续保留。

#### Scenario: 原 Gateway 工具独立续查

- **WHEN** 真实执行调用 Gateway inventory 的 `search_context`，随后在 Backend 重启后只续查其中一个 owner
- **THEN** 工具取得原有效上下文与规范资源身份，返回原冻结范围及独立 cursor；后续请求仅执行明确分支，不从头重扫其它工作区，不构造全局 revision

#### Scenario: 一个 owner 的响应无效或随后恢复

- **WHEN** 一个 owner 返回非法资源 URI 或无效页，而其它 owner 正常，随后该分支恢复
- **THEN** 无效分支返回可判别错误，健康 owner 的页及版本保留；完整异常进入原诊断日志，日志写入失败也不能把查询改成空成功或阻止资源释放；恢复不改变健康 owner 的独立 revision
