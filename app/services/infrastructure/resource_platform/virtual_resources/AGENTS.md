# 目录用途

`resource_platform/virtual_resources/` 提供统一资源寻址的 catalog 绑定、资源值对象与 typed resolver。`boxteam://` 的纯 grammar、解析值与构造器唯一归 `app/core/resource_uri.py`；本目录消费该内核，不再拥有 grammar 模块。URI 只是安全展示/引用层，不是 identity、capability、dedupe 或幂等 key，也不携带物理路径、endpoint 或 credential。

# 可修改内容

- 可以维护已登记资源 kind、operation、catalog 绑定和 resolve 闭合错误码；authority/path 的纯语法改动在 `app/core/resource_uri.py` 及其 owner 规范统一登记。
- 可以维护 `SemanticResourceDescriptor`、`ResolvedResourceHandle`、`ResourceProvenance`、`ResolutionContext` 和只读导航 shape/operation 的一致性校验与公共出口；`ObservedSourceDescriptor` 的唯一实现仍归 `sources/` 所有，这里只复用。

# 不可修改内容

- 不得在 URI 中编码 revision/hash/snapshot ref、物理 locator 或 credential。
- 不得从 URI、资源名或模型输入推导 provider locator；不得建立通用 plugin/provider 装配、动态 import 或可安装资源 API。
- 不得在此读取文件、网络或内存资源正文；resolver 只消费注入的 catalog 快照。
- 不得让历史 context、sealed assembly 或 tracked 绑定按当前 catalog 或 display URI 重解。

# 规范

- 解析只消费内核的已登记 grammar 与错误码，不复制正则或构造分支。身份/领域段仍严格校验；skills 包根与 workspace files/root 公布 file_segments child，文件节点不接受 child，领域资源不转成私有文件树。实际文件 shape/containment 由 sources/resource_navigation.py 核实。
- resolver 必须在任何 owner/provider 访问前依次完成 grammar、scope、catalog 绑定、operation 与 capability 校验。
- tracked 绑定在绑定时冻结 resource/revision/hash/snapshot_ref；同名覆盖只影响未来名称解析，不改绑既有 registration。
