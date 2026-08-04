# tests/unit/tool_testing/cases

## 目录用途

存放 `app/tool_testing/cases/` 工具测试用例定义的单元测试。

## 可修改内容

- 用例准备、真实工具调用结果和评估规则测试。
- 用例种子和临时工作区 fixture。

## 不可修改内容

- 不调用真实模型 Provider。
- 不把工具测试框架服务测试放入本目录。

## 规范

- 用例执行必须使用 pytest 临时工作区。
- 每个场景需断言准备状态与最终文件结果。
