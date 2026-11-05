# structured-user-message-attachments Specification

## Purpose
为多 provider 会话提供可持久化、可投影且可展示的结构化用户消息附件模型，使模型输入、checkpoint 历史和 Web 用户界面共享同一组附件身份但使用各自适合的 content block 投影。
## Requirements
### Requirement: 用户消息必须以有序 block 序列作为 canonical content

系统 SHALL 将用户消息的文本、附件说明和可选的 provider 内容 block 作为有序序列独立保存和遍历。任何包含多个 block 的用户消息不得在持久化或历史投影阶段整体 `json.dumps` 为一段用户可见文本；未知但结构合法的 block SHALL 原样保留，不能因为 Web 不认识它而丢失模型历史。

#### Scenario: 文本与图片 block 混合

- **WHEN** 用户消息包含一段文本、附件路径说明和一个 `image_url` 预览 block
- **THEN** canonical content 保持三个 block 的顺序，文本提取只读取文本 block，附件预览不变成用户消息正文中的 JSON 字符串

#### Scenario: 未知 provider block

- **WHEN** 用户消息包含当前 Web renderer 未定义的 provider 扩展 block
- **THEN** checkpoint 和 rollout 保留该 block 的原始字段，Web 只在自己的可见投影中忽略或标记它，不修改 canonical content

### Requirement: 附件必须保存原件并提供受界限的预览变体

系统 SHALL 将上传原始文件交给工作区附件 owner，以独立目录资源身份保存可靠原件版本及可处理工作目录，上传自动关联发起 Thread。工作目录的保留、释放及重建 SHALL 遵循 managed-attachment-directories：最后关联解除且无占用时，有修改保留、无修改释放可重建工作副本；释放 MUST NOT 删除可靠原件或改变附件身份。系统 SHALL 为需要预览的类型按稳定文件引用生成可重复读取的预览变体；图片预览最长边 SHALL 为 `min(512, max(原始宽度, 原始高度))`，不得放大原图。自动预览 SHALL 不被当作需要长期保留的用户处理结果。

附件引用 SHALL 至少包含稳定 id、文件名、MIME 类型、原件资源 URI 和目录 URI，模板由统一 URI owner 定义。工作目录已正常释放后，普通查看、原件/预览读取及文件操作 SHALL 经同一附件 owner 重建，不要求重新上传。原件真实缺失/损坏 SHALL 明确失败。其它文件类型 SHALL 按通用附件保存，不因没有专用预览器伪造预览成功。模型使用过的预览和正文版本 SHALL 遵循冻结历史合同，MUST NOT 从当前目录重新生成旧输入。资源引用使用 URI，展示、正文与诊断原生路径按字段职责处理。

#### Scenario: 高清图片生成预览

- **WHEN** 用户上传最长边大于 512 的高清图片
- **THEN** 原图可靠保存，工作目录提供原图、最长边 512 的预览及可处理的目录引用

#### Scenario: 小图片不被放大

- **WHEN** 用户上传最长边小于 512 的图片
- **THEN** 预览最长边不超过原图最长边，不为满足固定尺寸放大

#### Scenario: 非图片附件

- **WHEN** 用户上传 PDF、文档或其它当前没有专用预览 renderer 的文件
- **THEN** 保留原件、目录引用和通用附件元数据，现有文件工具/Shell 可处理，预览状态明确，不新增 PDF 专用应用级 modality

#### Scenario: 只有系统预览的目录被释放后查看

- **WHEN** 用户上传并查看图片，最后关联解除后无其它占用或修改，工作目录被释放，随后重新打开附件
- **THEN** 按原 URI 重建原件并取得原规则预览，自动预览不会阻止释放，实际用过的历史输入 hash 不变

### Requirement: 用户消息角色与附件 block 语义必须可区分

系统 SHALL 以 `HumanMessage` 的消息类型/role 作为“这是用户消息”的权威来源，不得为了区分用户消息而新增 `is_user_message` content 字段。生成的附件 manifest 文本 block 和 preview rich block SHALL 带有 canonical-only 的附件语义 metadata（至少包括 `origin`、`kind`、schema version 和关联的 `file_id`）；`response_metadata.attachments` SHALL 作为附件清单的权威来源，用户提交的 `message_metadata` 不得覆盖这些系统字段。新用户消息不得仅为 Web 展示重复保存一份 `display_content`；provider request projection SHALL 删除不属于 provider wire schema 的 canonical-only metadata。

#### Scenario: 普通用户文本与附件 manifest 并存

- **WHEN** 一个 `HumanMessage` 包含用户原文 text block 和系统生成的 attachment manifest text block
- **THEN** 两者都保持在同一个用户消息中，但 block walker 能依据 manifest kind 将附件 manifest 排除出用户可见正文，并通过 `file_id` 关联附件 DTO

#### Scenario: 用户消息与内部结构消息区分

- **WHEN** 消息内容中出现 `<attachment ...>` 文本
- **THEN** 系统仍以 LangChain message role 和系统拥有的 message metadata 判断消息角色，不把它包装成 `<system_reminder>`，也不把普通用户消息标成 `internal`

#### Scenario: provider 投影清理 canonical metadata

- **WHEN** 目标 provider 只接受标准 text/image content block
- **THEN** 请求投影保留模型需要的文本和 rich block 字段，移除 block `metadata` 等 canonical-only 字段，checkpoint source 仍保留完整 metadata

### Requirement: 模型必须获得可解析的附件资源引用

系统 SHALL 在生成用户 content blocks 的同一既有入口中提供明确绑定 Gateway/工作区/会话目标的附件资源 URI、目录 URI、派生区输出 URI、文件名、MIME 类型及稳定身份，提示处理结果写入新文件或子目录。manifest 可使用 `<attachment ...>` 形式并复用现有转义规则，MUST NOT 调用内部 system_reminder 消息链。模型 SHALL 能通过现有文件读取/查看能力访问原件；工作目录已释放时 SHALL 由原 owner 从可靠版本重建，资源引用 MUST NOT 改为新附件或当前工作区。

Shell 的结构化 workdir SHALL 在目标机器沿同一文件 owner 解析并取得占用；相对路径、绝对路径和资源 URI 继续复用原 PTY 链路，命令正文保留原生语义，MUST NOT 要求新增 attachment 专用工具。URI 读取 SHALL 遵循 resource-file-operations 的产物分派；媒体 read_file、原件/thumbnail 接口、Provider 媒体投影与变体参数语义保持，严格 UTF-8 文本限制不扩张到受支持媒体，变体不编码进 URI。

#### Scenario: 模型收到图片附件说明

- **WHEN** 用户消息带持久化图片附件
- **THEN** 模型获得原资源 URI 和元信息，可经现有工具读取原图/变体；用户正文与诊断路径不成为另一定位关系

#### Scenario: 附件没有可直接注入的 rich block

- **WHEN** 当前模型或 provider 不能直接接受某个附件类型的 rich content block
- **THEN** 消息仍保留 URI 和元信息，不伪装成模型已解析文本，也不静默删除附件

#### Scenario: 工作目录释放后模型再次使用附件

- **WHEN** 后继执行通过已有附件 URI 读取原件或将目录设为 Shell workdir
- **THEN** 沿同一目标端 owner 重建并取得占用，再复用原文件/PTY 链路，不新增工具或读取另一机器上的同名路径

### Requirement: 预览 rich block 可以按目标能力选择性发送

系统 SHALL 在目标模型声明支持图像输入且存在有效预览时，通过 LiteLLM 的模型请求入口发送预览；原始附件和资源 URI文本仍是 canonical 用户消息的一部分。Chat Completions 与 Anthropic Messages 传给 LiteLLM 的图片 block SHALL 使用 `image_url` 形状，Responses 传给 LiteLLM 的图片 block SHALL 使用 `input_image` 及字符串 `image_url` 形状。应用层不得构造或发送 Anthropic 原生 `image/source` block，最终 provider wire 转换由 LiteLLM 完成。目标 provider 不支持该 rich block 时，系统 SHALL 保留资源引用文本并产生可诊断的未发送/失败投影结果，不得将可选 rich block 的失败伪装为成功解析或绕过 LiteLLM 直连 provider SDK。

#### Scenario: Chat Completions 或 Anthropic 支持图像输入

- **WHEN** 目标协议为 Chat Completions 或 Anthropic Messages，且附件存在生成成功的预览
- **THEN** 请求通过 LiteLLM 发送并包含 `{"type":"image_url","image_url":{"url":"data:image/...;base64,..."}}` block，同时保留用户文本和附件资源 URI 元信息；应用层不生成 Anthropic 原生 `image/source` block

#### Scenario: Responses 支持图像输入

- **WHEN** 目标协议为 Responses，且附件存在生成成功的预览
- **THEN** 请求通过 LiteLLM Responses 入口发送，并包含 `{"type":"input_image","image_url":"data:image/...;base64,..."}` block，同时保留用户文本和附件资源 URI 元信息

#### Scenario: 不支持图像输入的目标 provider

- **WHEN** 目标 provider 不接受该图片 rich block
- **THEN** 请求仍包含可见文本和附件资源 URI，rich block 被明确标记为未发送或投影失败，原始用户消息和附件文件不被修改，系统不得绕过 LiteLLM 直连 provider SDK

### Requirement: 模型实际使用的预览 content 必须进入 checkpoint 和历史

系统 SHALL 将本次模型请求实际使用的有序用户 content 保存到 checkpoint 和 rollout 历史；其中为模型生成的预览 `image_url` data URL/base64 属于可恢复的模型输入历史，不能在持久化阶段因其是 base64 而被删除。该原始 content 仅作为模型/完整历史数据，不得直接作为 Web 用户消息正文渲染。

#### Scenario: 预览 base64 的 checkpoint roundtrip

- **WHEN** 模型请求使用了图片预览 `image_url` data URL
- **THEN** checkpoint 和 rollout 恢复后仍能得到相同的用户 block 顺序及预览数据，Web 用户消息正文只显示文本投影和附件卡片

#### Scenario: 同一历史切换 provider

- **WHEN** 同一 checkpoint 分别恢复到支持和不支持图像输入的目标 provider
- **THEN** 两次读取都不改写 source checkpoint；支持方可发送预览，不支持方只过滤请求投影中的 rich block 并保留路径文本

### Requirement: Web 必须从用户消息投影独立展示附件

Web 历史 API SHALL 为用户消息返回独立的可见文本和附件 DTO/元数据，不得把 `image_url`、data URL、provider block 或完整 content 数组直接放入 `content` 字段供正文 renderer 展示。客户端 SHALL 优先加载附件预览；用户点击附件后，主窗口的右侧侧边栏 SHALL 打开对应会话资源或附件查看器。

#### Scenario: 历史加载包含图片附件

- **WHEN** Web 加载一条包含文本和图片 preview block 的用户消息
- **THEN** 正文只渲染文本，附件区域根据附件元数据加载缩略图，不显示 `{"image_url": ...}` 或 base64 字符串

#### Scenario: 点击附件进入右侧资源

- **WHEN** 用户点击用户消息中的附件卡片
- **THEN** 主窗口的右侧侧边栏打开该会话附件的原图或原件资源，且资源身份来自稳定 `file_id`，不是从展示文本反解析路径

### Requirement: 附件处理失败必须透明且不伪造默认值

系统 SHALL 区分原件保存失败、预览生成失败、rich block 投影不支持和 Web 预览读取失败，并在对应的模型请求、历史 API 或客户端状态中提供可诊断信息。任何失败不得返回空附件、虚假的已解析正文或静默丢弃用户上传的原件引用。

#### Scenario: 预览生成失败但原件已保存

- **WHEN** 原件保存成功但预览生成失败
- **THEN** 附件元数据保留原件和失败原因，模型仍可获得附件资源 URI，Web 显示明确的预览不可用状态

#### Scenario: 附件资源读取失败

- **WHEN** 客户端根据稳定 `file_id` 请求预览或原件失败
- **THEN** 客户端显示可识别的加载错误，不把损坏响应当作图片或空白成功内容

