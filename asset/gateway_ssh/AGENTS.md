# 目录用途

`asset/gateway_ssh/` 存放 Gateway 跨端 e2e 测试专用 SSH 密钥对。

# 可修改内容

- 测试专用 SSH 私钥和公钥。
- 与 Gateway Docker 测试容器认证相关的说明文件。

# 不可修改内容

- 不要放入生产 SSH 私钥或用户个人 SSH 私钥。
- 不要把本目录密钥用于非测试容器环境。

# 规范

- 私钥仅用于本仓库本地 e2e 测试，开发安装时使用专用名称复制到用户 `.ssh` 目录。
- 公钥由 `tools/Dockerfile.gateway-test` 在构建测试容器时写入 `authorized_keys`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
