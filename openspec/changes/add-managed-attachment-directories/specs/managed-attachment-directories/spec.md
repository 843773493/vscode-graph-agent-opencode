## Purpose

将用户上传附件作为可由现有文件工具和 Shell 处理的工作区目录资源，统一目录关联、使用占用、历史版本、fork 和延迟回收，使异常退出后的清理可核验且不依赖 Agent 主动释放。

## ADDED Requirements

### Requirement: 附件目录是工作区级资源与回收单位

每次独立上传 SHALL 创建具有稳定资源身份的独立附件目录，包含原始文件、预览及后续处理结果。相同字节的独立上传 MUST NOT 合并可变目录身份。Thread 关联和回收 SHALL 作用于整个目录，MUST NOT 为每个派生文件另建 Thread 引用计数。原件 MUST 保留，处理结果 SHALL 写入新文件或子目录；具体文件的完整性摘要 MUST NOT 被当作可变目录的整体身份。工作区访问权限与用于保活的 Thread 关联 SHALL 分离，读取本身不产生永久关联。

#### Scenario: 相同内容被独立上传两次

- **WHEN** 用户在两个 Thread 分别上传相同字节的文件
- **THEN** 两次上传得到独立目录资源和关联；一个目录中的处理结果不出现在另一个目录，解除一个关联不影响另一个目录的保留

#### Scenario: Shell 生成处理结果

- **WHEN** Agent 在附件目录下生成多个文本、图片或解压后的文件
- **THEN** 文件可通过原文件操作链路继续使用，原件不被覆盖，目录整体保活且不需要逐文件 attach

### Requirement: 文件工具与 Shell 使用同一附件目录 URI

附件目录及子文件 SHALL 使用统一 URI owner 登记的 workspace 模板和 child 规则。模型消息 SHALL 提供目录 URI、原件 URI及必要元信息，并提示处理结果保存到该目录的新文件或子目录。Shell 的结构化工作目录 SHALL 在目标机器解析为原生 cwd 后执行，命令正文保留操作系统路径语义；MUST NOT 扫描替换任意命令字符串中的 URI。URI 不属于 Shell 目标时 SHALL 明确拒绝，不得在中继进程或其它机器解释。普通响应和持久业务引用 MUST NOT 泄漏真实附件目录路径，既有具名管理/Browser例外不扩张到本入口。

#### Scenario: 远端 Shell 处理附件

- **WHEN** Agent 将远端附件目录 URI 作为 Shell 工作目录，命令读取相对路径并生成派生文件
- **THEN** 所有文件操作发生在 URI 指定的目标机器和目录，使用原文件 owner/PTY 链路，响应继续使用规范 URI

#### Scenario: URI 目标与 Shell 目标冲突

- **WHEN** Shell 绑定的工作区与工作目录 URI 的目标不同
- **THEN** 在启动命令前明确拒绝，不能转换为本机相对路径或静默切换目标

### Requirement: 最后解除关联后默认保留附件目录三十天

上传 SHALL 经现有接受边界自动关联发起 Thread，Thread runtime 卸载、Thread 删除或 Session 删除 SHALL 由后端通知附件 owner 幂等解除精确关联。最后一个关联解除时 SHALL 持久记录零关联起点与默认 30 天回收期限；重新 attach SHALL 取消该期限，下一次归零重新计时。上传、排队输入、fork 和文件处理的有效操作占用 SHALL 阻止回收。周期检查 SHALL 使用低频到期查询，普通重启 MUST NOT 重置期限，也不要求精确到秒触发。删除前 SHALL 与新关联/占用互斥复核并登记可恢复删除状态，失败明确可见且可重试。Shell/Browser 原有临时资源的 30 分钟或 unload 立即清理规则 MUST NOT 扩张为附件规则。

#### Scenario: 三十日前重新关联

- **WHEN** 一个上传很久的目录刚刚解除最后一个关联，随后在第 29 天重新 attach
- **THEN** 目录未被按创建时间回收，原回收期限取消；再次归零后获得新的完整 30 天期限

#### Scenario: 到期时仍在处理文件

- **WHEN** 目录零关联超过 30 天，但 Shell 或其后台处理进程仍有经 owner 核实的占用
- **THEN** 目录不被删除，操作结束后再次复核到期、关联和占用，不能把工具调用提前返回视为进程已停止

#### Scenario: 回收与重新关联竞争

- **WHEN** 到期回收与 attach 或 fork 保留同一目录并发
- **THEN** 只有一个串行顺序生效；关联先行阻止删除，删除状态先行则新操作明确报告不可用，重启继续原删除记录而不复活空目录

### Requirement: 原件回收不得改写历史请求或破坏恢复依赖

canonical 历史引用 SHALL 保留原资源身份、来源和具体已用文件版本；已封存的模型输入、预览与请求 hash SHALL 从原历史 owner 的冻结内容重建，MUST NOT 读取当前目录猜测过去输入。历史里出现 URI 本身不构成无限期目录保活；无关联目录按期限回收后，附件查看或需要原件的新操作 SHALL 明确报告不可用。checkpoint/assembly 恢复仍依赖的冻结资产 MUST 保留，不能随目录 GC 删除。

#### Scenario: 派生文件变化后恢复旧请求

- **WHEN** 某派生文件已用于一次模型请求，之后文件被修改或整个附件目录被回收
- **THEN** 原请求仍可使用冻结输入重建并核对原 hash；查看当前原件明确反映当前可用性，不篡改 canonical 或从现有配置补造过去输入

### Requirement: Fork 通过原资源处理合同保留目录

fork SHALL 沿用统一资源目标检查及现有 journal/claim/publication，按所选历史的依赖保留目录。同工作区 SHALL 复用原目录身份并为目标建立关联；跨工作区 SHALL 按冻结目录版本复制为独立目标资源。历史来源身份及 URI MUST NOT 被改写为目标历史。必需目录已回收时 MUST 明确拒绝依赖它的 fork，不能创建空目录伪造成功；可选目录省略时 SHALL 保留不可用原因。预留失败或目标删除 SHALL 收敛本次关联，不能留下无限保活。

#### Scenario: Fork 后删除来源 Thread

- **WHEN** 同工作区 fork 已发布并关联所需附件目录，随后来源 Thread 被删除
- **THEN** 只解除来源关联，目标关联继续保活同一目录，历史来源身份保持不变

#### Scenario: 跨工作区复制期间目录改变

- **WHEN** 跨工作区 fork 捕获目录版本后，派生文件集合在发布前改变
- **THEN** 目标只发布冻结版本或明确冲突，不产生混合时刻的文件集合，失败不发布半个目标

### Requirement: 异常退出的占用与关联必须有 owner 对账

系统 SHALL 根据后端/Thread runtime 代次、权威 Thread 与任务状态收敛失效占用及关联，MUST NOT 只依靠 Agent 发送 detach 或仅看 Thread ID 仍存在。旧代迟到释放 MUST NOT 移除新代关联；cold Thread 和尚待执行输入 MUST NOT 被当作已删除。失去持有者不证明外部资源已停止；Shell、Browser、后台文件处理仍 SHALL 由原 owner 终止并核实，未知结果保持可发现的待核实状态与管理操作，不得伪报已释放。该对账 SHALL 复用原账本，不创建第二套资源事实源。

#### Scenario: 已 attach 后后端崩溃

- **WHEN** 后端在关联资源后退出且未发送 detach，随后重启
- **THEN** owner 按原持久事实区分待恢复任务、新代关联和已失效持有者，解除可证明失效的关联并开始相应回收规则，未知外部进程仍要求核实

#### Scenario: 创建请求取消或回执丢失

- **WHEN** 创建 intent 已登记原 operation lease，但创建 HTTP 请求被取消、超时或未收到正式结果
- **THEN** 原 lease 保持待核实并跨调用收尾、Turn 清理和重启保留；存活快照不能自动核实创建结果，只有原 owner 核实终态或本次正式创建结果后才能结清

#### Scenario: 创建在接纳前明确拒绝

- **WHEN** 外部 owner 确认尚未接纳具有唯一 resource/workspace/revision 的本次创建，并返回绑定该 intent 的正式未创建回执
- **THEN** Python owner 核对身份与版本后关闭该 intent、解除关联并结清其原占用；普通 HTTP 错误、冲突身份和其它创建的回执不得触发同样处理

#### Scenario: Terminal 在就绪前退出并取得停止证明

- **WHEN** 已接纳的 Terminal 在就绪前退出，原 Worker 沿唯一进程树终止链核实停止，宿主确认 Worker 退出且终态持久化成功
- **THEN** 外部 owner 保留原资源身份与恢复记录，返回绑定本次 intent 的正式失败回执和 verified 终态快照；Python owner 核对身份、整数版本及终态后结清本次 lease 和受管工作目录占用，回执丢失后可从原终态记录恢复对账，不重放创建

#### Scenario: 启动异常尚无进程树停止证明

- **WHEN** native 启动抛错、Worker 异常退出或清理失败，尚未取得本次 PTY 进程树的正式停止证明
- **THEN** 外部 owner 保留已取得的原身份与未知记录，不签发未创建或已停止回执；Python 保留本次待核实 lease，Worker 退出、已发送信号及普通 HTTP 错误均不能代替停止证明

#### Scenario: 正式停止回执必须属于本次版本

- **WHEN** Terminal 或 Browser 的停止/删除请求收到完整外部回执
- **THEN** owner 在更新终态和结清占用前核对资源、工作区及本次发布的整数版本；旧版本、未来版本或布尔版本均不得作为本次成功证明，冲突保留原精确 lease 与待核实状态

#### Scenario: Terminal 停止确认或终态写盘失败

- **WHEN** 原 PTY 进程树已进入停止链，但 Worker 退出未确认或最终终态写盘失败
- **THEN** 保留原 Worker 句柄和未知状态，不提前发布停止完成事件；显式重试复用原执行及唯一停止链，只有进程树停止、Worker 退出确认与终态写盘均成功后才清除句柄和返回成功；未知状态写盘也失败时保留两份原异常及因果关系

#### Scenario: Browser 已关闭运行时但最终发布失败

- **WHEN** Browser 已关闭原 context，但删除检查点或最终终态写盘失败
- **THEN** 原外部 owner 将该关闭结果保留为 unknown 并明确返回失败，Python 保留原精确 operation lease；下一次显式关闭沿原转换链完成检查点删除与终态落盘，不能凭内存中的 deleted 状态提前返回成功，也不能创建替代 context；关闭与 unknown 写盘都失败时保留两份异常及 cause

#### Scenario: 未知资源由新管理操作核实停止

- **WHEN** 用户从现有资源管理入口对 unknown 或 reconcile_required 的资源显式发起 stop，或对无关联资源显式发起 delete
- **THEN** owner 先核对外部资源身份与当前传输版本；已有正式终态时只对账，否则为新管理请求创建独立 operation lease，提升并同步版本后复用原停止链；本次正式终态回执与 owner 持久化成功后才结清原待核实占用，不能把新停止当作旧工具动作成功

#### Scenario: 显式停止不能绕过仍有效的操作或复用未知身份

- **WHEN** 管理停止仍遇到 active lease、重复的未知 operation 身份、不匹配的外部版本或尚未核实完成的删除
- **THEN** 原 owner 明确拒绝冲突并保留原占用；普通资源列表为身份和版本可核对的未知资源提供候选停止/删除动作，使用、关联和授权仍须 verified；自动恢复、卸载与扫描不得据此重发未知副作用，缺少外部身份或 native 停止证明仍保持待核实

### Requirement: 单个 Thread 清理失败不得停止其它 Thread 的周期回收

闲置清理 SHALL 独立尝试每个候选 Thread；失败 SHALL 保留该 Thread 的 scope/generation 和未核实资源，并明确报告 owner 与原始原因。事件循环正常调度时，扫描对单个卸载任务的等待 SHALL 最多为 30 秒，到期明确报告尚未完成并继续其它候选 Thread。该预算 MUST NOT 被解释为外部资源已经停止；唯一在途卸载任务 SHALL 继续由原 owner 持有，后续扫描 MUST NOT 为同代重复发起清理，新 admission MUST 等原任务完成。迟到成功 SHALL 沿原链释放，迟到失败 SHALL 记录原始原因并保留状态供后续周期重试。周期 worker SHALL 记录整批异常并继续原周期，不得因单项失败永久停止或把失败声明为已卸载；任务取消与关闭 MUST 正常传播。

#### Scenario: 一个资源 owner 暂时不可达

- **WHEN** 两个 Thread 同时到达卸载条件，第一个 Thread 的清理回执失败
- **THEN** 第二个 Thread 仍完成卸载，第一个保持待清理，错误可见且后续周期继续重试，核实成功后才报告该 Thread 已释放

#### Scenario: 清理回调超过本轮等待预算

- **WHEN** 第一个 Thread 的 listener 或 scope close 尚未完成，扫描等待达到 30 秒
- **THEN** 本轮报告精确 owner/generation 的等待超时并检查其它候选；原任务不被取消、资源不被标记为已释放；再次扫描不会重复启动清理，新 admission 不能复用仍在卸载的 runtime

#### Scenario: 超时后才收到清理失败回执

- **WHEN** 本轮已结束等待，原卸载任务随后收到真实失败
- **THEN** 后台回调完整记录原错误和 owner，保留失败 scope/generation，后续周期可以沿原 owner 重试，不能丢失异常或提前创建下一代 runtime

### Requirement: 挂起操作的终止与关闭期限必须由原 owner 核实

扫描等待预算 SHALL 与真实 operation 的截止时间和终止事实分开。挂起资源操作 SHALL 由原执行 owner 在截止时间后取消或终止并核实；锁等待、线程池排队、多次网络调用和迟到回执 SHALL 纳入同一 operation 的处理，MUST NOT 仅依靠单次 HTTP 超时。无关资源 SHALL 不因另一个挂起操作无限等待共享 owner 锁；同步阻塞代码 SHALL 不阻断整个事件循环的扫描与诊断。换代 admission、Session 删除和 shutdown SHALL 有明确等待期限及失败返回，期限到达时保留原未核实资源、lease、scope 和必要 locator，MUST NOT 在仍有副作用时宣称排空或物理删除恢复依据。该完整合同属于后续 5.4，本轮扫描有界等待不足以证明它已经实现。

#### Scenario: 挂起释放占有共享 owner 锁

- **WHEN** 一个资源的真实释放未收到回执，其它无关资源也需要同一 owner 处理
- **THEN** 无关资源能够独立推进，挂起操作保持唯一执行身份并交由原 owner 终止核实，不通过丢弃锁内任务或重发删除伪造进度

#### Scenario: Session 删除排空超过期限

- **WHEN** Session 删除仍在等待未核实的卸载操作且达到关闭期限
- **THEN** 删除入口明确返回未完成及可定位原因，保留后续核实/恢复所需事实和 locator，不能报告已删除后让原操作继续修改已移除的数据

#### Scenario: Browser 关闭遇到已接纳的准备或交互操作

- **WHEN** Browser Manager 关闭时，原交互正在准备唤醒、串行排队或并发执行，包括资源仍显示为冷回收的准备窗口
- **THEN** 每个运行资源先关闭操作准入，再通过原队列的完整已接纳集合等待原操作；单次排空预算不随阶段重置，成功后才冻结和保存最终检查点，空闲冷资源保留原检查点；Session 排空超时保留原操作、连接与锁，明确失败，不以在途计数为零或冷状态宣称原操作已经结束

#### Scenario: Browser Manager 排空超时后保留恢复依据

- **WHEN** 原 Session 的已接纳操作在 Manager 关闭排空预算到期后仍未结束
- **THEN** Manager 继续检查其它资源并汇总原排空错误，明确返回关闭失败；不将超时资源改为 lost、不删除其原检查点或关闭共享运行时，原操作与关闭准入保持，原操作结束后可通过同一关闭入口重新捕获最终检查点并完成关闭

#### Scenario: Browser Manager 的多个资源共享排空预算

- **WHEN** Manager 关闭时当前多个运行资源仍有已接纳的准备或交互操作
- **THEN** 在等待第一个资源前关闭本批所有资源的操作准入，本批共用默认五秒的单调排空截止；后续资源只使用剩余预算，空集合可即时确认；超时按原资源汇总且保留原操作、检查点和共享运行时，不能将排空预算称为整个检查点持久化或 shutdown 的截止

#### Scenario: Browser Manager 关闭遇到创建中的资源

- **WHEN** 关闭开始时浏览器仍在配额准入、首次导航或最终状态保存，新创建请求也可能排队
- **THEN** 先关闭创建准入，未接纳请求以绑定原身份与版本的正式未创建回执拒绝；原已接纳创建继续由唯一创建链持有，创建和交互排空共用本批默认五秒单调预算。超时明确失败且保留原资源、Promise与共享运行时；迟到创建完成后不再接纳新交互，同一Manager重试沿原检查点链关闭并可冷恢复，不重放创建或把等待超时当终止证明

#### Scenario: Browser 创建失败的收尾未核实

- **WHEN** 原启动失败，随后资源释放、检查点删除或终态保存再次失败
- **THEN** 启动失败复用原关闭链保留failed/unknown与原句柄、原始异常和完整因果关系，不签发正式失败回执；Manager不能凭创建Promise结束关闭共享池，原管理关闭核实后才可重试整体关闭

#### Scenario: Browser 检查点失败后的收尾再次失败

- **WHEN** Manager 关闭时一个资源的检查点采集失败，随后原 context 释放、检查点删除或状态保存再次失败
- **THEN** 按该资源聚合采集与收尾原错误并继续其它资源；原资源保持原非终态 status 与 unknown、保留尚未释放的句柄，不关闭共享池；同 Manager 重试仍处理原资源，只有原收尾全部完成才落为 lost，unknown 保存也失败时保留两份异常和因果关系，不能伪报检查点已恢复

#### Scenario: Browser 撤销版本时旧操作未能排空

- **WHEN** owner同步新的传输版本，而原队列已接纳的旧版本操作仍在准备或执行
- **THEN** 先撤销旧连接和授权并持久化新版本，再以同一队列的默认5秒预算等待原集合；超时明确返回排空失败，保留原操作与运行时，不回退版本、不声明旧副作用已终止；迟到结果仍按旧版本拒绝，同版本重试复用原集合且不重放旧动作
