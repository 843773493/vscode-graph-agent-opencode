## Purpose

为多 provider 会话提供可持久化、可投影且可展示的结构化用户消息附件模型，使模型输入、checkpoint 历史和 Web 用户界面共享同一组附件身份但使用各自适合的 content block 投影。

## ADDED Requirements

### Requirement: 用户消息必须以有序 block 序列作为 canonical content

系统 SHALL 将用户消息的文本、附件说明和可选的 provider 内容 block 作为有序序列独立保存和遍历。任何包含多个 block 的用户消息不得在持久化或历史投影阶段整体 `json.dumps` 为一段用户可见文本；未知但结构合法的 block SHALL 原样保留，不能因为 Web 不认识它而丢失模型历史。

#### Scenario: 文本与图片 block 混合

- **WHEN** 用户消息包含一段文本、附件路径说明和一个 `image_url` 预览 block
- **THEN** canonical content 保持三个 block 的顺序，文本提取只读取文本 block，附件预览不变成用户消息正文中的 JSON 字符串

#### Scenario: 未知 provider block

- **WHEN** 用户消息包含当前 Web renderer 未定义的 provider 扩展 block
- **THEN** checkpoint 和 rollout 保留该 block 的原始字段，Web 只在自己的可见投影中忽略或标记它，不修改 canonical content

### Requirement: 附件必须保存原件并提供受界限的预览变体

系统 SHALL 在当前会话的附件存储范围内保存用户上传的原始文件，并为需要预览的类型按稳定 `file_id` 生成可重复读取的预览变体。图片预览的最长边 SHALL 为 `min(512, max(原始宽度, 原始高度))`，不得放大原图；附件引用 SHALL 至少包含稳定 id、文件名、MIME 类型和相对路径或可解析的会话定位信息。其它文件类型仍按通用附件保存，不得因为没有直接预览器而伪造预览成功。

#### Scenario: 高清图片生成预览

- **WHEN** 用户上传一张最长边大于 512 的高清图片
- **THEN** 原图保持可读取，系统生成最长边为 512 的预览变体，并在附件元数据中同时区分原图与预览

#### Scenario: 小图片不被放大

- **WHEN** 用户上传一张最长边小于 512 的图片
- **THEN** 预览最长边不超过原图最长边，系统不得为满足固定尺寸而放大图片

#### Scenario: 非图片附件

- **WHEN** 用户上传 PDF、文档或其它当前没有专用预览 renderer 的文件
- **THEN** 原件和通用附件元数据仍被保存，预览状态明确表示不可用或使用已有通用变体；系统不新增 PDF 专用应用级 modality，也不返回虚假的图片预览

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

### Requirement: 模型必须获得可组合的相对附件路径元信息

系统 SHALL 由 `UserContentBuilder` 在生成用户 content blocks 的同一入口中提供相对于工作区可解析根的附件路径，以及文件名、MIME 类型和稳定附件身份等必要元信息。manifest 可以采用 `<attachment ...>` 形式并复用项目现有标记的转义规则，但不得调用只用于内部 `system_reminder` 的 `InternalMessageFactory` 或内部消息 middleware。模型可以使用现有文件读取、终端、查看或其它工具处理原件；系统不得要求新增 `attachment.xxx` 工具才能访问附件。

#### Scenario: 模型收到图片附件说明

- **WHEN** 用户消息带有持久化图片附件
- **THEN** 模型文本中包含可解析的相对路径和附件元信息，路径不依赖本机绝对路径，模型可以据此组合现有工具读取原图或其它变体

#### Scenario: 附件没有可直接注入的 rich block

- **WHEN** 当前目标模型或 provider 不能直接接受某个附件类型的 rich content block
- **THEN** 用户消息仍保留相对路径和元信息，系统不把附件伪装成已被模型解析的文本，也不因缺少专用附件工具而静默删除附件

### Requirement: 预览 rich block 可以按目标能力选择性发送

系统 SHALL 在目标模型声明支持图像输入且存在有效预览时，通过 LiteLLM 的模型请求入口发送预览；原始附件和相对路径文本仍是 canonical 用户消息的一部分。Chat Completions 与 Anthropic Messages 传给 LiteLLM 的图片 block SHALL 使用 `image_url` 形状，Responses 传给 LiteLLM 的图片 block SHALL 使用 `input_image` 及字符串 `image_url` 形状。应用层不得构造或发送 Anthropic 原生 `image/source` block，最终 provider wire 转换由 LiteLLM 完成。目标 provider 不支持该 rich block 时，系统 SHALL 保留路径文本并产生可诊断的未发送/失败投影结果，不得将可选 rich block 的失败伪装为成功解析或绕过 LiteLLM 直连 provider SDK。

#### Scenario: Chat Completions 或 Anthropic 支持图像输入

- **WHEN** 目标协议为 Chat Completions 或 Anthropic Messages，且附件存在生成成功的预览
- **THEN** 请求通过 LiteLLM 发送并包含 `{"type":"image_url","image_url":{"url":"data:image/...;base64,..."}}` block，同时保留用户文本和附件路径元信息；应用层不生成 Anthropic 原生 `image/source` block

#### Scenario: Responses 支持图像输入

- **WHEN** 目标协议为 Responses，且附件存在生成成功的预览
- **THEN** 请求通过 LiteLLM Responses 入口发送，并包含 `{"type":"input_image","image_url":"data:image/...;base64,..."}` block，同时保留用户文本和附件路径元信息

#### Scenario: 不支持图像输入的目标 provider

- **WHEN** 目标 provider 不接受该图片 rich block
- **THEN** 请求仍包含可见文本和相对附件路径，rich block 被明确标记为未发送或投影失败，原始用户消息和附件文件不被修改，系统不得绕过 LiteLLM 直连 provider SDK

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
- **THEN** 附件元数据保留原件和失败原因，模型仍可获得相对路径，Web 显示明确的预览不可用状态

#### Scenario: 附件资源读取失败

- **WHEN** 客户端根据稳定 `file_id` 请求预览或原件失败
- **THEN** 客户端显示可识别的加载错误，不把损坏响应当作图片或空白成功内容
