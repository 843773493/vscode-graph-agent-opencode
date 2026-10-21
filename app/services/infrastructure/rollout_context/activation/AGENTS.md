# 目录用途

`activation/` 持有既有资源激活 catalog、lineage、正文/detail 适配与 assembly binding；由 rollout context 组合根显式组装。

# 可修改内容

- 可以维护资源激活记录的 schema、存储、读取、lineage 与正文适配。
- 可以调用领域值对象、统一会话路径解析器及既有持久化端口。

# 不可修改内容

- 不得新增 activation writer 或事实源，不得复制 assembly、execution 或 checkpoint 的权威状态。
- 不得在基础 storage 中反向组装业务 owner，不得保留迁出前后的双套资源激活实现。

# 规范

- 沿既有 owner 维护身份、binding、提交和恢复边界；正文/hash 规则使用领域合同。
- 显式注入依赖，读取不隐式激活资源；失败必须保留原因并明确报告。
- 静态结构与迁移SQL唯一定义在 `sql/`；Python显式加载并保留事务、条件、版本与参数协调。
