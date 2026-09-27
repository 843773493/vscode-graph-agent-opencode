# 目录用途

`scripts/install/` 存放本地发布包安装和运行时安装后处理入口。

# 可修改内容

- 可以维护本地 runtime tarball 校验和安装后处理脚本。
- 可以维护检查源码指纹、必要时触发本地打包并安装 tarball 的入口脚本；版本必须从唯一发布版本源读取。
- 可以维护安装脚本所需的明确错误处理和环境变量读取。

# 不可修改内容

- 不在这里实现 Agent、Gateway 或工作区业务逻辑。
- 不把下载后的 runtime、node_modules 或其他构建产物提交到仓库。

# 规范

- 安装脚本必须校验构建输入指纹、本地 tarball 的 SHA-256 和最终安装文件，失败时直接终止；不得从公共 npm registry 获取 BoxTeam 包。
- 脚本应从当前工作目录或显式传入路径解析安装目标，不猜测仓库根目录。
- 发布打包入口位于 `scripts/release/`，构建实现位于 `packaging/runtime/`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
