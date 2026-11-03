# 目录用途

工作区搜索的具名 Provider 分派与生命周期；领域查询仍归原 owner。

# 可修改内容

能力注册、请求准入与取消结算，以及调用既有领域查询的 Provider。`llm_requests.py` 在原 catalog／Session 读准入内限定日志来源，正文读取、版本和位置仍归原 LLMRequestLogService。

`files.py` 将文件名称／正文请求交原 ResourceNavigation；每根保留局部选项及原 cursor，默认根只从目标工作区身份生成。文件名额在原 owner 分页前排除目录，不在本层重新扫描、匹配或存储文件。

# 不可修改内容

不实现第二套匹配器、持久索引、跨工作区调度或 HTTP／SSE 编码。

# 规范

按类别与操作唯一分派，冲突和缺失明确报错。注销先关闭准入，再取消并等待在途安全结算，最后释放资源；不发布迟到结果。旧领域入口与统一入口调用同一 Provider。

已分派 Provider 的原生 I/O／SQLite 读取失败由本服务带具名 Provider 归属发布，保留原 cause 和诊断；TimeoutError、取消、领域拒绝和内部编程错误沿各自原出口，不在 API 笼统标成文件错误。明确的文件系统错误可展示目标机器原生位置，不从任意异常正文推断资源目标。
