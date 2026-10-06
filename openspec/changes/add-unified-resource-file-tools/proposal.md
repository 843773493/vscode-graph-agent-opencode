## Why

已有本地虚拟文件后端和资源 URI 解析，但目录导航、普通文件访问和跨端文件执行还没有统一消费合同。模型无法稳定访问技能包的多层文件，也容易把当前激活工作区、资源生命周期与一次文件操作混为一谈。

## What Changes

- **BREAKING** 将 `ls/read_file/glob/grep/write_file/edit_file/apply_patch` 接入统一资源寻址的唯一解析器和文件 owner；搜索条件与目标根分开，结果返回可继续访问的资源 URI。
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
- 首批文本写入覆盖既有 write/edit 与 apply_patch 的新增、修改、删除和同根文本文件移动，保留原补丁能力；通用目录管理的 `mkdir/move/copy/remove` 仍留后续，不能用其延期删除已有文本补丁能力。普通文本读取有界且严格 UTF-8，既有 read_file 媒体读取与附件二进制传输经统一 URI 交原 owner，保留原工具结果和 Provider 合同。终端/Python/浏览器/调试的进程执行、新增通用二进制读写、`watch/tail`、跨 Gateway 复制与通用 `operation_id` 不纳入。
- 规划完成不代表生产实现完成；先固定公共合同，再本地技能包导航、目标端执行、工作区写入和旧链路删除，不打断当前 R 实现。
