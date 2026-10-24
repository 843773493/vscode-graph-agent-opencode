## MODIFIED Requirements

### Requirement: 附件必须保存原件并提供受界限的预览变体

系统 SHALL 将用户上传原始文件保存到工作区附件 owner 管理的独立目录资源，上传自动关联发起 Thread，后续处理输出与预览同属该目录资源。该目录的关联和回收遵循 managed-attachment-directories，不再按 Session 私有目录或共享单 blob 的生命周期决定原件寿命。系统 SHALL 为需要预览的类型按稳定文件引用生成可重复读取的预览变体；图片预览的最长边 SHALL 为 `min(512, max(原始宽度, 原始高度))`，不得放大原图。附件引用 SHALL 至少包含稳定 id、文件名、MIME 类型、原件资源 URI和附件目录 URI，模板和编码由统一 URI owner 定义，普通响应不返回真实路径。其它文件类型仍按通用附件保存，不得因为没有直接预览器而伪造预览成功。已用于模型输入的预览和正文版本 SHALL 遵循既有冻结历史合同，不能从当前目录重新生成旧输入。

#### Scenario: 高清图片生成预览

- **WHEN** 用户上传一张最长边大于 512 的高清图片
- **THEN** 原图在独立附件目录保持可读取，系统生成最长边为 512 的预览变体，并在附件元数据中同时区分原图、预览及可用于处理的目录引用

#### Scenario: 小图片不被放大

- **WHEN** 用户上传一张最长边小于 512 的图片
- **THEN** 预览最长边不超过原图最长边，系统不得为满足固定尺寸而放大图片

#### Scenario: 非图片附件

- **WHEN** 用户上传 PDF、文档或其它当前没有专用预览 renderer 的文件
- **THEN** 原件、目录引用和通用附件元数据仍被保存，Agent 可通过现有文件工具或 Shell 继续处理；预览状态明确表示不可用或使用已有通用变体，系统不新增 PDF 专用应用级 modality，也不返回虚假的图片预览

### Requirement: 模型必须获得可解析的附件资源引用

系统 SHALL 由 `UserContentBuilder` 在生成用户 content blocks 的同一入口中提供明确绑定Gateway/工作区/会话目标的附件资源 URI，以及目录 URI、派生区输出 URI、文件名、MIME 类型和稳定附件身份等必要元信息，并提示处理结果写入目录内新文件或子目录。manifest 可以采用 `<attachment ...>` 形式并复用项目现有标记的转义规则，但不得调用只用于内部 `system_reminder` 的 `InternalMessageFactory` 或内部消息 middleware。模型可以使用理解统一寻址的现有文件读取或查看能力访问原件；Shell 的结构化 workdir 由目标机器通过同一文件 owner 解析目录 URI并取得目录占用，命令正文继续采用原生路径语义，不扫描替换命令字符串；相对路径、绝对路径与 URI workdir 共用原 PTY 链路；系统不得要求新增 `attachment.xxx` 工具才能访问附件。

URI 读取 SHALL 按 `resource-file-operations` 的产物分派合同交原附件 owner；既有媒体 read_file、原件/thumbnail 内容接口和工具结果的 Provider 媒体投影保持同一行为。普通文本读取的严格 UTF-8 限制不适用于既有受支持媒体读取，变体继续由原 owner 的独立参数选择，不编码进 URI。

#### Scenario: 模型收到图片附件说明

- **WHEN** 用户消息带有持久化图片附件
- **THEN** 模型文本中包含可解析的统一资源 URI 和附件元信息，不包含真实路径，模型可用支持该读取能力的工具访问原图或其它变体

#### Scenario: 附件没有可直接注入的 rich block

- **WHEN** 当前目标模型或 provider 不能直接接受某个附件类型的 rich content block
- **THEN** 用户消息仍保留资源 URI 和元信息，系统不把附件伪装成已被模型解析的文本，也不因缺少专用附件工具而静默删除附件
