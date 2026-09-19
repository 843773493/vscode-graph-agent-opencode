# 目录用途

`resource_platform/derivation/` 存放无环 `ResourceDerivationGraph` 与语义派生值对象：
把已接受的来源 revision 按代码内固定注册的 derivation 规范，经可注入 loader port
解析为语义 facet payload，并向语义 `ResourceRegistry` CAS 发布不可变 `ResourceSnapshot`。

# 可修改内容

- 可以维护 SemanticResourceDescriptor/SemanticInput/SemanticPayload/ResourceSnapshot 等纯值对象。
- 可以维护 graph 的注册、无环校验、代际一致性检查、语义 diff/CAS 发布与 unavailable 保留语义。
- 可以维护 loader port（Protocol）与针对本目录的单元测试。

# 不可修改内容

- 不得引入 plugin manifest、动态 import、可安装 provider/loader/reaction API 或运行期注册业务 loader。
- 不得读取文件、网络或直接调用 StableSourceReader；来源事实只经 SourceReconciler 已发布的 revision。
- 不得写 ContextStore/checkpoint/会话状态，不得决定 wire role、epoch 或 Skill 激活策略。

# 规范

- 来源 revision 与语义 revision 分离：语义 revision 只由 facet payload 的 JCS hash 决定，payload 不变不推进、不重发布。
- 依赖缺失、依赖成环、混合 generation、来源 unavailable 都必须显式 unavailable 并保留旧 valid 快照可审计。
- loader 失败/无效输入由 loader 以 SemanticPayload(available=False) 显式返回；loader 自身缺陷直接抛出。
