# 目录用途

本目录按具体领域存放 Gateway 请求、响应与控制面 DTO；配置、工作区、生命周期、联邦、用户、诊断、界面设置、端口转发、目录和 SSH 类型分别由对应模块维护。

```text
gateway/
├── config.py             配置重载、待确认变更与配置来源
├── diagnostics.py        健康状态、诊断日志和开发运行时重启结果
├── directory.py          Gateway 目录 URI 与目录列表
├── federation.py         联邦协议、对端和入站工作区
├── lifecycle.py          运行组重启、状态、阻塞项和影响范围
├── native_directories.py 原生目录绑定与解析
├── port_forwarding.py    端口转发创建、修改和列表
├── skill_sources.py      Gateway Skill 来源目录
├── ssh.py                远端 Gateway 添加与 SSH 连接选项
├── ui_settings.py        Web 布局、主题与 Gateway 界面设置
├── users.py              Gateway 用户、租约和视图状态
├── workspaces.py         Gateway 工作区、激活、排序和管理状态
└── __init__.py           保持轻量，不聚合转出 DTO
```

按 DTO 所属领域直接导入对应文件；新增类型先确认它的权威 owner，再扩展该文件或拆出新的领域模块，不通过包级转发建立第二入口。

# 可修改内容

- 可以维护具体领域的 Pydantic 类型、字段校验与序列化合同。
- 可以同步迁移 API、服务、协议与测试的直接和间接消费者。

# 不可修改内容

- 不得实现业务流程、进程管理、文件、数据库或网络访问。
- 不得保留旧 `gateway.py`，也不得在 `__init__.py` 聚合再导出类型或添加兼容别名。

# 规范

- 消费者直接导入拥有该 DTO 的具体模块；同一类型只定义一处。
- 普通结构搬迁保持公开字段、验证和序列化行为；已授权的共享 Backend 合同字段与全部消费者同批迁齐。
- Backend UUID 与 Gateway target ID 保持各自已定命名空间；generation、完整影响集合和 blocker 归属使用生命周期 owner 的事实。
