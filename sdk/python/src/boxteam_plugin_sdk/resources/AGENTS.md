# 目录用途

保存 Python SDK 随包交付的生成资源，供 editable 和 wheel 安装使用。

# 可修改内容

通过 `bun scripts/plugins/prepare-product-sdk.mjs python` 从唯一源更新资源。

# 不可修改内容

不得手改 `runtime-contract.json`；其唯一可编辑源为根目录 `resources/logging/runtime-contract.json`。

# 规范

生成文件纳入版本控制；`prepare-product-sdk.mjs --check python` 校验一致性，不回写文件。
