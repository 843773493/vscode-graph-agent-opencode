# 目录用途

`adapters/` 提供 resource platform 的内置资源适配器：共享文件监视、Gateway 受认证快照与权威内存状态。适配器只做固定装配与类型化端口转发，不解释业务语义。

# 可修改内容

- 可以调整内置适配器的类型化端口、纯值对象与固定装配参数。
- 可以为新的内置资源种类新增同构适配器，并同步更新 bootstrap 与直接测试。

# 不可修改内容

- 不得引入动态 provider 注册、plugin manifest、热替换 loader 或可安装 reaction API。
- 不得让来源适配器取得 ContextStore writer 或决定 Skill 激活、wire role、epoch。
- 不得把物理 locator、正文或凭据写入模型可见协议。

# 规范

- 共享文件监视必须按完整 watch key（monitor instance + locator + recursive/filter/exclude/correlation/options）共享并引用计数，返回可释放 consumer handle；SessionThread 只持有 handle，不独占 watcher。
- 快照与内存状态必须携带可验证 version token；读取失败显式报错，不得伪造默认值。
- 长任务与订阅释放统一交给 LifetimeScope 或显式 owner，禁止第二套 dispose 抽象。
