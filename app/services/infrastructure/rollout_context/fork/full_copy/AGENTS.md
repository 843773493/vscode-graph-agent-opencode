# 目录用途

完整 fork 的私有 staging、protected artifact 和 target manifest 收敛。

# 可修改内容

- 复用既有 storage writer、typed detail capability 和 identity mapping 的复制事务。

# 不可修改内容

- 不读取旧格式、不导入 migration parser、不访问 cipher 或复制 source session 密钥。
- 不在 staging 验证前发布 target，不改 source 原件。

# 规范

- 所有正文经显式 capability 验证；目标 key、detail、plan、ref 均为 target-local。
- 保留 retention/visibility，失败只留下明确错误，不泄露敏感原文。
