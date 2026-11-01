# 目录用途

集中管理测试创建的子进程及其生命周期，区分通用进程 scope、Workspace Backend 和 Gateway 启动职责。

```text
processes/
├── scope.py        当前测试拥有的进程句柄登记、嵌套归属与统一清理
├── workspace.py    Workspace Backend 启动/关闭、端口检查、Python 路径与 HTTP 就绪
└── gateway.py      Gateway 配置准备、启动/关闭、就绪与持久状态重置
```

## 可修改内容

- 测试拥有的 Workspace、Gateway 和辅助子进程生命周期。
- 端口占用检查、健康探针以及进程输出句柄归属。
- Windows Job Object 保护统一消费 `app/runtime/windows_job.py`，不在测试目录保留另一实现。

## 不可修改内容

- 不管理非测试创建的进程，不按端口杀死未知监听者。
- 不把产品服务逻辑或真实 E2E 场景编排放入测试进程 helper。
- 不为旧平铺模块路径增加重导出或兼容 shim。

## 规范

- `scope.py` 只登记和回收进程句柄；进程创建者显式移交句柄与清理动作。
- `app/runtime/windows_job.py` 在挂起根进程时先绑定 Job Object；矩阵 runner 和 Windows 目标套件共用同一套进程树资源限制与退出回收。
- `workspace.py` 与 `gateway.py` 分别拥有对应运行面的启动参数和就绪合同。
- 创建进程前检查端口并准备日志；创建失败关闭日志，成功后由句柄接管。
- Backend 的 HTTP 就绪探针先确认持有的 PID 已在目标回环地址监听，再请求健康接口并核对工作区；不在 uvicorn lifespan 阶段连接未监听端口，避免 TCP 自连接阻止后端绑定。等待仍共用原总期限。
- 更新本目录文件时同步维护 `tests/support/AGENTS.md` 的树形索引与调用方导入路径。

- Backend 显式传入 `boxteam_home` 时，配置安装与子进程环境使用同一绝对目录；联邦装具与 Gateway 共用该 owner 的 HOME，不能只改环境或只迁移 Gateway 控制面。
