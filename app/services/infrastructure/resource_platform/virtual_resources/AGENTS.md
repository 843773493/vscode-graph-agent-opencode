# 目录用途

`resource_platform/virtual_resources/` 提供 `boxteam://` 虚拟资源命名空间（VRN）的严格 grammar、纯值对象与 typed resolver。URI 只是安全展示/引用层，不是 identity、capability、dedupe 或幂等 key，也不携带物理路径、endpoint 或 credential。

# 可修改内容

- 可以扩展已登记的 authority/path grammar、资源 kind、operation 与闭合错误码。
- 可以维护 `SemanticResourceDescriptor`、`ResolvedResourceHandle`、`ResourceProvenance`、`ResolutionContext` 的一致性校验与公共出口；`ObservedSourceDescriptor` 的唯一实现仍归 `sources/` 所有，这里只复用。

# 不可修改内容

- 不得在 URI 中编码 revision/hash/snapshot ref、物理 locator 或 credential。
- 不得从 URI、资源名或模型输入推导 provider locator；不得建立通用 plugin/provider 装配、动态 import 或可安装资源 API。
- 不得在此读取文件、网络或内存资源正文；resolver 只消费注入的 catalog 快照。
- 不得让历史 context、sealed assembly 或 tracked 绑定按当前 catalog 或 display URI 重解。

# 规范

- 解析只接受已登记 grammar：percent 编码整体拒绝（因此不存在二次解码歧义），userinfo、query/fragment、控制字符、反斜杠、空 segment、`.`/`..`、非 ASCII 与大小写变体一律显式报错，错误码闭合。
- resolver 必须在任何 owner/provider 访问前依次完成 grammar、scope、catalog 绑定、operation 与 capability 校验。
- tracked 绑定在绑定时冻结 resource/revision/hash/snapshot_ref；同名覆盖只影响未来名称解析，不改绑既有 registration。
