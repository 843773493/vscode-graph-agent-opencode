# 目录用途

为一次性 SQLite schema2→3 artifact 升级提供受保护 detail 的显式重绑定能力。
旧 detail blob 的 v1 编码不是 rollout message-line v1 reader。

# 可修改内容

- 按可信历史定义验证旧 AAD、认证正文、校验 session digest，准备新 typed detail。
- 返回仅含新 record、普通 redaction manifest 与密文的内存结果。
- 只读认证已有 typed staged record/manifest/密文与 session digest，供迁移重试原样复用。

# 不可修改内容

- 不读取旧路径、不扫描或安装 artifact、不提交 SQLite、不访问 backend 私有 cipher。
- 不给普通 detail read 添加旧字符串、旧 record 或版本 fallback。
- 不复制加密、JCS 或 digest 算法，不输出明文、密钥或原始异常链。
- 不为重试固定 nonce，不在认证已有 staged artifact 时重新加密或生成密钥。

# 规范

- 只能由显式 migration 入口调用；普通 runtime 不调用本模块。
- 缺少 protected key 或既有 session key 必须拒绝；升级不能创建新 digest key。
- 新旧数据必须先完整校验；安装与 rollback 由 migration owner 负责。
- 测试使用独立同名正式工作区，每批运行 ruff 与 compileall。
