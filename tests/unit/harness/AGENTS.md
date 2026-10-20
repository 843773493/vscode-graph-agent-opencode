# 目录用途

存放测试 harness 自身纯函数、命令生成和进程生命周期的单元测试；生命周期用 mock 注入启动、就绪、中断和清理结果。

```text
harness/
├── processes/                         进程启动、就绪、日志和归属的故障注入
│   ├── test_process_scope.py           登记归属与嵌套清理
│   ├── test_process_log_lifetime.py    Workspace/Gateway 日志句柄
│   ├── test_auxiliary_process_lifetime.py  Browser/Terminal 辅助服务
│   ├── test_fixture_process_lifetime.py    组合装具的启动失败清理
│   ├── test_navigation_fixture_lifetime.py 导航下游的进程与日志
│   └── test_http_readiness.py          HTTP 就绪预算、身份与退出诊断
├── test_run_context.py                测试运行上下文
├── test_catalog_fixture_lifetime.py   SQLite catalog 装具归属
├── test_ports.py                      测试文件端口分配
└── test_*.py                          配置、命令生成与 API 等待器
```

先按被管理资源分组：进程故障注入集中到 `processes/`，运行上下文、catalog、配置和业务协议等待器留在各自命名的测试文件。真实进程或网络组合进入 `tests/integration/harness/`；不要根据被导入装具原本位于 E2E 就把 mock 测试归为 E2E。

## 可修改内容

- 不启动真实产品进程的 harness 单元测试。

## 不可修改内容

- 不要放产品业务或完整链路测试。
- 真实子进程、监听端口和跨模块启动组合归 `tests/integration/harness/`。

## 规范

- 可使用 fake 描述符验证命令生成，但不得归类为 E2E。
