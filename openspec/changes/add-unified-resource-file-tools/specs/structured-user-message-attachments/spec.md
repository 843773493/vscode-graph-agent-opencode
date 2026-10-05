## RENAMED Requirements

- FROM: `### Requirement: 模型必须获得可组合的相对附件路径元信息`
- TO: `### Requirement: 模型必须获得可解析的附件资源引用`

## MODIFIED Requirements

### Requirement: 附件必须保存原件并提供受界限的预览变体

系统 SHALL 在当前会话的附件存储范围内保存用户上传的原始文件，并为需要预览的类型按稳定 `file_id` 生成可重复读取的预览变体。图片预览的最长边 SHALL 为 `min(512, max(原始宽度, 原始高度))`，不得放大原图；附件引用 SHALL 至少包含稳定 id、文件名、MIME 类型和由原附件 owner 提供的统一资源 URI（模板引用 add-unified-virtual-resource-addressing）。其它文件类型仍按通用附件保存，不得因为没有直接预览器而伪造预览成功。

#### Scenario: 高清图片生成预览

- **WHEN** 用户上传一张最长边大于 512 的高清图片
- **THEN** 原图保持可读取，系统生成最长边为 512 的预览变体，并在附件元数据中同时区分原图与预览

#### Scenario: 小图片不被放大

- **WHEN** 用户上传一张最长边小于 512 的图片
- **THEN** 预览最长边不超过原图最长边，系统不得为满足固定尺寸而放大图片

#### Scenario: 非图片附件

- **WHEN** 用户上传 PDF、文档或其它当前没有专用预览 renderer 的文件
- **THEN** 原件和通用附件元数据仍被保存，预览状态明确表示不可用或使用已有通用变体；系统不新增 PDF 专用应用级 modality，也不返回虚假的图片预览

### Requirement: 模型必须获得可解析的附件资源引用

系统 SHALL 由 `UserContentBuilder` 在生成用户 content blocks 的同一入口中提供明确绑定Gateway/工作区/会话目标的附件资源 URI，以及文件名、MIME 类型和稳定附件身份等必要元信息。manifest 可以采用 `<attachment ...>` 形式并复用项目现有标记的转义规则，但不得调用只用于内部 `system_reminder` 的 `InternalMessageFactory` 或内部消息 middleware。模型可以使用理解统一寻址的现有文件读取或查看能力访问原件；原生终端命令不会自动解析资源 URI，跨端进程执行合同另行定义；系统不得要求新增 `attachment.xxx` 工具才能访问附件。

#### Scenario: 模型收到图片附件说明

- **WHEN** 用户消息带有持久化图片附件
- **THEN** 模型文本中包含可解析的统一资源 URI 和附件元信息，不包含真实路径，模型可用支持该读取能力的工具访问原图或其它变体

#### Scenario: 附件没有可直接注入的 rich block

- **WHEN** 当前目标模型或 provider 不能直接接受某个附件类型的 rich content block
- **THEN** 用户消息仍保留资源 URI 和元信息，系统不把附件伪装成已被模型解析的文本，也不因缺少专用附件工具而静默删除附件

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

### Requirement: 附件处理失败必须透明且不伪造默认值

系统 SHALL 区分原件保存失败、预览生成失败、rich block 投影不支持和 Web 预览读取失败，并在对应的模型请求、历史 API 或客户端状态中提供可诊断信息。任何失败不得返回空附件、虚假的已解析正文或静默丢弃用户上传的原件引用。

#### Scenario: 预览生成失败但原件已保存

- **WHEN** 原件保存成功但预览生成失败
- **THEN** 附件元数据保留原件和失败原因，模型仍可获得附件资源 URI，Web 显示明确的预览不可用状态

#### Scenario: 附件资源读取失败

- **WHEN** 客户端根据稳定 `file_id` 请求预览或原件失败
- **THEN** 客户端显示可识别的加载错误，不把损坏响应当作图片或空白成功内容
