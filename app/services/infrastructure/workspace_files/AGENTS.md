# 目录用途

承载当前工作区文件树的目录观察、持久 operation 存储及执行 worker，由现有 WorkspaceService 唯一装配。真实文件系统保存文件事实，目录版本、操作及恢复状态由当前工作区 `.boxteam/navigation/workspace-files.sqlite` 持有；普通导航不登记逐文件长期身份。

本目录的 operation、worker、上传 staging 和 202 结构仅适用于 `add-itemized-rollout-context` tasks 10.4b 分支①成立后的实施；本轮 navigation011 的固定量化及独立重算已满足该条件，证据归 tasks 10.7。目录说明不能代替其它范围的量化门控。精确子树失效、单目录替换和权威增量收敛持续遵守原文件树合同。

# 可修改内容

- 目录观察 revision、操作冻结、依赖解析、领取执行及按原 operation 恢复。
- 既有 create 文件/目录、paste 文本、copy、multipart upload 四入口所需的存储与文件执行闭包。
- 同一 owner 的上传 durable staging、路径/长度/hash 清单与清理。
- `mutation_lock.py` 提供正文保存与模型文本工具共用的短写入临界区；`text_publication.py` 负责有界原字节读取、发布前重核和单文件发布后核实，不自动恢复旧内容。
- `text_owner.py` 是容器装配并经 runtime 显式注入的唯一模型文本入口，接受具名 create/edit/patch 请求；创建与精确替换的内容准备不留在工具层。
- `text_mutation.py` 唯一执行模型 write/edit/patch 的整批路径、权限、原字节版本预检和逐文件发布；`text_patch/` 唯一准备 V4A 补丁内容，目标端不依赖 Agent 或复制语法。
- `text_journal.py` 保存临界区内原件、最终观察到的后件与逐路径发布结果；SessionChanges 直接消费这些 URI/内容事实，不在事件到达时重读当前文件。有会话的调用通过统一解析器聚合到所属会话 `changes/text_mutations`，无会话独立工具调用只写显式工作区缓存。

# 不可修改内容

- 不另建文件树 writer、前端版本权威、Gateway 业务存储或 Session catalog。
- 不新增 delete/rename/move 端点、通用文件工具或兼容旧同步集合 writer；资源 URI 根模板只消费统一寻址 owner 的定义。
- 不改变正文 PUT 的同步 expected_revision 合同，不把全部工作区锁定为一个 CAS。
- 不扫描磁盘静默重建操作状态，不伪称文件系统与 SQLite 多文件原子提交。

# 规范

- operation 冻结明确的 workspace、scope、root、规范路径及认证 actor；不因工作区切换改投，也不把代理选项当成免认证。
- 普通文件入口统一接受目标机器原生路径和资源 URI；业务记录、响应、游标与事件只保存或返回 URI。未注册目录根仅由原生 Gateway 绑定，后台恢复重新解析绑定，不把真实根复制进工作区存储。
- 同目标及祖先链依赖沿既有 operation 语义；前序创建者提供 accepted 身份和 revision，禁止从执行时当前版本补缺。
- upload 必须先完整持久保存 payload 与校验清单再返回 202，后台不持有请求 UploadFile 句柄。
- 部分或未知外部效果透明返回；恢复只使用原 operation 的可核验证据，不盲重放或伪报回滚。响应丢失沿原 ID 查询或重试。
- 目录查询、操作结果及 SSE 消费同一目录观察版本；成功 terminal 与事件发布受影响父目录的配对分页快照，前端直接替换，不因成功再重拉目录。文件内容 hash 按需计算，不为搜索逐文件注册身份。
- 迁齐四入口调用方、DTO、协议、Web 与必要测试后下线旧链路；批次内快速目标静态，核心后集中边界，批末一次 Web build。测试始终使用进程外保护。
