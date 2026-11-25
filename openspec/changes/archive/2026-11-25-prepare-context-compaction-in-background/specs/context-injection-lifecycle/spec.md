## MODIFIED Requirements

### Requirement: 已提交上下文前缀必须保持字节级稳定

系统 SHALL 把 Provider profile 下按 plan ordinal 排序并序列化的 wire context item frames 作为稳定前缀边界。在同一 prefix epoch 内，新 sealed assembly 必须完整保留父 assembly 的 context bytes，并且只能在其后追加新 item；不得修改旧 item 的 role、正文、content block、空白、编码、顺序或序列化。同一 epoch还必须绑定完全相同的 Provider projection profile、ToolSetRef和影响 root/tool visibility的 policy hash。首次组装、实际发生的上下文压缩重建、rewind重建和 ToolSet hard rebase可以建立重建前缀的新 epoch；此外，模型路由明确选择不同 Provider profile（包括真实失败后的 fallback）且没有实际compaction时，MUST 以 `provider_changed` 建立仅重投影的 epoch，复用上一 sealed assembly 的正文、角色、来源身份及顺序，不重新观察来源、编译 root、刷新 view 或伪造 ToolSet change。

实际模型选择后的容量准备 MUST 先区分重投影与正文替换。纯provider_changed保留上述冻结内容合同；若较小窗口必须改变active history，系统 SHALL 显式经原compaction owner应用适用候选或完成阻断生成，登记compaction原因并生成新view／plan／assembly和request hash。两类原因可由原epoch owner在同seal记录，但不得以provider_changed名义偷偷替换正文或重新观察来源。候选ready本身不改变view、epoch或已封存请求。

#### Scenario: 普通 source 更新只追加后缀

- **WHEN** 已提交 assembly 后 AGENTS、Skill、团队状态或其它 runtime source 发生变化
- **THEN** 下一 assembly 的已提交 context bytes 以旧 assembly 的完整 context bytes 为前缀，并只在尾部追加新的不可变 source item

#### Scenario: 普通 canonical item 只追加后缀

- **WHEN** 真实用户消息、assistant/reasoning、tool call或配对 tool result在当前 ToolSet与 active view下提交
- **THEN** owner保持当前 prefix epoch，并按 canonical ordinal只在父 assembly尾部追加对应不可变 item frames

#### Scenario: 重复 dispatch 复用同一字节

- **WHEN** 同一 sealed assembly 因 Provider transport 或进程恢复而重试
- **THEN** 系统复用已封存的 item 顺序和 Provider serialization，不重新投影、合并或格式化上下文

#### Scenario: 稳定前缀校验失败

- **WHEN** 新 assembly 的父前缀长度、hash 或逐字节内容与已提交父 assembly 不一致
- **THEN** 系统阻止 Provider dispatch 并返回明确的 stable-prefix violation，不以 cache miss 或重新组装静默继续

#### Scenario: 已组装 thread 切换 Provider profile

- **WHEN**SessionThread已有sealed assembly，用户或控制面把desired model/provider projection profile改为不同值，但既没有本次实际模型路由的明确选择，也没有实际compaction、rewind或有效ToolSet hard rebase
- **THEN**owner保持原applied profile和assembly，返回`provider-profile-change-requires-rebuild`且不dispatch；不得静默继续使用旧模型、绕过路由授权创建epoch或用no-op ToolSet变化伪造rebase

#### Scenario: 跨 Provider fallback 保留冻结上下文

- **WHEN** 实际路由选中的 Provider 在请求或流迭代阶段失败，随后选择不同协议或 profile 的候选，且本次不需要实际compaction
- **THEN** owner 在同一封存事务发布 `provider_changed` epoch 和新 profile 请求，旧 assembly 保持不可变；root 正文及 provenance、已有 canonical/source frame 的正文、角色和顺序完整保留，只转换投影并按原合同追加新 item。失败和成功请求均保留各自可重建的 request hash；工具循环后再次选择候选以及进程重启恢复仍能读取全部历史，不因上次失败的 profile 锁死

#### Scenario: 合法边界应用 pending Provider profile

- **WHEN**desired profile已变化，随后实际compaction、rewind或有效ToolSet hard rebase建立合法新epoch
- **THEN**owner把新profile、对应projection bytes和首个新epochassembly原子applied并保存旧/新profile provenance；initial assembly则可直接采用当时desired profile

#### Scenario: 合法重建边界

- **WHEN** 首次组装、实际上下文压缩、rewind 重建 active view或 ToolSet hard rebase发生
- **THEN** 系统创建新的 prefix epoch并记录 `initial`、`compaction`、`rewind`或 `toolset_changed`原因；新 epoch内的后续请求重新遵守字节级稳定约束

#### Scenario: ToolSet 变化不得沿用旧 epoch

- **WHEN** 下一次模型请求的 ToolSetRef或工具 policy hash不同于当前 applied binding
- **THEN** 系统不得在旧 prefix epoch继续 seal或只追加工具切换文本，必须先执行 ToolSet hard rebase

#### Scenario: 小窗口fallback需要真实压缩

- **WHEN** 实际fallback模型的容量不足以容纳旧冻结输入，而可用候选能够适配
- **THEN** 原owner先显式应用compaction并按新预算封存该候选模型请求，保留compaction与provider_changed真实原因；旧失败请求和assembly保持不变，新正文拥有新的请求身份

#### Scenario: 仅后台候选完成

- **WHEN** 当前请求仍在运行，后台摘要候选ready
- **THEN** 不替换当前请求正文、不推进source applied revision或prefix epoch，只在下一合法请求准备边界按真实预算决定应用

## ADDED Requirements

### Requirement: 派生摘要封存不能推进主上下文消费

后台摘要请求 SHALL 独立封存其冻结输入，沿原派生请求选择合同保持与主请求隔离。其封存／完成不得推进主上下文latest assembly、prefix epoch、source applied checkpoint、待注入通知消费或ToolSet绑定。后继普通请求只从真实主assembly续接，不能以“最后封存的任意请求”判断主前缀。派生输入只读取冻结来源，不安装当前Skill目录、观察新文件或修改主live CSM。候选范围的资源保留由原owner显式持有。

#### Scenario: 摘要是最近封存的请求
- **WHEN** 前台请求P后，后台摘要S完成封存，随后普通请求Q开始
- **THEN** Q仍沿P的主prefix及真实追加内容继续，S追加的任务指令、拒绝结果与输出不进入Q；S保持可复用来源root／tools但其执行权限收窄不改变主ToolSet，主来源applied状态未被S提前确认

#### Scenario: 摘要期间Skill发生变化
- **WHEN** 摘要只读取冻结Skill revision A，主运行当前发布revision B
- **THEN** 摘要只使用其已保存A引用，不刷新主CSM或把B标记已消费；候选采用按实际依赖判定，下一主请求正常处理B
