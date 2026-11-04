# 目录用途

完整工作台的业务搜索贡献，连接原版 SearchService 与原文件查询 owner，并按完整资源身份限制查询和本地模型。

# 可修改内容

原版搜索服务的业务分支、file/text Provider、查询条件及后续 Quick Access 范围接线。

# 不可修改内容

不复制上游搜索 UI、不维护文件索引或业务状态，不解析公开 URI grammar，不改写固定 upstream。

# 规范

使用唯一文件投影及现有 Gateway 传输；查询发起前限制根、extraFileResources 和模型。取消和 dispose 释放实际操作；不支持的选项明确拒绝。原生虚拟容器沿原服务，业务资源不进入浏览器文件 handle worker。

Quick Access 的 Session／Thread／文件名称及消息、工具结果、LLM 请求记录和 Trace 正文只通过共享 Gateway 联邦客户端查询；本层保留候选投影、原 Quick Input 展示及接受动作，续页／重试传回冻结范围和单个 owner 分支。Trace 沿精确 Thread 打开原事件详情，不拉全量日志、不查询未选运行日志来源。文件根元数据由原有界加载器读取，原 QueryBuilder 保留各根配置；领域查询不再接回前端跨目标队列。普通文件搜索自动继续各根原 cursor，Quick Access 明确单分支继续；两者复用同一后端文件 Provider，原 fuzzy scorer 只负责名称展示评分和高亮。
