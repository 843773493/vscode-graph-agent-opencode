# 目录用途

保存从根目录 `resources/logging/runtime-contract.json` 生成的 Node SDK 日志合同。

# 可修改内容

通过 `prepare-product-sdk.mjs node` 更新生成文件。

# 不可修改内容

不得手改生成的 `runtime-contract.json`；唯一可编辑源仍归根日志资源目录。

# 规范

`prepare-product-sdk.mjs --check node` 核对一致性，workspace 开发和 tarball 制品通过同一加载入口读取。
