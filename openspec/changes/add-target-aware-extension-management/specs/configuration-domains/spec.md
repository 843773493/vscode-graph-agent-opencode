## MODIFIED Requirements

### Requirement: Workspace 配置来源层必须分别物化并支持用户级 fan-out

Workspace SHALL（必须）分别维护 `user`、`user_local`、`workspace` 和 runtime override 的 payload、`layer_revision`、`layer_digest`、来源路径和 CAS 基线，不得把 `workspace.jsonc` 与 `workspace_local.jsonc` 合并成一个来源层。有效配置仍按既有优先级合并；source layer 更新不等于 active snapshot 更新。用户级 `workspace.jsonc` 是一个共享 source，写入或 watcher 导入必须生成 `fanout_id`；显式 source writer 直接分配该关联键，直接人工编辑则由 watcher 按规范化 source path 与 post-write layer digest 派生。该键只用于 fan-out 关联/幂等，不得充当 commit revision、event cursor 或内容 digest。

每个 Workspace 必须独立执行 layer/active CAS，并分别分配自己的 candidate_id、commit_revision 和配置事件。运行中的 Workspace 立即尝试导入，未运行的 Workspace 在下次启动时追赶同一 source generation；跨 Workspace 不得伪造一个 SQLite 全局事务。一个 Workspace 冲突不得回滚其他 Workspace 的成功导入，跨 Workspace 汇总结果可报告为 `fanout_partial`，但该名称只属于 fan-out 诊断汇总，不属于单 Workspace 配置事件的 `result` 枚举。被更高优先级层遮蔽的 source layer 仍更新自身 layer revision/digest，但 effective digest 不变并返回 `unchanged`。

#### Scenario: 用户级 Workspace JSONC fan-out
- **WHEN** 用户修改一次 `workspace.jsonc`，且多个 Workspace 中有的正在运行、有的已停止
- **THEN** 每个 Workspace 按同一 `fanout_id` 独立导入并产生自己的结果；运行中的立即处理，停止的在启动时追赶，任何一个 Workspace 的 commit_revision 都不冒充其他 Workspace 的 revision

#### Scenario: fan-out 中单个 Workspace 冲突
- **WHEN** 一个 Workspace 的 `user` layer 已被 API/UI 以更高 revision 修改，随后共享 `workspace.jsonc` 以旧基线到达
- **THEN** 该 Workspace 进入 `conflict` 并保留自己的 active/pending，其他基于各自有效基线的 Workspace 可继续导入；汇总诊断报告 `fanout_partial` 和逐 Workspace 结果

#### Scenario: API/UI 指定配置范围
- **WHEN** API/UI 修改 Workspace 配置
- **THEN** 请求必须明确是单 Workspace 的 `workspace`/`user_local`/runtime override，或是用户级 source；原单 Workspace 层物化／runtime override 请求只写目标 SQLite；显式来源文件编辑由原 source writer 先执行文件 CAS／原子保存，再沿原导入和候选链物化目标层；用户级 source 请求先原子写 JSONC 再触发 fan-out。入口不得隐式改写其它层或多个 SQLite，文件已保存不代表 active 已生效

#### Scenario: 明确编辑单工作区来源文件
- **WHEN** 用户选择指定 Gateway／workspace 的 workspace 来源文件，以文件及层／active 基线保存一个覆盖片段
- **THEN** 原 source writer 验证合并后的最终配置并原子保存所选文件，再沿原导入／候选链更新该工作区；runtime override API 不回写文件，另一工作区不被修改


## ADDED Requirements

### Requirement: 来源正文编辑必须由配置 owner 提供

Gateway 与 Workspace 配置 owner SHALL 公布各来源层的读取与写入能力，返回绑定明确目标和 source key 的脱敏正文、schema、来源 revision／digest、有效值及遮蔽关系。未提供 writer 的层必须只读；现有有限 runtime override PATCH 不扩展为任意 JSON 或路径写入，通用文件保存不替代配置提交。来源资源未登记 URI 时保持无 URI，消费者不得编造定位。

可写来源 SHALL 提供无副作用预检及文件 CAS writer；预检按实际来源优先级合并后执行 schema／业务校验，不要求覆盖片段具有完整配置。删除覆盖只移除选中层的键，不能把下层默认值复制回该层。编辑与保存保留 JSONC 注释及格式，秘密仅在实际 owner 处理；正文中的脱敏占位不得被当作原值保存。

#### Scenario: 旧编辑基线与真实文件发生冲突
- **WHEN** 两个编辑器读取相同来源，另一编辑器或原生文件操作已改动来源后，旧编辑器提交
- **THEN** writer 明确拒绝旧文件／层／active 基线，不覆盖新内容，并允许重新读取原目标来源处理冲突

#### Scenario: 脱敏正文保存与删除覆盖
- **WHEN** 用户读取含秘密及注释的来源，仅修改普通字段或删除其中一个覆盖键
- **THEN** 原秘密及未修改注释保持，选定键删除后显示真实下层值，不保存遮罩为密码，不修改其它层

#### Scenario: 只读来源与无副作用预检
- **WHEN** 用户预检可写来源的合法修改，或尝试保存未提供 writer 的来源层
- **THEN** 预检不改变文件、SQLite、active 或运行资源；只读层保存明确拒绝，不使用通用文件 API 绕过

#### Scenario: Gateway 来源编辑缺少新连接身份
- **WHEN** 来源预检或保存遇到未填写 `connection_id` 的条目，且原连接身份 owner 无法明确匹配既有连接
- **THEN** 返回 409 并指出需要补齐身份的位置，文件、SQLite 及运行状态不变；不按数组位置借用其它连接身份，不在预检中随机生成或持久化身份，原文件 loader 的旧配置迁移行为保持

#### Scenario: 来源保存需要先完成旧版本迁移
- **WHEN** 提交的 v1 来源缺少连接 ID，原 loader 需要另外迁移并改写来源字节
- **THEN** 预检和保存明确拒绝并提示沿原迁移入口处理或补齐 ID，不在文件保存后悄悄换成另一份正文；读取与原迁移入口行为保持

### Requirement: 受管服务配置必须分离目标登记与工作区业务输入

Gateway SHALL 持有自有插件来源、安装／服务登记、实例范围、诊断设置及生命周期配置，Workspace SHALL 继续持有 MCP 参数、工作区使用关系、私有输入及业务配置。两域保持原来源优先级、独立 CAS 和 active／pending；Gateway MUST NOT 读取工作区 `.boxteam/`、复制秘密或直接改写工作区来源。

插件启用关系 SHALL 复用原配置 owner，记录 package_id／plugin_id、确切包版本、固定 Gateway／workspace 范围和 enabled；无包级／单项贡献第二 enabled，纯声明插件不伪造运行进程。manifest schema 仅描述合法字段，用户值仍属上述原来源域；Workbench 中的贡献注册是可释放展示投影，不形成第二份可生效配置或跨窗口业务账本。

参数 SHALL 在原配置域正式登记的 package_id／plugin_id 参数容器中保存，与版本绑定分离；未绑定或未安装插件的残留保留原来源，明确未生效，不进入 effective／active／pending／热应用或占包引用。原 source writer 仍验证容器结构／JSON 类型；重新显式绑定按确切版本 schema 完整校验，失败不启用。容器外未知字段和已绑定插件非法值仍拒绝；卸载删除包 schema 投影，不留下影子 schema、参数账本或第二配置源。

受管服务绑定 SHALL 引用明确 package_id／plugin_id、service_id、已发布包版本及 manifest 允许范围；workspace 绑定另核精确工作区。Backend stdio 的 managed_launch／command 创建链 SHALL 迁齐为 Gateway 服务连接并删除，不保留新旧配置运行时双读。完整工作区运行时恢复所需非秘密绑定 SHALL 由 Workspace 配置 owner 重新认证交付；未交付时 Gateway 等待，不用旧实例／库存推断启用状态，不越界读取文件。Backend 单独重启／Gateway 正常接管时已核实实例仍沿原合同保留。服务 ready 前所需工作区输入 SHALL 由原 owner 经认证准入交付，缺失时明确等待输入而非伪报就绪。安装发布不自动提交配置或运行插件。

通用插件秘密参数 SHALL 沿 target-scoped-settings 的同一合同支持密码输入、保存后复用、更换及清除，由原 Gateway／Workspace 配置 owner 使用现有权限受限私有文件与原子发布保存不可变值；普通来源只保存绑定目标、必要 workspace、插件、参数位置和值版本的正式引用。writeOnly 允许专用受保护入口接收真实值，拒绝普通 JSONC／参数通道中的秘密明文，写入核原 configuration_write，不回显原值。引用更换与清除仍经原 source writer／CAS，秘密保存不代替配置提交或实例生效，不删除仍被 active／pending 引用的值，不新增秘密账本或通用 SecretStorage SDK。

既有 `${ENV_NAME}`／`env:NAME` SHALL 作为可选秘密来源，仅由配置所属目标的原 owner 解释；实际值按确切包版本 schema 验证，普通正文不自动展开引用。工作区秘密仅经原认证启动／配置应用交付传给对应插件，不进入 Gateway 接管材料或普通配置镜像；合法已保存输入在重建时复用，缺失和归属错误明确报告，不隐式改用其它目标或来源。状态读取／后台恢复不自动弹密码框。

认证交付 SHALL 由原启动／配置变更通知或用户显式重试驱动；缺输入报告 waiting，认证失败报告 failed，普通状态读取不重新交付或启动插件，不加轮询、扫盘及配置修复流程。

#### Scenario: 两个工作区绑定同一安装包
- **WHEN** A1/A2 在各自来源 CAS 提交同一插件包的使用关系和不同私有引用
- **THEN** 两者配置独立生效，Gateway 只管理合法登记与实例，工作区秘密仍由原 owner 保存；旧 command 不在 Backend 触发进程

#### Scenario: Gateway 启动时工作区输入尚未交付
- **WHEN** Gateway 已读取非秘密服务登记，但所需 workspace 输入尚未由其 owner 交付
- **THEN** 原状态明确等待输入，Gateway 不读取工作区文件、不发布业务 ready；控制通道可先可用，等待输入不阻塞 Backend 初始化或其它已就绪服务，输入到位后才核实业务启动，Backend 后续重启不顺带关闭已运行服务

### Requirement: 诊断热配置必须沿原 Gateway 配置域扩展

日志 SHALL 继续使用 `logging.runtime`；新增本地遥测／统计 SHALL 分别归 `telemetry.runtime`、`statistics.runtime`，自有源归 `plugins.sources`，复用 Gateway JSONC 族、schema、原合并优先级与来源 writer，不增加插件配置副本。遥测／统计 enabled 默认 false，来源默认未配置；telemetry 的 sample_ratio 默认1且范围0–1，sample_buffer_limit 默认256且范围1–4096，statistics 的 collection_interval_ms 默认5000且范围1000–60000。schema SHALL 明确这些边界，允许原 Gateway 用户层合法覆盖，不接受 Workspace 覆盖。

只有已登记为热应用的字段 SHALL 在运行时应用；实例范围、入口或版本变更须明确对应实例的重启／切换，不由客户端猜测或一律重启 Backend。未支持的诊断能力 SHALL 明确报告，不将保存成功当作实际生效。

#### Scenario: 修改诊断开关不重启业务服务
- **WHEN** 用户在 Gateway 来源关闭遥测及统计并提交合法候选
- **THEN** 原 owner 以同一 revision 热应用并收集逐实例实际回执，成功实例停止额外采集，不更改工作区配置或为诊断开关重启 Backend

### Requirement: 插件开关和新建工作区默认值必须分离

当前 Gateway 的插件 enabled SHALL 归 Gateway 配置 owner，当前工作区的 enabled 归精确 Workspace owner；各有固定插件范围和包版本，不通过共享用户 Workspace 层 fan-out 修改其他工作区。开关和重启须获运行管理准入；同一来源写入同时遵守原配置写权限，不建立第二启用账本。实际状态单独报告，配置已保存不等于运行成功。

Gateway 新建工作区模板 SHALL 使用 plugins.workspace_defaults.entries，默认空数组，数组沿原整体替换语义；条目只有 package_id、plugin_id、确切 package_version 和 enabled，键组合唯一，只能引用已安装支持的 workspace 插件，不保存参数或秘密。沿原 Gateway inline／user／user_local 合法覆盖和 CAS 提交；不接受 workspace 层覆盖，模板编辑核原配置写权限，不产生进程副作用。模板和已冻结未结清创建 intent 的版本均为原安装 owner 的使用引用，更新清理及卸载不得删除；模板修改不解除旧 intent 引用，创建完成交接给工作区绑定，取消／失败明确结清才释放，不新建账本。安装不自动改模板；引用版本计入使用冲突，未解除引用不能卸载。

工作区创建 owner SHALL 在原创建 intent 冻结当时模板 revision 与条目，经原 Workspace writer 初始化该新工作区独立配置，再向 Gateway 交付已认证非秘密绑定；Gateway 不读写 .boxteam。原创建重试复用冻结输入，模板修改只影响之后新建工作区。已有工作区重新注册／挂接／启动不应用模板，不新增动态继承或第二创建账本。

模板提交、创建冻结／引用交接与包删除 SHALL 复用原安装互斥边界，交接完成后由工作区绑定接替 intent 引用，不先释放后创建引用，也不等对方先释放造成循环。卸载仅检查原实例、active／pending 绑定、模板／未结清 intent 和未完成读取／使用 lease；来源无法确认则拒绝该次卸载并说明，不新增全局引用追踪或后台巡检，不为检查启动 Backend。

#### Scenario: 当前工作区和以后新建默认值独立
- **WHEN** A1／A2 已存在，用户仅关闭 A1 的插件，再修改新建默认值并创建 A3
- **THEN** A1 按自身设置关闭，A2 不变，A3 取得创建时模板的独立设置；之后修改模板不改 A3，也不回写 A1

#### Scenario: 创建重试保持模板冻结
- **WHEN** 创建 intent 冻结模板 revision R 后中断，模板变为 R2，再重试原创建
- **THEN** 同一创建继续使用 R，只有后续新 intent 使用 R2；即使模板移除 R，其版本仍受原 intent 引用保护，更新清理／卸载返回该引用冲突，完成或取消后按原 owner 交接／释放；挂接已有工作区不写模板值，Gateway 无工作区文件访问

#### Scenario: 模板非法引用与版本卸载
- **WHEN** 用户保存不存在／非 workspace 插件或重复键的模板，或卸载当前模板引用的版本
- **THEN** 前者提交前拒绝且原模板保持，后者返回真实模板引用冲突，不自动安装、清空默认值或停其他插件
