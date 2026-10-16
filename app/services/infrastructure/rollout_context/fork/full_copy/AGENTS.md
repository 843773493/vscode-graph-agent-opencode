# 目录用途

冻结 Thread 快照所使用的 SQLite 一致捕获叶模块。

# 可修改内容

- 可以维护有界 SQLite backup 与只读源连接；目标安装由 fork/snapshot_copy.py 负责。

# 不可修改内容

- 不读取旧格式、不导入 migration parser、不访问 cipher 或复制 source session 密钥。
- 不在 staging 验证前发布 target，不改 source 原件。

# 规范

- 历史 local ID、正文、时间、hash 与 pending 事实保持原值；只建立目标自己的运行状态。
- 历史 detail/ref/plan 必须在目标物理节点冷读，不改写为目标身份、不回读 source locator。
- 保留 retention/visibility，失败只留下明确错误，不泄露敏感原文。
