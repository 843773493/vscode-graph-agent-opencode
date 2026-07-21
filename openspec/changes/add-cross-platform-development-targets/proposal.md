## Why

现有 Gateway Docker E2E 依赖把宿主机仓库直接挂载进容器，并在项目根目录创建专用 `.venv_docker_*`，因此无法复用于 VMware Windows 等独立目标，也无法真实验证远程源码开发、纯净安装和 Gateway 联邦连接。需要把测试目标提升为经 SSH 管理、状态持久且拥有完整开发仓库的跨平台开发环境。

## What Changes

- 新增统一的跨平台开发目标管理能力，通过目标配置管理 Docker、Linux 虚拟机和 Windows 虚拟机。
- 宿主机把当前 Git 工作区（含已跟踪修改和未忽略的未跟踪文件）制作成临时快照提交，经 SSH 增量推送到目标端完整仓库的专用引用，不再自动挂载当前仓库。
- 目标端保留一个可直接开发和测试的完整仓库；激活宿主机快照前要求目标工作区干净，避免覆盖目标端本地开发修改。
- 通过目标 SSH 配置安全复制最新版 `.env`，校验后原子替换；`.env` 不进入快照、不输出内容。
- 每个目标仓库使用自身构建的标准 `.venv`，移除项目根目录 `.venv_docker_*` 及相关路径探测逻辑。
- Docker 把目标用户 Home、完整仓库、缓存和测试产物持久化在 `out/` 的专用子目录中，容器重建后仍可继续使用。
- 开发运行默认使用 `~/.boxteams-dev/boxteam_workspace`；纯净安装验证使用隔离的 `~/.boxteams/boxteam_workspace`。
- 提供平台适配的启动、停止、状态、同步、初始化、远程 shell、测试和产物收集工具；Linux 使用 shell，Windows 使用 PowerShell。
- 将现有 Docker Gateway E2E 改为使用目标无关的 SSH 管理接口，并新增 `tests/e2e/windows/` 兼容性测试目录骨架，暂不增加 Windows 测试用例。
- 目标端既可接受远程 SSH 自动化，也可本地操作，还可作为其他 Gateway 的远程连接与联邦功能测试对象。

## Capabilities

### New Capabilities

- `cross-platform-development-targets`: 定义独立完整开发目标的配置、快照同步、环境初始化、双运行配置、持久化、平台适配和测试接入行为。

### Modified Capabilities

无。

## Impact

- 主要影响 `scripts/` 下的开发目标编排脚本、`tools/` 下的 Docker/平台工具、`tests/e2e/gateway/` 的 Docker 辅助层和测试入口。
- 删除现有项目根目录 `.venv_docker_debian13`，并移除 `.venv_docker_*` 的创建、查找和使用逻辑。
- Docker 不再依赖宿主机仓库 bind mount；目标仓库、用户状态和缓存改由 `out/cross-platform-dev-targets/` 持久化。
- 新增包含 SSH 主机、用户、端口、平台和目标路径的本地目标配置；真实配置及密钥不提交 Git，只提交可读示例和 schema。
- 不改变 Gateway 联邦协议、工作区业务 API 或已安装 Launcher 的外部行为。
