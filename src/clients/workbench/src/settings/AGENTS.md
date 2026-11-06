# 目录用途

通过原 Command／Quick Input 维护明确目标工作区的业务配置来源。

# 可修改内容

默认 Agent 来源的选择、JSONC 单键编辑、来源预检和提交状态展示。

# 不可修改内容

不维护另一份配置真相，不注册到 Code OSS 配置服务，不复制 HTTP 客户端或绕过来源 writer，不代替 Backend 重启与健康证明。

# 规范

冻结 Gateway／工作区／来源；只读来源仍可查看。复用 web API owner 和原 JSONC AST 编辑器，完整保留 CAS 与幂等身份；冲突保留输入，显式重新预检，未知结果仅查询原提交。使用中文提示与注释。
