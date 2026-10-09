# 目录用途

承载当前工作区文件树的目录观察、持久 operation 存储及执行 worker，由现有 WorkspaceService 唯一装配。真实文件系统保存文件事实，目录版本、操作及恢复状态由当前工作区 `.boxteam/navigation/workspace-files.sqlite` 持有；普通导航不登记逐文件长期身份。

本目录的 operation、worker、上传 staging 和 202 结构仅适用于 `add-itemized-rollout-context` tasks 10.4b 分支①成立后的实施；本轮 navigation011 的固定量化及独立重算已满足该条件，证据归 tasks 10.7。目录说明不能代替其它范围的量化门控。精确子树失效、单目录替换和权威增量收敛持续遵守原文件树合同。

# 可修改内容

- 目录观察 revision、操作冻结、依赖解析、领取执行及按原 operation 恢复。
- 既有 create 文件/目录、paste 文本、copy、multipart upload 四入口所需的存储与文件执行闭包。
- 同一 owner 的上传 durable staging、路径/长度/hash 清单与清理。

# 不可修改内容

- 不另建文件树 writer、前端版本权威、Gateway 业务存储或 Session catalog。
- 不新增 delete/rename/move 端点、通用文件工具、资源 URI 文件根及兼容旧同步集合 writer。
- 不改变正文 PUT 的同步 expected_revision 合同，不把全部工作区锁定为一个 CAS。
- 不扫描磁盘静默重建操作状态，不伪称文件系统与 SQLite 多文件原子提交。

# 规范

- operation 冻结明确的 workspace、scope、root、规范路径及认证 actor；不因工作区切换改投，也不把代理选项当成免认证。
- 同目标及祖先链依赖沿既有 operation 语义；前序创建者提供 accepted 身份和 revision，禁止从执行时当前版本补缺。
- upload 必须先完整持久保存 payload 与校验清单再返回 202，后台不持有请求 UploadFile 句柄。
- 部分或未知外部效果透明返回；恢复只使用原 operation 的可核验证据，不盲重放或伪报回滚。响应丢失沿原 ID 查询或重试。
- 目录查询、操作结果及 SSE 消费同一目录观察版本；成功 terminal 与事件发布受影响父目录的配对分页快照，前端直接替换，不因成功再重拉目录。文件内容 hash 按需计算，不为搜索逐文件注册身份。
- 迁齐四入口调用方、DTO、协议、Web 与必要测试后下线旧链路；批次内快速目标静态，核心后集中边界，批末一次 Web build。测试始终使用进程外保护。
