# 目录用途

`tests/unit/gateway/runtime/` 存放 Gateway 工作区进程生命周期与进程日志基础设施的单元测试。

## 职责树

```text
runtime/
├── consumer / local workspace
│   ├── test_consumer_protocol.py       消费端协议边界
│   ├── test_local_workspace_access.py  本机工作区访问权限
│   ├── test_workspace_health_errors.py 健康响应与身份错误
│   └── test_workspace_process.py       工作区后端进程生命周期
├── process operations
│   ├── test_process_groups.py           进程组所有权
│   ├── test_process_lifecycle.py       启停、退出确认与回收
│   ├── test_process_logs.py             进程日志落盘与读取
│   ├── test_process_ports.py            端口分配与监听检查
│   └── test_ssh_process.py               SSH 进程生命周期
├── gateway runtime coordination
│   ├── test_development_restart.py       开发服务重启
│   ├── test_gateway_runtime_lock_table.py 运行时互斥锁
│   └── test_workspace_reconnect.py       重连、Backend owner 生命周期与接管
```

`test_workspace_reconnect.py` 中的 `WorkspaceRuntimeControllerCase` 把可变状态按职责分组：

```text
WorkspaceRuntimeControllerCase
├── registry              Gateway 注册表与路由状态
├── graph                 Backend owner、合同、runtime 和进程句柄
├── controller            被测运行时协调器
├── runtime_actions       已观察到的启停调用
├── lifecycle_states      fake 后端当前状态
└── blockers_by_workspace 按工作区组织的退出阻塞项
```

该测试文件目前承载多个 managed Backend 生命周期场景；修改前先按行为定位用例，拆分该文件时同步处理测试矩阵、镜像产物路径和引用它的 OpenSpec 审计记录。

## 可修改内容

- 进程启动、停止、健康检查和日志文件管理的单元测试。
- 相关测试辅助对象与 fixture。

## 不可修改内容

- 不启动依赖真实用户配置的长期后台进程。
- 不把测试日志写入真实 Gateway 控制面目录。

## 规范

- 文件系统测试统一使用 pytest 临时目录。
- 权限断言需要按操作系统能力显式跳过不适用平台。
