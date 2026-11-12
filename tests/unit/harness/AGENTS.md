# 目录用途

存放测试 harness 自身纯函数、命令生成和资源装配生命周期的单元测试；生命周期用 mock 注入启动、就绪、中断和清理结果。

```text
harness/
├── processes/                         进程启动、就绪、日志和归属的故障注入
│   ├── test_process_scope.py           登记归属与嵌套清理
│   ├── test_process_log_lifetime.py    Workspace/Gateway 日志句柄
│   ├── test_fixture_process_lifetime.py    组合装具的启动失败清理
│   ├── test_navigation_fixture_lifetime.py 导航下游的进程与日志
│   └── test_http_readiness.py          HTTP 就绪预算、身份与退出诊断
├── test_run_context.py                测试运行上下文
├── test_fixture_output_isolation.py   单测工作区、manifest、Docker证据隔离与混合收集标记
├── test_integration_gate.py           闸门根位置、原生锁失败与异常释放
├── test_output_paths.py               正式输出镜像路径与显式项目根
├── test_e2e_control_files.py           控制事件与模型回放文件的UTF-8读写
├── test_fixture_stage_observer.py      fixture计时JSONL的初始化与摘要失败释放
├── test_catalog_fixture_lifetime.py   SQLite catalog 装具归属
├── test_gateway_target.py             Gateway SSH 命令生成与平台分支
├── test_workspace_tool_fixture_lifetime.py  资源工具装具的多资源清理及错误链
├── test_shutdown_environment_fixture_lifetime.py  隔离配置恢复与Linux目录fd释放
├── test_ports.py                      测试文件端口分配
└── test_*.py                          配置、命令生成与 API 等待器
```

先按被管理资源分组：进程故障注入集中到 `processes/`，运行上下文、catalog、配置和业务协议等待器留在各自命名的测试文件。真实进程或网络组合进入 `tests/integration/harness/`；不要根据被导入装具原本位于 E2E 就把 mock 测试归为 E2E。

控制文件测试在真实隔离文件上显式模拟 CP1252/GBK 默认文本编码，分别检查事件保存、已有回放读取、新回复写入和损坏 UTF-8 拒绝。server/thread 是生命周期替身，不监听端口、不启动产品；它证明文件合同，不等于 Windows 原生或完整 E2E 验收。

统一性能记录器使用真实隔离文件核验write/flush失败后的句柄释放、并发阶段归属、取消/真实setup失败与显式报告比较；不运行427轮业务fixture，也不把记录器验证当成生成器验收。业务专属计数留在业务装具。

关闭环境装具的单测只使用正式输出目录内的文件和当前进程创建的目录fd，配置解析/schema校验是显式替身。验证准备中断、部分写入和关闭异常后的原字节恢复，不启动Gateway或验证业务关闭；故障注入遗留的本用例fd由单测兜底回收。

## 可修改内容

- 不启动真实产品进程的 harness 单元测试。

## 不可修改内容

- 不要放产品业务或完整链路测试。
- 真实子进程、监听端口和跨模块启动组合归 `tests/integration/harness/`。

## 规范

- 可使用 fake 描述符验证命令生成，但不得归类为 E2E。
- 端口分配测试同时核仓库全部integration/e2e文件的块不重叠、跨层共享剩余额度、完整池边界、整体溢出提前拒绝与缓存快照；不监听端口，不把文件索引测试当成真实进程互斥证明。

- Gateway目标启动合同断言唯一runner模块名；路径转义及环境装配仍核原完整命令，不放宽为只匹配后缀参数。
