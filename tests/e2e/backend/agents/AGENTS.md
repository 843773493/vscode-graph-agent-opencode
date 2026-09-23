# 目录用途

存放 Agent 调试工具组的后端 E2E，覆盖 `/api/v1/tools` 暴露的 debugging 工具与真实 Node.js Inspector 进程之间的端到端链路，包括 Session 的 main/child owner 隔离与 owner 停启隔离。

## 可修改内容

- 基于真实 Node Inspector 的调试工具 E2E 用例，以及工具 schema 与 Session owner 解析断言。
- 本目录专用的调试 fixture 源码、调试服务组装 helper 和证据收集代码。

## 不可修改内容

- 生产调试服务、工具 factory 与 Session/catalog 解析实现。
- 使用进程内 stub 或假 Node 进程冒充 Inspector；不通过真实后端 HTTP 端口验证的测试应下沉到 `tests/integration/backend/agents/`。

## 规范

- 测试通过 `tests/e2e/conftest.py` 的 `client` fixture 走真实 HTTP 端口，并用 `e2e_workspace_root_path` / `e2e_workspace_config_path` 注入隔离工作区与配置。
- 依赖注入统一用 pytest fixture；应用代码统一用 FastAPI Depends。
- 依赖真实 `node` 可执行文件；`shutil.which("node")` 为 `None` 时以明确 reason `skip`，不得改用替身，也不得放宽断言掩盖缺陷。
- 调试断点、变量与进程存活断言必须读取真实 Inspector 结果；owner 隔离测试必须验证一个 owner 的重启不改变其它 owner 的状态、方案、动作与 durable launch claim。
- 正式产物写入 `out/tests/e2e/backend/agents/<测试文件名>/workspace/` 与 `artifacts/`；不得写入 `src/`、`app/`、`asset/` 或项目根，也不得在项目根产生 `.boxteam/`。
- `asset/` 是只读模板目录，不得作为输出目录。
