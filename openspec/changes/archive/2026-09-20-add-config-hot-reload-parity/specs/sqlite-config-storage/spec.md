## ADDED Requirements

### Requirement: JSONC 文件变化必须与 SQLite 可变配置状态同步

SQLite SHALL（必须）继续作为运行时可变配置的持久化权威，但已纳入配置加载边界的 JSONC 文件变化 MUST（必须）被识别为一个带来源和基线 digest 的配置变更输入。文件变化通过完整校验后 SHALL（必须）事务性更新对应 SQLite 配置层或形成待重启 revision；不得在已有 SQLite 记录后静默忽略文件编辑。

#### Scenario: 迁移后修改用户级 Workspace JSONC
- **WHEN** 用户级 `workspace.jsonc` 已经迁移到 SQLite，用户随后修改该文件
- **THEN** 系统发现 digest 变化，校验并导入该配置层，或明确报告 pending/rejected，而不是继续无提示地使用旧 SQLite 值

#### Scenario: 迁移后修改工作区级 Workspace JSONC
- **WHEN** 当前工作区 `.boxteam/workspace.jsonc` 已经迁移到工作区 SQLite，用户随后修改该文件
- **THEN** 只有该工作区的配置层被更新，其他工作区和 Gateway 控制面状态不受影响

#### Scenario: 文件与 API 并发修改
- **WHEN** JSONC 文件基于旧 digest 修改，同时 API/UI 已提交了同一配置层的新 revision
- **THEN** 系统拒绝无法合并的文件候选，保留最新 active/pending 状态，并报告冲突双方来源和 digest

### Requirement: 配置迁移和导入必须可恢复

JSONC 到 SQLite 的首次迁移、后续导入和待重启提交 SHALL（必须）使用版本化、可恢复和可核验的写入顺序。失败时 MUST（必须）保留原始文件、旧 SQLite 记录和当前 active snapshot，并提供可定位的失败状态。

#### Scenario: 文件导入成功
- **WHEN** 修改后的 JSONC 通过解析、schema 和业务预检
- **THEN** SQLite 事务提交新的配置层、来源 digest 和 revision，随后运行时按生效策略处理该候选

#### Scenario: 文件导入失败
- **WHEN** JSONC 语法、schema、版本或 SQLite 写入失败
- **THEN** 系统不删除原始文件、不写入部分记录，并保留旧运行时配置

### Requirement: 配置状态和事件 outbox 必须同事务提交

Gateway/Workspace 的 active、pending、applying、恢复状态、revision 和配置事件 outbox SHALL（必须）在同一个配置域 SQLite 事务内写入。SSE/订阅 relay MUST（必须）只发布已经提交的 outbox 记录，并以 event_id/cursor 幂等确认；relay 崩溃不得造成状态已提交但事件永久丢失，也不得通过重复发布生成第二个配置提交。

#### Scenario: 状态提交后 relay 崩溃
- **WHEN** active promotion 或 pending 状态已经与 outbox 在同一事务内提交，但 relay 在发送前退出
- **THEN** 下次 relay 依据未确认的 event_id/cursor 重放事件，active/pending 状态不需要回滚

#### Scenario: relay 重试
- **WHEN** 同一个 outbox 事件因网络或客户端断开而重复发送
- **THEN** event_id/cursor 保持不变，消费者能够去重，不产生新的 commit_revision

### Requirement: JSONC、SQLite 与 API/UI 写入必须遵循 CAS 协议

SQLite SHALL（必须）继续作为运行时可变配置的持久化权威。JSONC 文件变化、API/UI 修改都 MUST（必须）携带或绑定目标配置层的 `base_layer_revision` 与 `base_layer_digest`，并在 SQLite 事务中执行 compare-and-set；API/UI 还 MUST（必须）校验调用方声明的 active revision。CAS 失败 MUST（必须）返回 conflict，不能覆盖较新的来源。

JSONC 导入 MUST（必须）在读取内容前后重新确认文件 digest 或等价的稳定文件快照；发现文件在解析期间变化时必须重新读取或返回 TOCTOU conflict。API/UI 写入默认只更新明确指定的 SQLite 配置层，不得隐式修改其他层或通过诊断接口写库；只有显式声明 `scope=user_source` 的 source-writer 操作可以在 expected source revision/digest 校验后原子替换用户级 JSONC，并由各 Workspace 独立 fan-out 导入。其他文件物化操作必须使用独立的显式写入命令并原子替换文件。

#### Scenario: API/UI 使用旧 active revision
- **WHEN** API/UI 提交配置时声明的 active revision 已不是当前 active revision
- **THEN** 系统拒绝写入并返回 conflict，响应包含当前 revision 和可重新读取的来源摘要

#### Scenario: JSONC 读取期间发生替换
- **WHEN** watcher 读取 JSONC 后、提交 SQLite 前检测到文件 digest 已改变
- **THEN** 系统不提交该字节内容，重新计算候选或报告 TOCTOU conflict

#### Scenario: 诊断接口只读
- **WHEN** 客户端调用配置 sources/reload-status 诊断接口
- **THEN** 接口只能读取并返回状态，不触发迁移、备份、revision 递增或 SQLite 写入

### Requirement: JSONC 迁移时机和后续编辑必须可证明

首次迁移 SHALL（必须）仅在对应 SQLite 配置层不存在记录时执行，并在同一可恢复流程中校验稳定 JSONC、写入层 payload、记录 `layer_digest`/来源路径和迁移摘要；已存在记录时，启动和诊断不得用 JSONC 无条件覆盖 SQLite。后续 JSONC 编辑只能通过 watcher 导入协议更新 SQLite 或形成 pending/conflict 状态。

#### Scenario: 首次启动迁移
- **WHEN** Gateway 或 Workspace 配置层尚无 SQLite 记录且 JSONC 稳定、合法
- **THEN** 系统事务性创建配置层记录和 digest，保留可核验备份，并以该记录作为后续运行时来源

#### Scenario: 已迁移配置重启
- **WHEN** SQLite 已有配置层记录而 JSONC 内容未产生新的可接受导入
- **THEN** 重启继续使用 SQLite 记录，不因启动读取 JSONC 而改变 active revision

#### Scenario: API/UI 修改后再次编辑 JSONC
- **WHEN** API/UI 已基于当前 CAS 成功更新 SQLite，而用户继续编辑旧基线 JSONC
- **THEN** watcher 比较层 revision/digest；可证明不重叠时按规则生成新候选，重叠时进入 conflict，不静默覆盖 API/UI 修改

### Requirement: Workspace 配置层和用户级 source 的 fan-out 必须可追踪

Workspace SQLite SHALL（必须）以独立记录保存 `user`、`user_local`、`workspace` 和 runtime override 层；每条记录分别具有 payload、来源路径、`layer_revision`、`layer_digest`、导入时间和 CAS 基线。`workspace.jsonc` 与 `workspace_local.jsonc` 不得共享一个 `layer_key`。有效合并得到的 active snapshot、需重启的 pending candidate 和 source layer payload MUST（必须）分别存储，source layer 提交不能使旧 runtime 读取 pending。

用户级 `workspace.jsonc` 的一次稳定文件变化 MUST（必须）生成 `fanout_id`/source generation。每个 Workspace 将其独立导入自己的 SQLite，并以自己的 layer/active CAS、candidate_id、commit_revision 和配置事件记录结果；跨 Workspace 不使用伪造的全局事务。冲突 Workspace 进入 `conflict`，成功 Workspace 不回滚；停止 Workspace 在启动时追赶。被更高优先级层遮蔽时，source layer revision/digest 可更新，但 effective digest 不变且结果为 `unchanged`。

#### Scenario: 同一用户源进入多个 Workspace
- **WHEN** 一个用户级 `workspace.jsonc` 原子替换后有两个运行中的 Workspace 和一个停止的 Workspace
- **THEN** 每个 Workspace 按同一 `fanout_id` 处理并产生独立 revision/event；运行中的立即导入，停止的启动时导入，任一 Workspace 的 SQLite 写入不会冒充或覆盖其他 Workspace

#### Scenario: fan-out 的 SQLite 冲突
- **WHEN** 某个 Workspace 的 `user` layer 在 fan-out 前已被 API/UI 以更高 layer revision 提交
- **THEN** 该 Workspace 的 CAS 失败并记录 conflict，其他 Workspace 仍可成功或各自失败；汇总结果明确为 `fanout_partial`，不跨数据库回滚

### Requirement: 配置候选中的秘密必须以引用或自包含字面量持久化

待持久化的 active/pending candidate SHALL（必须）只保存规范化配置和 `secret_ref`、secret version、`secret_binding_digest` 或用户显式写入的自包含字面量 key，不得保存任何运行时新解析出来的秘密字节。Workspace runtime 在启动/应用时才通过 Workspace-owned secret resolver 解析 `env:` 引用，并将引用/绑定摘要用于 candidate/effective digest；健康 proof、配置事件、诊断、日志、outbox 和 Gateway candidate_ref 委托 MUST（必须）脱敏，不得包含秘密原文或完整候选 payload。

字面量 key（例如本地部署模型的 dummy key、临时测试 apikey）SHALL（必须）按原文写入 SQLite 以支持原样重启恢复；它没有独立的 `secret_ref`，绑定摘要按字面量原文计算，因此无法通过该摘要检测轮换。只有旧版本写入的不可逆 `literal-sha256:` 摘要无法还原成可用 key，导入 MUST（必须）返回 `rejected`/`secret_reference_required`，保留旧 active 和原始 JSONC。secret resolver 失败时新 generation 启动失败，不得回退到 active 并伪造 pending 已加载。

#### Scenario: pending 候选包含 provider key
- **WHEN** 待重启候选包含 provider API key 或环境变量引用
- **THEN** SQLite pending、outbox、诊断和 health proof 只保留 secret reference/binding digest 或字面量的不可逆摘要；新 Workspace backend 在内存中解析并返回匹配的脱敏 proof

#### Scenario: 字面量秘密按原文持久化
- **WHEN** 配置导入发现字面量 API key（如本地模型 dummy key 或临时 apikey）
- **THEN** 候选按原文写入 SQLite 并可在重启后原样恢复运行，但所有诊断、事件、日志、outbox 和 health proof 中只出现不可逆摘要，不出现该 key 原文

#### Scenario: 旧版不可逆摘要无法恢复
- **WHEN** 旧 SQLite 记录包含 `literal-sha256:` 形式的不可逆摘要
- **THEN** 导入被拒绝并返回 secret_reference_required，旧 active 不变，任何持久化事件和错误详情都不包含该摘要之外的秘密值

### Requirement: JSONC source layer 必须区分 present 与 absent

每个 JSONC source layer 记录 MUST（必须）保存 `presence`，取值为 `present` 或 `absent`，并允许 absent layer 的 `payload=null`。规范文件删除时，source owner 必须写入新的 layer revision、absent tombstone、删除前 digest、canonical path 和 source generation；SQLite 中旧 payload 不能继续代表已删除文件，也不能因为启动优先读取 SQLite 而掩盖删除。

`absent` 合并语义是该层不提供覆盖并向下回落，和存在但内容为空的 JSONC 对象不同。删除导致的 effective 配置变化仍必须走候选校验、策略分类和 active/pending 判断；删除需重启层的有效贡献必须产生 pending_restart。watcher 不得把 `.tmp`、`.swp`、`.bak`、恢复文件或未完成 rename 当作稳定 source；rename 应由 source journal 有序记录旧路径 absent 与新路径 present。删除、rename 和恢复都必须保留备份或可核验 digest，恢复操作重新走 source owner/CAS。

#### Scenario: 删除已迁移的 JSONC
- **WHEN** 已有 SQLite payload 的 JSONC source 文件被删除
- **THEN** 系统写入 absent tombstone 并重新合并优先级，旧 SQLite payload 不再遮蔽删除；若有效配置变化需重启则保留旧 active 和 pending

#### Scenario: 删除被更高层遮蔽的文件
- **WHEN** 被更高优先级 layer 覆盖的 JSONC 文件被删除，effective payload 不变
- **THEN** source layer revision/digest 仍更新，但 effective digest 不变，结果为 unchanged，不产生虚假的 active 变化

#### Scenario: 编辑器临时文件和原子 rename
- **WHEN** watcher 观察到临时文件、恢复文件或一组 rename 事件
- **THEN** 临时路径不单独导入；稳定 rename 按 source journal 顺序产生 absent/present 事件，不能用中间文件内容覆盖 active

### Requirement: active snapshot 与 pending candidate 必须持久且可恢复

Workspace SQLite MUST（必须）分别保存 active snapshot、pending candidate、source layer 和 apply claim。active snapshot 至少包含配置域唯一键、active revision、可空 candidate id、脱敏规范化 payload、完整 source baseline、source generation、逐层 revision/digest、effective digest、secret binding 摘要、schema version、promoted generation、promoted apply id 和时间；pending 至少包含配置域/candidate 唯一键、idempotency 唯一键、pending revision、脱敏 payload、source baseline、candidate/effective digest、目标 generation、fencing token、state、last error 和创建时间。

promotion 必须在同一事务中更新 active snapshot、pending state、apply claim 和 outbox。事务提交前崩溃只能保留旧 active/pending；提交后进程或 relay 崩溃必须依据 active snapshot、promoted generation、fencing token、outbox 和 apply journal 恢复，不能出现 active 已改变但没有可重放事件。启动先使用 active；只有匹配 restart intent/candidate_ref 才能读取 pending。active 缺失、损坏或 source baseline 无法核验时，不得从 pending 猜测恢复，必须报告 recovery_required 或走明确 bootstrap。

#### Scenario: source 更新但 pending 尚未 promotion
- **WHEN** source layer 已产生新 revision，而 pending candidate 尚未完成外部 apply
- **THEN** active snapshot 保持旧 payload，pending 保留原始基线并在后续 CAS 失败时重建或冲突，不覆盖旧 runtime

#### Scenario: promotion 事务提交后进程崩溃
- **WHEN** active/pending/outbox 事务已经提交但进程在发布事件或切换后退出
- **THEN** 启动恢复从 active snapshot 和 outbox 识别已 promotion 的 generation，补发事件或完成 journal，不重新从当前 JSONC 猜测 active

#### Scenario: active snapshot 损坏或 secret binding 失效
- **WHEN** 启动发现 active payload/digest 不一致或 active 的 secret reference 无法解析
- **THEN** 系统明确进入 recovery_required/blocked，不能以 pending 或虚假默认值替代 active，也不能在诊断中回显秘密

### Requirement: 用户 api_key 与内部 secret_ref 的边界必须稳定

当前用户 JSONC schema MUST（必须）继续以 `api_key` 作为公开契约，并接受受支持的 `${ENV_NAME}` 引用与自包含字面量 key。`${ENV_NAME}` 在内部候选和持久 snapshot 中规范化为不可逆的 secret reference；字面量 key 按原文持久化并直接使用，但所有诊断、事件、日志、outbox 和 health proof 只暴露其不可逆摘要。只有旧版本写入的 `literal-sha256:` 摘要无法还原，导入返回 `secret_reference_required`。未来正式支持用户侧 `secret_ref` 必须伴随配置版本升级。

旧 SQLite 中的字面量和环境变量引用必须逐条迁移；环境变量引用规范化为 `env:NAME`，字面量按原文保留，两者迁移成功都不阻断。只有不可逆 `literal-sha256:` 摘要的迁移必须返回阻断路径并进入 blocked/recovery_required。secret rotation 必须产生新的 binding/version 和 candidate digest，旧 active 在新 generation proof 成功前继续服务。

#### Scenario: 环境变量引用和 secret rotation
- **WHEN** `${ENV_NAME}` 解析到新的 secret binding，或用户轮换 secret
- **THEN** 系统以新的 reference/version 生成 candidate，旧 active 不被原地改写，新 generation proof 匹配后才 promotion

#### Scenario: 旧 SQLite literal key 迁移成功
- **WHEN** 旧 SQLite 记录包含普通 literal key 或 `${ENV_NAME}` 引用
- **THEN** 迁移分别按原文保留和规范化为 `env:NAME`，返回空阻断路径，记录可正常启动恢复

#### Scenario: 旧 SQLite 不可逆摘要迁移失败
- **WHEN** 旧 SQLite 记录包含 `literal-sha256:` 形式的不可逆摘要
- **THEN** 迁移返回该字段路径并进入 blocked/recovery_required，旧 active 尽可能保持服务，任何错误和新记录都不复制该摘要之外的秘密值

### Requirement: 用户级 source 必须由共享 journal 分配单调 generation

用户级 `workspace.jsonc` 的 source owner MUST（必须）维护 append-only source journal，使用 `(source_key, source_generation)` 唯一身份，并记录 source event id、canonical path、presence、post-write digest、layer revision、previous digest、writer/watch origin 和 fan-out 状态。source generation 由 source owner 通过 CAS 单调递增；`fanout_id` 绑定 source event/generation，digest 只用于内容去重，不得作为事件身份。

#### Scenario: A 到 B 再回到 A
- **WHEN** 同一 source 依次写入 A、B、A
- **THEN** journal 产生连续的 generation 1、2、3 和三个可追踪 fanout id；只有稳定的 A 到 A 重复事件才允许按 digest 去重

#### Scenario: Workspace 停止期间发生 source 变化
- **WHEN** Workspace 停止时 source journal 产生多个新 generation，之后 Workspace 启动
- **THEN** Workspace 读取 high-water mark 与自身 last-applied generation，按顺序追赶缺失事件，不能只比较当前 digest 猜测状态
