## Context

动机见 proposal。统一寻址已拥有本地 grammar/resolver，但当前 Skill 固定入口形态、整体百分号拒绝和工作区文件工具的路径解释无法构成完整导航合同。附件与工具输出仍生成 `boxteam-session://`。公共根/编码的唯一规范归统一寻址 change；Skill 激活归 context change，通信目标归现有 Session change。

## Goals / Non-Goals

**Goals:** 一个文件 owner 消费一个 canonical target；本地/远端相同操作合同；普通文件无需逐项注册；失败明确体现真实写入效果。

**Non-Goals:** 不将终端、浏览器、团队变成文件系统，不新建跨端执行平台、通用任务账本、文件快照库或事件订阅。首批迁移既有文本补丁的新增/修改/删除/同根文件移动；新增通用目录 mutation、二进制通用写入和跨端复制独立定义。

## Decisions

### 1. 一个内部目标，按资源形态分派

read/write 接收完整文件 URI，ls/glob/grep 接收根 URI 与独立查询参数，apply_patch 接收一个根 URI 与相对文件 patch。全部由寻址 owner 归一为固定 Gateway/workspace/root/child binding，再由目标文件 owner 执行。现有受信调用方若有普通相对路径，在其显式工作区边界构造同一 URI；不维持另一套解释规则，也不允许裸路径在中继端重新绑定当前工作区。拒绝万能目录映射：领域对象不暴露磁盘树，Skill 目录读取不调用 CSM。

### 2. 实际 owner 就地执行，路由不是持有数据

工作区文件操作由目标工作区 owner 执行；Gateway/inline 技能由各根所属 owner 提供内容能力，不让 Workspace 读取对端真实路径。复用 federation channel、认证和有界星型路由，分别登记文件操作授权，保留既有 send/read/wait grant 的边界。固定 target 与单次 channel lease 分开；断线不可重新裸 ID 选目标。选择沿现有路由扩展，避免另一套远程文件服务或全局业务数据库。

### 3. 轻量读取，不伪装历史快照

目录/搜索返回 URI、shape、可用操作、相对显示名及有界匹配。使用显式 limits/deadline 和与目标/查询绑定的 opaque continuation，普通可变视图披露变化，无法继续时明确失败，不承诺固定历史分页。只为实际读取/条件写入计算原始 byte SHA-256；不使用 mtime 冒充内容版本，不为搜索建逐文件 ResourceIdentity。上下文追踪继续进入原 source owner。

### 4. 条件写入与多文件发布结果

write/edit/patch 的内容前置使用精确原始 byte `sha256:<hex>` 或该 owner 已有等价 revision；创建使用 expected_absent。owner 在一个根内串行自己管理的 mutation，先校验所有 preimage/能力/patch、准备结果，再在每次发布前重新核当前 preimage；同目录临时文件经完整写入和原子替换发布，失败回收自己的句柄和未发布临时文件。

既有 `*** Delete File` 和 `*** Move to` 同步接入该 owner。删除校验原文件的预期内容版本；移动的 source 和 destination 必须同根，分别绑定 source 版本及 destination 的 expected_absent 或明确预期版本，不能无条件覆盖目的文件。preflight 校验所有受影响路径，包括移动的两个端点。移动目的发布与源删除是两个可观察步骤，不伪称两路径原子事务；结果逐路径记录存在状态、已核 hash 和执行知识边界，目的已发布但源仍在时不得报移动完成。目录、二进制一般节点的 move/remove 与递归操作不由文本补丁能力推导。实施须复用既有 add/delete、hints/move 正式用例，保留补丁输入和内容/changeset断言，只迁目标和条件写入合同。

普通操作系统不提供针对任意外部写入者的跨文件 CAS/事务。owner 串行化只约束本服务的写入者；外部修改需前置重核和发布后核实，不能宣称关闭所有外部竞争窗口。能力不足的目标明确拒绝相应保证，不添加假 CAS。单文件不暴露半截正文；跨文件不假装全局原子事务。遇到失败立即停止，逐文件报告 published/not_published/not_attempted/unknown 与验证到的 hash；不以自动反向补丁覆盖后来写入。对于要求更强外部 CAS 的场景，另设可验证机制后再公布能力。

### 5. 执行结果与寻址拒绝分开

寻址三个错误域引用统一 owner；文件执行原因只在 resource-file-operations spec 登记。确定拒绝和 preflight 失败为未执行；发布完成才为完成；中途失败为部分；无法观察实际效果为未知。调用 cancel 与确认停止分开，source deadline 不抹去目标可能的写入。未知结果先对原目标核实，禁止自动重放。复用现有工具调用身份记录诊断；不预设所有文件操作需要新 operation_id 或 durable coordinator。

### 6. 会话产物仍是业务文件引用

附件与工具结果在 session 模板下公开单文件 URI（模板唯一登记于寻址 owner），保留原业务 ID 和 owner。当前工具输出物理文件名为tool_<digest>.txt，不能把它当成通用身份段；没有独立result ID时由原owner基于现有稳定tool_call_id派生/公布规范引用，物理文件名留在owner内部，不新增通用身份注册库。迁移清点生成器、store/parser、middleware、大输出引用、消息附件、Provider 和 Web/历史 projection，完成后删除旧 scheme 常量/分支。现有附件二进制读取通过原接口保留，不把它扩成通用文件写入能力。不迁旧开发生成数据、不暴露会话私有目录。

现有 `read_file` 经 workspace filesystem backend 返回图片媒体 block，Provider 侧已有 `project_tool_message_content`；Web 的附件 content 接口经 `AttachmentBlobStore.read/read_thumbnail` 返回原件或缩略图。附件的这两条消费者统一从资源 URI 分派到原附件 owner，不能让普通文本的 UTF-8 要求废除既有媒体读取。`original`/`thumbnail` 保留原 owner 参数，URI 只描述同一附件身份；不新增 attachment 工具、URI query/view 解析器或绕过 owner 的 SessionArtifactBackend 私有目录映射。迁移后 workspace 文件读取同样保留既有受支持媒体能力，仍归其工作区文件 owner；首批新增写入仍仅文本。

## Risks / Trade-offs

- 文件名编码与现有身份 profile 混用 → 公共 grammar 先交完整构造/解析往返与拒绝矩阵，所有消费者只引用，不再加 parser。
- 远端路由虽正确但文件仍在 source 执行 → 两端同名不同内容/标记文件验收，验证远端修改及 source 不变。
- 外部编辑器竞争/进程崩溃 → 诚实披露 CAS 能力边界和部分/未知效果，以原目标内容核实，不伪造回滚。
- 改进 Skill 导航误增 activation → 读取前后对 catalog/source-control/activation 记录逐项断言不变，显式 skill_load 才进入来源事务。

## Migration Plan

1. 先完成统一寻址的根、路径编码和能力登记；现有 context 规范删除通用读取禁令但保留唯一激活 owner。新增任务不因规划已写而勾选。
2. 先落一条技能目录只读纵向链路，再通过同一合同实现目标端读取/搜索，验证真实远端与固定目标。
3. 接工作区源码根和条件写入/补丁；验证冲突、preflight 零写入、部分发布、断线未知及重开核实。
4. 平移会话产物全部直接和间接消费者、协议与测试；删除旧解析链与 scheme，开发中间数据按已授权清理。
5. 按 tasks 逐项验收。公开破坏性改动同闭包一次交付，不保留旧链兼容回退；若候选失败则不发布该候选，已发生的目标写入按真实 owner 结果处理。
