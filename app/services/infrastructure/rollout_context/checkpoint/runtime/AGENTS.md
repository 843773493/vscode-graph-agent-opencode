# 目录用途

`runtime/` 是 LangGraph CheckpointSaver 的同步/异步公开适配层：把 v2 Saver port
暴露为 LangGraph 的 CheckpointSaver 接口。

# 可修改内容

- 可以维护 LangGraph 公开接口到 v2 Saver port 的同步与异步适配。

# 不可修改内容

- 不得在此实现 canonical item/storage 事实或 Provider 映射。
- 不得复制 Saver 的 owner 逻辑，只能委托 port。
- 不得为旧 import 路径提供 re-export 垫片或兼容别名。

# 规范

- 公开适配层只做协议转换，不改变业务语义，不缓存第二份状态。
- 失败必须显式抛出，禁止返回伪造的成功结果。

