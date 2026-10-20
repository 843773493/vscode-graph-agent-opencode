## Why

已有本地虚拟文件后端和资源 URI 解析，但目录导航、普通文件访问和跨端文件执行还没有统一消费合同。模型无法稳定访问技能包的多层文件，也容易把当前激活工作区、资源生命周期与一次文件操作混为一谈。

## What Changes

- **BREAKING** 将 `ls/read_file/glob/grep/write_file/edit_file/apply_patch` 接入统一资源寻址的唯一解析器和文件 owner；搜索条件与目标根分开，结果返回可继续访问的资源 URI。
- 普通文件目标保留相对路径、绝对路径和资源 URI 三种输入，引用统一寻址 owner 的原生路径输入合同；在明确目标机器/工作区或目录上统一解析，不用 source/relay 当前目录。工作区外绝对路径按既有能力和授权访问或明确拒绝，不重定向到工作区内、不因 URI 化强制先注册；Windows 路径按目标平台解释。
- 文件树必须继续浏览、编辑未注册目录，快捷目录仍可保存和重开；引用统一寻址 owner 补齐的规范地址，不伪造 workspace_id、不强制或自动注册，不能以缺少 URI 模板为由删功能。
- 消费 `add-unified-virtual-resource-addressing` 定义的目录根、单文件、领域对象与分段编码；技能包和工作区文件树支持导航，领域对象不因拥有 URI 而成为文件目录。
- 搜索和读取不逐文件注册长期 ResourceIdentity 或快照；普通 hash 按读取/冲突检查需要计算。显式上下文追踪和历史恢复仍由原来源 owner 管理。
- 固定目标 Gateway、工作区和资源根，复用既有联邦路由；列目录、搜索、读取与补丁在目标 owner 所属进程执行，不可达或不支持时明确失败。
- 规定写入前置 hash/revision、一次补丁一个根、单文件原子发布和多文件部分失败合同；超时、取消和响应丢失不伪报目标未执行或整体回滚。
- **BREAKING** 将附件及工具输出的 `boxteam-session://` 生成、消费、工具结果和历史投影迁入统一寻址，生命周期、业务 ID 和授权仍归附件/工具结果 owner。
- 通用读取 Skill 文件只返回内容；`skill_load` 继续独占冻结来源、激活和追踪的上下文语义。send/read/wait 沿已有会话变更统一目标解析，不在此重建通信协议。

## Capabilities

### New Capabilities

- `resource-file-operations`：资源 URI 文件工具的统一目标、导航和搜索输出、文本读写与冲突、目标端执行、失败结果及会话产物接入合同。

### Modified Capabilities

- `structured-user-message-attachments`：附件模型可见定位从相对路径改为原owner提供的统一资源 URI；保留身份、预览/协议投影和失败语义。

寻址和 Skill 生命周期的公共要求分别在其现有 change 修订，本 change 只引用；不重复定义 URI grammar、kind 或来源激活状态机。

## Impact

- 依赖：`add-unified-virtual-resource-addressing` 的可导航根/路径编码与联邦能力；`add-context-injection-lifecycle` 的读取与激活分离；`migrate-session-context-resource-uri` 和 itemized 的既有会话目标 owner。
- 实施影响面：工作区文件工具/backend 与路径解析、资源平台及文件 API、Gateway 联邦透明路由、附件和工具输出 store、Provider/历史投影、协议与客户端引用，以及对应测试。
- 已修复的本地切片：`workspace_tool_paths.py::_absolute_to_workspace_relative()` 曾将工作区外绝对路径去除前导斜杠后拼入工作区，可能误读/误写同名文件；该函数与歧义回退现已删除。六个普通文件工具及源码调试统一解析工作区相对路径、工作区内原生绝对路径和 Workspace files URI；`ls/read_file` 复用目标 owner 已装配的原生目录能力处理外部目标，其余模型文件工具目前仍明确拒绝工作区外目标。Web 未注册目录沿既有文件 owner 保留浏览、编辑能力，DeepAgents 虚拟路径只在内部 backend 边界解释。完整搜索/写入资源输出、远端执行及产物迁移仍待实施，验收范围见 tasks。
- 首批文本写入覆盖既有 write/edit 与 apply_patch 的新增、修改、删除和同根文本文件移动，保留原补丁能力；通用目录管理的 `mkdir/move/copy/remove` 仍留后续，不能用其延期删除已有文本补丁能力。普通文本读取有界且严格 UTF-8，既有 read_file 媒体读取与附件二进制传输经统一 URI 交原 owner，保留原工具结果和 Provider 合同。终端/Python/浏览器/调试的进程执行、新增通用二进制读写、`watch/tail`、跨 Gateway 复制与通用 `operation_id` 不纳入。
- 规划完成不代表生产实现完成；先固定公共合同，再本地技能包导航、目标端执行、工作区写入和旧链路删除，不打断当前 R 实现。
- 终端/shell 继续使用目标操作系统原生路径语义；Gateway 原生目录选择和工作区注册保留，其管理入口边界不取消普通文件绝对路径输入。原生路径仅是具名请求输入，内部持久引用、普通响应及跨 Gateway 资源投影继续遵循 URI 合同。

未注册目录引用及控制面根持久化例外只消费统一寻址 owner「原生目录根绑定必须由目标 Gateway 唯一持有」；目标端复用既有文件 owner。本 change 已开始本地原生路径解析切片，其余任务仍按各自完整验收闭包推进，不因局部修复标为完成。
