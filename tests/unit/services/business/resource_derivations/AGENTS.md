# 目录用途

验证business/resource_derivations中只消费冻结输入的纯业务loader。

# 可修改内容

- 业务facet选择、可见性、忽略字段与无效输入的单元测试。
- 不触发来源I/O的SemanticInput fixture。

# 不可修改内容

- 不启动fanout、工作区后端、Gateway或Provider。
- 不在测试中实现第二份生产投影或持久化事实。

# 规范

- 使用pytest fixture注入输入，以可观察业务payload核对行为。
- 真实owner接线、Registry发布与恢复验证归integration。
