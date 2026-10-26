# 目录用途

`app/gateway/runtime/` 存放 Gateway 所拥有的本地服务进程、工作区运行时和生命周期控制。

## 职责树

```text
runtime/
├── __init__.py           运行时包说明
├── controller.py          启动恢复与运行时编排
├── group_actions.py       完整 Backend 成员组的变更
├── local_workspace.py     本地工作区目标生命周期
├── managed_backend.py     Backend 组合同与唯一进程 owner
├── workspace.py           单工作区运行时句柄和服务地址
├── workspace_process.py   Workspace/辅助服务启动及身份 CLI 边界
├── process_lifecycle.py   POSIX/Windows 进程树关闭与句柄回收
├── process_groups.py      POSIX session/process group 身份核验
├── process_ports.py       端口占用检查与预留
├── process_logs.py        子进程日志句柄、写入归属与接管核验
├── ssh_process.py         SSH 隧道进程
├── port_forwarding.py     本地与 SSH 端口转发
├── consumer_protocol.py   runtime consumer 健康证明协议
├── logging_control.py     真实进程的受认证日志状态与应用确认
├── logging_consumer.py    冻结进程集合、逐进程应用与补偿结算
├── development_restart.py 开发运行时重启协调
└── skill_sources.py       Gateway 到 Workspace 的 Skill 快照来源
```

按资源 owner 选择模块：完整 Backend 的进程合同放 `managed_backend.py`，启动命令与本地身份 CLI 调用放
`workspace_process.py`，Windows/POSIX 清理分支放 `process_lifecycle.py`，POSIX session/process group
身份核验放 `process_groups.py`；该模块的 `_ProcessIdentity` 按字段命名 PGID、OS session ID 和
进程起始标记。Linux 的启动滴答数与其他 POSIX 的创建时间都只作为不透明身份比较。不要在
`controller.py` 或 Gateway 路由中复制这些底层规则。工作区 `.boxteam/` 身份仍由 Workspace CLI
读取，Gateway 不得直接访问该目录。

# 可修改内容

- 可以维护进程组、命名服务 handle、本地工作区启动、健康检查和运行时控制器。

# 不可修改内容

- 不实现 Job、Agent、会话或工具执行业务规则。
- 不终止 Gateway 无法证明所有权的外部进程。

# 规范

- Workspace API、Terminal、Browser 和 SSH tunnel 必须使用明确名称管理。
- 关闭过程先优雅终止，超时后终止完整进程组，并暴露清理错误。
- 后端重启不得无必要地重启 Terminal 或 Browser。
- 日志消费者从 prepare 到最终配置结算持有原 registry 的拓扑锁；共享 Backend 按实际实例确认，不按挂载工作区重复应用。逐进程发送前先登记副作用，超时后查询实际状态再补偿，不能把结果未知当作未生效。
- 新受管进程通过 `BOXTEAM_RUNTIME_LOGGING` 接收已验证的 revision、level 与 pretty；该值在生命周期锁内从当前 Gateway 配置 owner 读取，不能缓存等待启动前的旧候选。它只交付启动设置，不构成第二个运行期配置来源。
- 子进程日志在打开／轮转前核对同路径的写入归属；交付中断不能当作空闲。detach 只关闭当前 Gateway 的描述符，存活进程组继续保留归属。完整关闭后才释放；接管通过受认证的进程状态核对实际描述符和文件身份，不重开日志。
- 辅助服务入口缺失只向普通生命周期响应及持久错误发布 service、stage 和异常类型，安装目录位置完整记录在 Gateway 诊断日志，不进入普通注册表投影。
- Gateway 把 `runtime.workspace.default_skill_groups` 作为环境变量传给受管 Workspace，并仅观察自身 `${BOXTEAM_HOME}/skills`，向认证且已登记的本机 Workspace 发布无路径的原始 Skill 快照；Skill 语义、工具注册和 Agent 业务逻辑由 Workspace 运行时负责。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
