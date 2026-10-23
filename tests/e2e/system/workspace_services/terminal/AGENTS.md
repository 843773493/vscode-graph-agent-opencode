# 目录用途

`tests/e2e/system/workspace_services/terminal/` 存放持久终端管理器相关的端到端测试，以及这些场景专用的端口、状态文件断言和历史 checkpoint 输入。

```text
terminal/
├── test_*.py                  生命周期、持久资源与协议场景
├── terminal_process_helpers.py 端口、HTTP 请求及进程/状态断言
└── terminal_history_fixture.py 历史回显场景专用的 checkpoint 构造
```

通用 Terminal 后端启动归 `tests/harness/python/terminal_backend.py`，不由 E2E helper 向其他测试层导出。历史输入构造单独依赖 checkpoint owner，普通进程装具与 HTTP 断言不应为此导入整条历史存储链。

## 可修改内容

- 终端管理器生命周期、持久终端后台连接面板、终端 attach 前后端联动相关 e2e 测试。
- terminal e2e 专用 helper、fixture 和测试数据构造。

## 不可修改内容

- 不放与终端无关的通用 helper；跨测试层的端口和进程管理放在 `tests/support/`，客户端辅助服务的进程装具放在 `tests/harness/python/`。
- 不在这里修改生产终端实现或主 Web UI 实现。

## 规范

- 测试文件仍使用 `tests/e2e/conftest.py` 提供的文件级工作区、后端端口和 client fixture。
- terminal 专用端口必须从当前测试文件的 e2e 端口块中派生，避免与其它 e2e 文件并发冲突。
- 进程启动失败、HTTP 失败和状态缺字段时直接抛出明确错误，不要静默跳过。
- 启动前只检查端口占用；退出时只清理本用例创建的进程句柄，不按端口杀死未知所有者。
- 多进程 fixture 在创建后立即向 `ExitStack` 登记释放；部分启动失败或某个清理动作抛出普通异常/中断时，仍尝试释放其余已持有进程。原始错误沿异常链保留，不以首个清理失败为由跳过剩余资源。
- Terminal 后端与本目录直接创建的 attach 前端把输出直接写入当前测试的 `TestRunContext.artifacts_dir`，使用独立不可覆盖日志；嵌套工作区不改变这个归属。清理只终止所持进程，不再通过 `communicate()` 收集或重写整份输出。
- 只核验 mount 身份、空列表和传输隔离的场景使用 `prepare_test_workspace(..., template_items=())`；只有断言依赖工作区文件时才复制对应 fixture 项。
