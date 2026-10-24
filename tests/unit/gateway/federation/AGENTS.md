# 目录用途

存放 Gateway 联邦通道生命周期、发现与管理协议的单元测试，与 federation 生产 owner 对应。

# 可修改内容

精确 channel owner 的登记、取消和资源释放测试，以及联邦发现、管理和 Workspace HTTP port 的请求、响应与诊断边界和测试专用 fixture。

# 不可修改内容

不在此启动真实 Gateway/工作区后端，不写用户控制面，不改认证、联邦协议或 Agent 业务。

# 规范

使用 pytest fixture 注入依赖，持久化错误采用真实 SQLite 边界，网络替身明确标注；正式输出位于同名 out/tests 路径，所有测试有进程外超时和内存限制。
