# 目录用途

纯领域类型与确定性规则的单元测试。

# 可修改内容

domain fixture、schema 和 hash contract 测试。

# 不可修改内容

不得加入 Saver、存储、Provider 或网络集成测试。

# 规范

依赖使用 pytest fixture 显式注入；产物遵守 tests/ 根规则。
