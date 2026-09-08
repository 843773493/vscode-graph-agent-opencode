# 目录用途

协调 Saver 的显式 draft 注册、seal 重试校验、detail 分配和失败清理。

# 可修改内容

- 可调用既有 domain composer、plan registry 和 storage owner 端口。
- 可实现不分配新 identity 的已提交请求重试比较。

# 不可修改内容

- 不直接打开 SQLite、写 canonical JSONL、补造 draft 或实现 Provider 投影。
- 不为缺失的 registry、请求幂等键或 detail 返回兼容默认值。

# 规范

- 先校验 owner、注册状态与请求 identity，再分配随机 assembly/detail。
- 失败保留独立 control outcome；已提交 assembly 的 detail 不得删除。
- 同一次 seal 的 registry/snapshot/selection 提交只由 storage owner 完成。
