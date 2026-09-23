# 目录用途

存放 `app/services/infrastructure/events/` 的单元测试，覆盖通用 `EventChannelService` 的 channel 隔离、溢出策略与 cursor 重放合同，以及 `resource.state/*`、`config.lifecycle/*`、`context.source/*` typed 轻量事件的字段与值校验。

## 可修改内容

- channel 命名/解析、六类 channel kind、history 关闭与溢出错误、cursor 重放等合同测试。
- typed 轻量事件的类级字段白名单、值级校验、channel 名构造，以及「携带正文或宿主机路径必须显式报错」红线断言。

## 不可修改内容

- 生产事件服务与事件模型实现。
- 不连接真实外部服务或真实 LLM，不依赖跨模块集成的完整链路；这类测试放在 `tests/integration/`。
- `asset/` 只读模板不得作为输出目录。

## 规范

- 依赖注入统一用 pytest fixture；应用代码统一用 FastAPI Depends。
- 测试应快速、独立、可重复；异步用例用 `asyncio` 事件循环构造，避免真实等待与时间竞争。
- 溢出、history 关闭、校验失败等错误路径必须显式断言具体错误类型，不得通过放宽断言或 `skip` 掩盖真实缺陷（程序绝不能默默失败）。
- 需要文件系统副作用时使用 `tmp_path` 隔离，不得在项目根产生 `.boxteam/` 或测试数据；正式产物按测试路径写入 `out/tests/unit/services/infrastructure/events/`。
