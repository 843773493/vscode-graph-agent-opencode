# 目录用途

完整工作台的业务搜索贡献，连接原版 SearchService 与原文件查询 owner，并按完整资源身份限制查询和本地模型。

# 可修改内容

原版搜索服务的业务分支、file/text Provider、查询条件及后续 Quick Access 范围接线。

# 不可修改内容

不复制上游搜索 UI、不维护文件索引或业务状态，不解析公开 URI grammar，不改写固定 upstream。

# 规范

使用唯一文件投影及现有 Gateway 传输；查询发起前限制根、extraFileResources 和模型。取消和 dispose 释放实际操作；不支持的选项明确拒绝。原生虚拟容器沿原服务，业务资源不进入浏览器文件 handle worker。
