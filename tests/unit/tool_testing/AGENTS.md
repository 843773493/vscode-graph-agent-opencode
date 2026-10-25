# 目录用途

`tests/unit/tool_testing/` 存放模型工具测试框架及各用例的单元测试。按生产职责拆分测试，便于定位协议、模型调用、运行编排、持久化与文件沙箱的行为。

## 测试树

```text
tests/unit/tool_testing/
├── test_tool_testing_definitions.py       用例协议和公开工具参数
├── test_tool_testing_model_invocation.py  模型探测、重试与调用记录
├── test_tool_testing_service.py           provider 调度、执行和并发
├── test_tool_testing_store.py             run 结果读写与覆盖规则
├── test_sandbox.py                        attempt 文件访问边界
└── cases/
    ├── test_apply_patch_case.py           apply_patch 场景准备和结果评估
    └── test_edit_file_case.py              edit_file 场景准备和结果评估
```

# 可修改内容

- 可以新增用例准备、真实工具执行、结果评估和注册行为测试。
- 可以按 `app/tool_testing/` 的职责树拆分和归档对应单元测试。

# 不可修改内容

- 不调用真实 LLM 或外部 provider。
- 不写入仓库内的模型测试运行目录。

# 规范

- 测试依赖通过 pytest fixture 注入。
- 工具执行必须使用临时工作区，并断言真实文件结果。
