## Purpose

在现有本地 SQLite、canonical item 和执行恢复体系内，通过不可变版本引用、基准与增量重放减少长对话重复持久化，保留历史请求重建、消息顺序、同步异步执行、进程重启、回退及独立 fork 的原有用户能力，并用相同产品工作负载验证结构性收益。

## ADDED Requirements

### Requirement: 依赖更新保持执行与恢复协议一致

系统 MUST 使用已核实发布且满足本变更合同的依赖版本，低于目标的 LangGraph 和 checkpoint 依赖分别以1.2.16和4.3.0为升级目标，实施时已更高版本不得回退。模型节点、消息合并、序列化、同步/异步和检查点恢复合同 MUST 在升级片验收；升级成功不得作为存储去重完成的证据。

#### Scenario: 当前依赖低于目标

- **WHEN** 当前锁定和运行版本分别低于已核实发布的目标
- **THEN** 更新依赖并验证真实模型节点、消息合并、同步/异步保存与重启恢复，保留版本及发布来源证据

#### Scenario: 当前依赖已高于目标

- **WHEN** 实施时项目已使用更新版本
- **THEN** 保留更新版本并验证相同合同，不为匹配建议版本而回退

### Requirement: 复杂 channel 正文按不可变版本复用

系统 SHALL 在现有 SQLite 内以明确的历史owner、namespace、channel和version定位不可变状态值，checkpoint保存完整逻辑快照的版本引用。新版本才写入正文；未变化的大对象 MUST 依据new_versions和已冻结引用避免重复序列化与持久化。同一键被声明为不同内容、缺失引用或serializer/hash不匹配 MUST 明确报错。小型执行metadata可以完整保存，不要求通用JSON diff。

#### Scenario: 多个 checkpoint 沿用同一复杂值

- **WHEN** 连续checkpoint使用相同owner/namespace/channel/version且该值未变化
- **THEN** 它们引用同一不可变正文，正文份数及该版本的序列化次数不随checkpoint数增加，且每个历史checkpoint都能恢复独立对象

#### Scenario: 同版本发生内容碰撞

- **WHEN** 写入入口为已有owner/namespace/channel/version提供不同冻结内容，包括复用可变对象后修改其值的情况
- **THEN** 提交明确失败，不能覆盖旧正文或通过忽略新候选值伪报成功

#### Scenario: 相同版本在不同 Thread 或 namespace 出现

- **WHEN** 两个Thread或namespace使用相同channel名和version文本
- **THEN** 按各自历史归属解析，不互借正文，不因版本字符串相等而混合状态

### Requirement: 增量消息恢复复用官方语义并保持 canonical 唯一性

系统 MUST 在先确认现有canonical消息正文复用收益的基础上接入官方增量channel、消息reducer、序列化和恢复协议，形成基准加增量的单一链路。图状态、Saver写读、重放、fork及恢复 MUST 一起迁齐；增量只保存必要的不可变正文引用和控制信息，不维护第二份完整消息历史。新增、同ID替换、删除、清空、Overwrite和摘要后的顺序及工具配对 MUST 与实时执行一致。

#### Scenario: 多次工具循环后冷恢复

- **WHEN** 真实产品执行多次工具循环并生成并发工具结果，随后进程重启
- **THEN** 按冻结基准及父链上的增量恢复原消息身份、顺序和工具配对，不依赖内存对象或最新active view

#### Scenario: 替换删除与摘要组合

- **WHEN** 同一执行经历同ID消息替换、RemoveMessage、Overwrite和摘要/保留后缀
- **THEN** 冷恢复结果与官方消息reducer实时执行一致，原canonical事实不被覆盖或删除

#### Scenario: 父链重放顺序

- **WHEN** 一个checkpoint有多个并行task向同一channel写入
- **THEN** 祖先writes按官方task_path、task_id、idx顺序重放，只沿目标父链，目标checkpoint自身尚未应用的pending writes不提前进入其已提交值

#### Scenario: 无 snapshot 与损坏断链区分

- **WHEN** 非快照步没有保存完整channel值，或已登记的基准/中间依赖缺失
- **THEN** 前者按增量协议恢复；后者明确失败，不能把断链、None或缺失channel当作合法空消息

### Requirement: Assembly 与 view 共享可核验的历史内容

系统 SHALL 让多份assembly共享相同消息、工具定义、资源正文与封存前缀的不可变版本，保存有序引用及必要增量；view索引 SHALL 复用基准、范围或成员变化，避免为每次提交复制完整成员集合。系统 MUST 仅依据历史冻结内容重建规范化请求并校验原request_hash，保持选择、省略、可见性、保护级别与logical/context ordinal语义。不得读取当前文件/配置猜测历史，也不得将每次完整大JSON简单搬成独立文件后宣称去重。

#### Scenario: 多份 assembly 使用相同工具资源和前缀

- **WHEN** 多次模型调用沿用相同工具定义、资源正文和上下文前缀，仅增加少量新输入
- **THEN** 相同内容由多份assembly共享，单独的选择/贡献和新输入仍可追溯，每份历史请求重建后request_hash一致

#### Scenario: 当前资源已经改变

- **WHEN** 文件、配置或工具注册在历史assembly封存后发生变化
- **THEN** 历史请求仍只使用原冻结版本，内容缺失时明确失败，不用当前资源替代

#### Scenario: 视图共享后回退或隐藏成员

- **WHEN** 共享基准的view新增、隐藏或取消选择成员，再从深历史位置回退
- **THEN** 返回准确的visible/context_selected、logical/context ordinal和历史范围，未选择的旧后缀不混入当前请求

### Requirement: 去重引用与既有执行状态原子发布

系统 MUST 保留现有durable canonical正文和SQLite事务边界。checkpoint、状态版本引用、assembly、model call及资源绑定的相关发布 SHALL 按原原子性要求完成，不产生可观察的悬空引用或第二可变事实源。canonical item继续是业务正文唯一权威，恢复记录、投影、索引各有明确职责。

#### Scenario: 新正文写入后引用发布失败

- **WHEN** 新版本或canonical正文已准备，而原SQLite提交中途失败
- **THEN** 不暴露半个checkpoint/assembly，不伪报执行完成，恢复按原提交记录处理未发布内容

#### Scenario: 提交成功后进程退出

- **WHEN** 相关引用与执行状态已原子提交而进程立即退出
- **THEN** 冷恢复读取同一已提交状态，hash、owner和resource binding保持一致，不退回旧版本

### Requirement: Fork 保留来源身份及完整增量依赖

系统 MUST 在fork目标变为可用前保留恢复所需的基准、中间增量、状态版本、assembly和view引用闭包。历史来源身份与目标存储归属分离，历史version和业务hash不得改写；目标继续沿既有显式归属授权访问本地闭包，不能跨Thread混用或在线回源补齐缺失历史。

#### Scenario: 深历史 fork 后父来源删除

- **WHEN** 从真实产品生成的深历史checkpoint创建fork，随后删除允许删除的源会话并重启目标
- **THEN** 目标仍能恢复、重建历史请求和继续执行，保留原历史身份、顺序与hash

#### Scenario: 连续 A 到 B 到 C

- **WHEN** 已fork的目标再次被fork
- **THEN** C具有独立恢复所需闭包，历史source identity保持不变，正常运行不需要A或B在线

### Requirement: 清理保护 retained 历史与增量基准

系统 SHALL 以保留的checkpoint、assembly、资源快照、fork与原有retention引用为根保护完整依赖闭包。增量恢复所需的基准及中间writes MUST 保留，或先在事务内建立经过等价验证的新基准再回收；先回收无引用冗余，不默认只保留最新checkpoint，也不改变用户可回退/fork的历史点或保留期限。

#### Scenario: 中间增量仍被深历史点依赖

- **WHEN** 旧增量或基准仍被任一保留checkpoint、assembly或fork引用
- **THEN** 清理保留它们，或原子建立等价新基准并切换引用；提交后所有保留状态仍可冷恢复、回退和fork

#### Scenario: 无引用内容回收

- **WHEN** 不可变版本或prefix/view节点已不在任何保护闭包内
- **THEN** 可以回收该冗余内容，保留历史点的身份、消息、工具配对、资源与request_hash均不变

### Requirement: 结构迁移与收益验收各有明确边界

系统 MUST 仅在实际结构变化时提升schema版本，明确支持的旧库升级范围、失败恢复和无双轨切换；纯SQL文件提取不要求数据迁移。验收 MUST 经真实Gateway、目标工作区及产品执行/fork/恢复入口生成关键状态，覆盖同步与异步，并以相同负载保存优化前后的各表体积、正文/索引增长、序列化次数/字节、写入和恢复耗时。不得预先承诺固定压缩比或全链路线性增长。

#### Scenario: 相同负载前后比较

- **WHEN** 比较包含多轮工具循环、并发结果、摘要、进程重启、深fork与回退的相同产品工作负载
- **THEN** 正确性断言全部保持，并分别证明未变化channel正文不重复、多assembly共享相同内容、回收后保留状态仍可恢复和fork，记录原命令、源码/依赖版本、原始结果及各层测量

#### Scenario: 手工历史与产品生成历史

- **WHEN** 测试需要覆盖查询展示、损坏或旧库迁移边界
- **THEN** 可以手工准备局部装具，但完整执行/fork/恢复验收的关键状态由产品入口产生，不能由装具代替待验证的产品步骤

#### Scenario: 性能长尾暂未解决

- **WHEN** 正确性和结构性去重已验收，但部分写入或恢复耗时短期难以优化
- **THEN** 留下包含工作负载、原始测量与未达目标的TODO，不放宽正确性断言或原业务期限，也不把依赖升级或局部helper通过声称为整体优化完成
