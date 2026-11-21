# 目录用途

存放 Node SDK 随包交付的生成资源，使 workspace 和 tarball 安装使用相同包内路径。

# 可修改内容

通过 `bun scripts/plugins/prepare-product-sdk.mjs node` 从唯一源更新生成资源。

# 不可修改内容

不得在此维护独立的日志合同或产品业务配置。

# 规范

生成结果纳入版本控制；只读准备检查不得回写文件。
