# 目录用途

`app/tool_testing/` 是模型工具能力测试的独立功能模块，负责测试用例协议、注册、异步运行和可读结果持久化。

## 职责树

```text
app/tool_testing/
├── definitions.py       用例协议、模型调用输入和评估结果
├── registry.py          按工具名登记和查询用例
├── sandbox.py           attempt 内文件访问边界与安全发布
├── service.py           provider/用例调度、运行状态和并发控制
├── model_invocation.py  单次工具调用探测、交换记录和短暂错误重试
├── store.py             run 与 case JSON 产物的读写
└── cases/
    ├── apply_patch_case.py  apply_patch 场景准备与结果评估
    └── edit_file_case.py    edit_file 场景准备与结果评估
```

主链路由 `service.py` 取得 `registry.py` 中的用例，调用用例准备独立 attempt，交给
`model_invocation.py` 驱动模型发起真实工具调用，再由用例评估并通过 `store.py` 保存结果。
新增工具场景放在 `cases/` 并实现 `definitions.py` 中的协议；模型调度与持久化仍由各自 owner
负责，避免每个场景复制一套 provider 或结果写入逻辑。

# 可修改内容

- 可以新增工具测试用例、注册规则、运行编排和结果存储实现。
- 可以扩展模型并发策略、指标和测试工作区准备流程。

# 不可修改内容

- 不在这里实现 FastAPI 路由或前端展示。
- 不把正常 Agent 会话执行流程复刻到测试运行器中。
- 不把 API key 或工作区绝对路径硬编码到测试用例。

# 规范

- 每个模型的测试必须串行，不同模型可以并发。
- 测试产物必须写入当前工作区 `.boxteam/tool_tests/`。
- `sandbox.py` 仅向本次 attempt 的普通文件授予能力，复用正式文件解析和文本发布；父目录、兄弟 attempt、越界符号链接和 attempt 内 `.boxteam` 不公开，普通文件入口的私有树保护不变。
- 用例失败必须记录明确原因，不得把异常转换成成功。

