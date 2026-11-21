# 目录用途

存放插件 SDK 的协议生成产物及其子包。

# 可修改内容

仅由 bun run gen:protocol 从 proto/boxteam/ 及固定生成配置更新。

# 不可修改内容

不得手写 wire 类型、修改生成正文或导入产品业务实现。

# 规范

协议源码只有 proto/boxteam/ 一份；使用 bun run check:protocol 核对产物，包内不得建立平行协议。
