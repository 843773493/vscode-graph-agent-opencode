## Why

用户上传的附件需要可由文件工具和 Shell 继续处理的独立工作目录，而现有按 digest 共享的单文件 blob、Session/Thread 私有引用及即时 GC 无法表达这个生命周期。现有资源回收也存在漏 detach 后无法成为孤儿、重启遗留占用阻断释放，以及单个 Thread 清理失败或一直不返回而阻断整个闲置扫描的问题。

## What Changes

- 以工作区附件目录为资源单位，每次独立上传创建独立资源；目录包含原件、预览及处理结果，关联和回收只计算整个目录，不逐文件维护 Thread 引用。
- 复用现有领域 owner、Thread attach/detach、operation lease 和恢复对账机制。上传成功自动关联发起 Thread，Thread 释放由后端解除关联；最后一次 detach 后默认保留 30 天，以低频检查和删除前事务复核回收目录。
- **BREAKING** 附件目录替代按内容摘要合并上传资源的身份和保留机制；digest 仅校验明确的文件版本，不能作为可变目录身份。迁齐上传、媒体读取、文件工具、Shell、历史、fork 和删除消费者后移除旧附件存储链路。
- 在统一 URI owner 中登记工作区附件目录及文件 child 模板；Shell 的结构化工作目录在目标端解析 URI，命令正文继续使用原生 Shell 语义，不做命令字符串替换。
- 保留上传原件；处理结果使用新文件/子目录。已封存的 canonical 输入和 assembly 不从当前目录重建，回收后历史展示明确不可用，不能改写历史或伪造成功。
- 补齐运行时 owner 失效、崩溃恢复和挂起操作的可核验释放规则；闲置扫描对单个 Thread 的卸载最多等待 30 秒，超时后保留唯一在途清理任务并继续扫描，原任务失败后才允许后续周期重试，不能重复发起外部释放。
- 已完成 Thread 闲置扫描失败隔离/周期重试与有界等待，以及 Shell、Browser 资源记录查询的遗留改名调用迁齐；接续实施附件目录的单链路迁移。全仓调用方与 schema 3 离线切换范围见 design，目录代码、挂起任务终止核实、共享 owner 锁隔离和关闭期限仍按未完成任务推进，不把规划或扫描等待超时当作资源已经释放。

## Capabilities

### New Capabilities

- `managed-attachment-directories`: 工作区附件目录、URI 与 Shell 访问、Thread 关联、30 天回收、历史/fork 保留及失效 owner 对账。

### Modified Capabilities

- `structured-user-message-attachments`: 上传原件和预览改由工作区目录资源保存，消息使用其文件与目录 URI。

## Impact

- 影响 attachment store/catalog、UserContentBuilder、统一 URI owner、文件操作 owner、Terminal workdir、Thread residency、Session/Thread 删除和 fork 资源处理；不新增服务、第二套文件操作链路或另一套 lease 账本。
- 与 `add-itemized-rollout-context` 的附件 blob/owner refs、`add-unified-virtual-resource-addressing` 的附件单文件模板、`add-unified-resource-file-tools` 的只读附件产物边界及 `add-workspace-persistent-resource-management` 的即时释放规则做显式衔接，冲突与替代顺序记录在 design。
- 保持 SQLite、canonical item、冻结请求、现有媒体预览和权限边界。只有实际调整数据库结构才提升对应 schema；支持的旧数据范围沿用项目政策，迁移不得静默丢弃原件或双读旧实现。
