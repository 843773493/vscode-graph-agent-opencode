## Purpose

统一本项目受支持运行时配置的继承、来源解释和变更失效方式，使文件、实体选项和已有调用覆盖使用相同的单父节点规则。用户可以从一条真实链路理解最终配置，同时保持不同机器、Gateway 和工作区之间的所有权及运行快照边界。

## ADDED Requirements

### Requirement: 每个配置节点必须只有一个明确父节点

配置节点 SHALL 包含本地 settings 和零或一个 `extends`；系统 MUST 仅沿选定入口的实际父链计算有效值。文件与内嵌选项节点 SHALL 使用同一规则，多个消费者可以共享祖先；整体按 owner 形成森林，不要求所有配置属于同一根。系统 MUST NOT 在父链之外再叠加固定来源顺序、多父 include、按字段父节点或代理专用优先级。

#### Scenario: 用户绕过一个默认来源
- **WHEN** 用户将工作区入口的父引用改为一个合法自定义同域文档，该文档不继承原 user_local
- **THEN** 原 user_local 的字段不再影响结果，诊断展示自定义链，不因旧文件仍存在而继续叠加

#### Scenario: 声明多个父节点或循环
- **WHEN** extends 为数组，或规范化后的祖先形成循环
- **THEN** 候选失败并指出原来源和问题链，当前 active 保持，不跳过异常节点后继续启动

### Requirement: 文件和内嵌节点必须使用相同信封与稳定引用

受支持配置文档 SHALL 以域版本、可选 extends、settings 和可选 profiles 表达定义。版本、schema、extends 和 profiles MUST NOT 作为普通 settings 合并。Provider／Model 的 config_ref SHALL 只选择一个节点，不叠加另一套选项；内嵌节点本体 MUST NOT 跨文件隐式拼接。文档身份 SHALL 使用所属 owner 的规范化来源 URI；定义身份为文档身份和节点位置，绑定节点身份还区分 owner／workspace，不能以显示名或 basename 推导。

#### Scenario: 不同文件包含同名 profile
- **WHEN** 两个文件都定义 profiles.fast，模型明确引用其中一个
- **THEN** 只使用被引用节点，另一份同名定义不参与合并，来源可准确跳转

#### Scenario: profile 只是被定义
- **WHEN** 一个文档声明 profiles，但没有任何已配置消费者选择它
- **THEN** profile 不自动成为文档 settings 的覆盖层，也不对普通业务 URI递归追踪

### Requirement: 引用必须保留声明来源与执行上下文

相对引用和 fragment SHALL 基于声明它的原来源解析；继承来的引用必须保留该来源基准。上下文引用 SHALL 明确绑定本次 owner、workspace 和快照，且解析后仍为一个父节点。系统 MUST 拒绝缺失来源、非节点位置、未知别名、跨 owner／配置域的越权父引用及不受支持的网络父文档，不通过读取远端或扫描目录补齐。

#### Scenario: 继承来的相对引用
- **WHEN** user 文件的模型 config_ref 为 `#/profiles/fast`，工作区继承该目录且自己也定义同名节点
- **THEN** 引用仍指 user 文件的 fast，不被工作区同名节点劫持

#### Scenario: 两个工作区共享 Provider 定义
- **WHEN** A、B 使用同一 user profile，该 profile extends 为 context:workspace
- **THEN** 各自绑定到 A、B 的实际入口与快照，不跟随前端激活工作区，不复用另一工作区的来源结果

### Requirement: 合并语义必须统一且保留明确覆盖意图

缺键 SHALL 继承；标量、false、0 和数组 SHALL 明确替换；普通对象 SHALL 递归合并，空对象不清空父对象。schema 声明 `x-merge: replace` 的对象 SHALL 整体替换；null 仅按字段 schema 作为明确值解释，不作为通用删除符。删除本节点定义 SHALL 恢复其父链结果，不能复制有效值作为恢复动作。相同值的本地显式覆盖 MUST 保留。

#### Scenario: 空数组和零值
- **WHEN** 子节点将父数组设为空数组，将允许零值的字段设为0，将布尔项设为false
- **THEN** 有效值分别为空数组、0、false，不因空值判断恢复父值

#### Scenario: 整块替换和恢复继承
- **WHEN** 子节点对 replace 块写入合法完整新块，随后删除该本地块
- **THEN** 写入时不残留父块子字段，删除后才重新采用父块；普通空对象和非法 null 不充当该删除动作

### Requirement: 解释和执行必须消费同一个有效配置结果

系统 SHALL 为有效节点返回实际父链、最终值和字段定义的节点／来源／原文位置；原子块来源须一致。表单、JSONC 预检、目录发现、测试及真实执行 SHALL 使用同一解析结果，不在前端或消费者另算优先级。秘密值 MUST 保持引用或脱敏，不因 inspection 解密或回传环境代理认证。

#### Scenario: 最终值相同但定义位置不同
- **WHEN** 子节点明确设置与父相同的值
- **THEN** inspection 仍标为本地覆盖，父后来改变时该值保持；恢复继承后才跟随父变化

#### Scenario: 参数名称在输入规范化
- **WHEN** 合法参数别名按协议无关逻辑键或 header 按大小写无关键在 schema 边界规范化
- **THEN** 合并结果仅在最终发送时按最终协议映射，来源仍定位原文真实键，同节点规范化后重复键明确失败

### Requirement: 依赖变化必须沿原快照边界生效

文件正文、父引用、可达节点、source presence 和已捕获环境代次 SHALL 属于候选依赖。父链变化须重建受影响候选并执行所属 owner 的既有 CAS、重载分类和发布；旧 active 与已封存调用不得被直接改写。解析与完整校验 SHALL 在启动、来源导入或显式预检边界发生；普通暖读只消费不可变有效快照，不重新扫描图、读取父文件或探测代理。

#### Scenario: apply 时父配置发生变化
- **WHEN** 候选准备后，一个父来源或父边发生变化
- **THEN** 原候选不能以旧依赖基线 promotion，按既有冲突和补偿合同处理，下一次有效结果显示真实来源

#### Scenario: 修复缺失的父文件
- **WHEN** 热重载因新父不存在失败，用户随后补齐该明确声明的文件
- **THEN** owner 仍保留旧 active，重新观察并校验修复后的候选，不要求通过切换工作区重建另一套 watcher

#### Scenario: 无变化的配置暖读
- **WHEN** 对有效快照执行至少20次不超过512KiB的配置 inspection 暖读
- **THEN** 从完整请求到完整响应的 p95≤500ms、单次≤1500ms，且不随每次读取父文件、解密或进行网络探测；已有更严格预算继续适用
