# vscode-graph-agent 项目脚本
项目脚本统一通过 `mask` 调用。命令按用途分组，运行 `mask --help` 可查看完整列表。
## dev
> ### start
> [开发] 启动后端、Gateway、Web 和辅助服务
```sh
  bun run scripts/dev.mjs --service=all --only-launch
```
> ### backend
> [开发] 仅启动 Workspace 后端
```sh
  bun run scripts/dev.mjs --service=backend
```
> ### gateway
> [开发] 仅启动 Workspace Gateway
```sh
  bun run scripts/dev.mjs --service=gateway
```
> ### web
> [开发] 仅启动当前纯 Web 客户端
```sh
  bun run scripts/dev.mjs --service=web
```
## check
> ### python
> [检查] 运行 Python 静态检查
```sh
  uv run ruff check .
```
> ### web
> [检查] 构建当前纯 Web 客户端
```sh
  bun run build:web
```
> ### protocol
> [检查] 校验 protobuf schema、descriptor 和生成代码的新鲜度
```sh
  bun run check:protocol
```
## test
> ### list
> [测试] 查看正式测试矩阵
```sh
  bun run test:matrix -- --list
```
> ### suite [name]
> [测试] 运行测试矩阵中的指定套件
```sh
  name="${name:-unit-python}"
  bun run test:matrix -- --suite="$name"
```
> ### unit
> [测试] 运行 Python 单元测试
```sh
  uv run pytest tests/unit
```
> ### contracts
> [测试] 运行协议契约测试
```sh
  uv run pytest tests/contracts
```
> ### integration
> [测试] 运行 Python 集成测试
```sh
  uv run pytest tests/integration
```
> ### e2e-web
> [测试] 运行当前纯 Web E2E 测试
```sh
  uv run pytest tests/e2e/clients/web
```
> ### e2e-basic-chat-tool-loop
> [测试] 运行 Web 基础聊天工具循环完整流程 E2E
```sh
  uv run pytest tests/e2e/clients/web/test_basic_chat_tool_loop.py
```
> ### e2e-gateway
> [测试] 运行 Gateway 真实进程 E2E 测试
```sh
  uv run pytest tests/e2e/system/gateway
```
## config
> ### install
> [配置] 安装或更新当前源码开发配置
```sh
  bun run install:config
```
> ### diagnose
> [配置] 输出当前配置诊断信息
```sh
  uv run python -m configs.boxteam diagnose --project-root .
```
## protocol
> ### generate
> [协议] 生成协议 bindings 和相关派生文件
```sh
  bun run gen:protocol
```
> ### breaking
> [协议] 检查相对于现有 proto 的破坏性变更
```sh
  bun run check:protocol:breaking
```
## openspec
> ### status (change)
> [OpenSpec] 查看指定 change 的当前任务和阶段状态
```sh
  openspec status --change "$change"
```
> ### validate (change)
> [OpenSpec] 严格校验指定 change 的 proposal、design、spec 和 tasks
```sh
  openspec validate "$change" --type change --strict
```
## target
> ### status [target]
> [开发目标] 查看 Linux、Windows 或 Docker 开发目标状态
```sh
  target="${target:-docker-debian}"
  bun run scripts/cross-platform-development-target.mjs status "$target"
```
> ### start [target]
> [开发目标] 启动指定开发目标
```sh
  target="${target:-docker-debian}"
  bun run scripts/cross-platform-development-target.mjs start "$target" --profile development
```
> ### stop [target]
> [开发目标] 停止指定开发目标
```sh
  target="${target:-docker-debian}"
  bun run scripts/cross-platform-development-target.mjs stop "$target" --profile development
```
> ### restart [target]
> [开发目标] 重启指定开发目标
```sh
  target="${target:-docker-debian}"
  bun run scripts/cross-platform-development-target.mjs restart "$target" --profile development
```
## package
> ### linux
> [打包] 构建 Linux x64 发行包
```sh
  bun run package:linux-x64
```
> ### windows
> [打包] 构建 Windows x64 发行包
```sh
  bun run package:windows-x64
```
> ### windows-cross
> [打包] 跨平台构建 Windows x64 发行包
```sh
  bun run package:windows-x64-cross
```
