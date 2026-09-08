# 目录用途

itemized 的 JCS、item/ref/selection/plan 领域合同证据。

# 可修改内容

纯领域测试和显式注入的共享 fixture。

# 不可修改内容

不得复制 storage、migration 或 projector 实现，不得把这些集成断言搬入本目录。

# 规范

golden 来自 tests/fixtures/itemized 的独立文本合同；Bun 验证使用 ESM 与 ECMAScript 数字编码。禁止从生产枚举生成预期矩阵或自动更新 golden。
