## MODIFIED Requirements

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
