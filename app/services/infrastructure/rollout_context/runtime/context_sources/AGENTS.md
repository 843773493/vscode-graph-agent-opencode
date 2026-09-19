# 目录用途

`context_sources/` 保存 ContextSourceManager 及来源生命周期的运行时协调代码，负责把已由来源 owner 解析的资源状态转换为待提交的上下文增量。

# 可修改内容

- 可以维护 source identity、revision、snapshot/tracked 状态和待注入 delta 的内存协调。
- 可以接收受信 source owner 已验证的正文，并把结果交给唯一 ContextStore owner。

# 不可修改内容

- 不得自行扫描工作区、创建 watcher、写入 JSONL/SQLite 或决定业务 channel。
- 不得向模型暴露来源的宿主机路径、credential 或内部 locator。

# 规范

- snapshot 只读取一次；tracked 的变化通过已注册观察事件或显式 observe 输入进入。
- 同一来源的未发送变化必须从已应用 revision 合并成一个 delta；untrack 不移除已提交上下文。
- 生命周期机制只返回状态和释放结果，不发布业务事件。
