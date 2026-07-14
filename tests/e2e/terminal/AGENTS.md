# 目录用途

`tests/e2e/terminal/` 存放持久终端管理器相关的端到端测试，以及只服务于这些测试的进程启动、端口、状态文件和历史 checkpoint 辅助函数。

## 可修改内容

- 终端管理器生命周期、持久终端后台连接面板、终端 attach 前后端联动相关 e2e 测试。
- terminal e2e 专用 helper、fixture 和测试数据构造。

## 不可修改内容

- 不放与终端无关的通用 e2e helper；通用端口和后端进程管理继续放在 `tests/e2e/` 根层。
- 不在这里修改生产终端实现或主 Web UI 实现。

## 规范

- 测试文件仍使用 `tests/e2e/conftest.py` 提供的文件级工作区、后端端口和 client fixture。
- terminal 专用端口必须从当前测试文件的 e2e 端口块中派生，避免与其它 e2e 文件并发冲突。
- 进程启动失败、HTTP 失败和状态缺字段时直接抛出明确错误，不要静默跳过。
