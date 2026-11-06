# 目录用途

完整工作台的来源资源快速访问，使用原领域 owner 的真实发现与打开能力。

# 可修改内容

来源候选的 Quick Input 展示、具名查找命令及原 Opener 接线。

# 不可修改内容

不拼装公开 URI 模板、不维护资源索引或上下文激活状态，不以普通文件或文本冒充其它资源能力。

# 规范

发现工作区根 AGENTS 和工作区已配置 HTTP Skill；Skill 只消费原 owner 的 accepted metadata，不抓取上游、扫描本地 Skill 或编译 runtime。同次 provide 只发现一次、逐键本地过滤，不保留跨打开缓存。查询固定发起 owner，结束选择器或身份失效时取消实际读取，全部 metadata 请求共用三并发限制且各分支独立报告失败。打开复用原 Opener、来源只读 Provider 和编辑器组选项，不额外 describe/read；其它类别按各自原合同继续接入。
