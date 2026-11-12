## MODIFIED Requirements

### Requirement: kind 闭集定稿且描述符闭集独立不可混用

URI kind 闭集 MUST 为 `agent-spec` | `skills` | `config` | `session` | `files` | `attachments` | `workspace` | `folder` | `changes`。`attachments` 只允许 workspace scope，目录根为 `boxteam://{gateway_authority?}/workspace/{workspace_id}/resources/attachments/{attachment_id}`，其后按同一文件路径段规则访问 child；原件与预览只读，派生区复用原文件 owner 的写入能力。`files` 用于 `workspace` scope 的文件树根 `boxteam://{gateway_authority?}/workspace/{workspace_id}/resources/files/root`，以及 requirement「原生目录根绑定必须由目标 Gateway 唯一持有」登记的 `gateway` scope 原生目录根；`workspace`/`folder` 沿用现行领域导航模板。config 定位配置逻辑来源，session 定位领域对象或由原 owner 公布的产物引用，二者不得被文件工具自行解释成私有目录。

语法 kind 与语义来源描述符 kind MUST 分开：描述符仍只有 `agent-spec`/`skills`，普通 files 导航不会创建来源描述符。后续扩展必须先在本 owner 更新模板和能力，不存在通用未知 kind fallback；旧 memory 两点式仍拒绝。

#### Scenario: 工作区文件树有正式根

- **WHEN** 用 files/root 定位某个已明确指定工作区的源码树
- **THEN** owner 以 URI/身份解析到目录根并返回目录 shape、规范 URI 与操作能力，可按公布的 child path 规则导航；解析结果和普通 DTO MUST NOT 返回物理根，实际文件访问只在原生 owner 本地使用 root binding，且不依据当前激活工作区替换 scope_id

#### Scenario: 未登记 kind 或 scope 组合被拒绝

- **WHEN** 使用未登记 kind 或在 user/gateway/inline scope 使用 workspace files 根
- **THEN** owner 显式拒绝，不回退为本地普通路径

#### Scenario: 描述符闭集不因目录导航扩张

- **WHEN** 普通 files 目录列举或搜索返回许多文件
- **THEN** 系统不将 files 加入语义来源描述符闭集，也不逐文件建立来源或长期身份


## ADDED Requirements

### Requirement: Session 文件更改使用原 owner 的当前 Diff 地址

中央寻址 owner SHALL 登记 `boxteam://{gateway_authority?}/workspace/{workspace_id}/resources/changes/{session_id}/{selection}/{source_gateway?}/{source_scope}/{source_scope_id}/resources/{source_kind}/{root}/{child...}`。`changes` 只属于 workspace scope；Session ID 沿既有 profile，selection 只接受 `all`、`turn_[0-9a-f]{32}` 或 replay owner 的 `turn-replay-[0-9a-f]{32}`，分别映射原变更 API 的 `all` 或 `turn:{turn_id}`，身份段不得百分号编码。

selection 后的层级 SHALL 是完整规范源文件 URI 去掉 `boxteam://` 的地址，由同一 grammar 解析为明确 `files` 或 `attachments` child；不能是目录根、其它 kind 或嵌套 changes。源文件名沿原文件段编码，只解码一次，不继续解释字面百分号。不得把完整源 URI 百分号编码进单段，避免通用 URI 对象将编码斜杠展开后改变结构。嵌入引用补齐明确 Gateway，交原 owner 读取时归一为目标 Gateway 的规范本地形式；复制的完整外层 URI 同样携带明确 Gateway，解析不在中继机器解释路径。

地址仅表达 Session、变更范围及源文件，不带 edit_id、revision、query、fragment 或正文。认证 Gateway 地址构造／解析不读取业务存储或授予权限。实际打开 SHALL 由原 SessionChanges owner 读取当前聚合 Diff，并由原只读编辑器展示；已打开版本与恢复仍归原变更 owner，不增加普通文件快照、通用版本字段、第二份历史或持久账本。未接通通用正文读取 owner 时，只提供复制／打开和实际正文粘贴，不将其伪装成可供 Composer 读取的文件引用。

#### Scenario: 中文文件及 replay Turn 通过同一 grammar 往返

- **WHEN** 普通执行或 replay Turn 修改包含中文、空格、百分号等文件名的资源并复制 Diff 地址
- **THEN** 构造和解析保留原 Session、Turn 及源文件归属，文件名只按原文件段规则解码一次；零正文读取，内部编辑器投影不被公开

#### Scenario: 引用重开读取当前变更而不制造快照

- **WHEN** 用户关闭 Diff 后用公开 URI 重新打开
- **THEN** 原 owner 核验访问并返回当前聚合 Diff，前后文本共用一次响应；源文件不在变更集中时明确失败，不改投同名文件，不以历史版本或当前文件猜测内容
