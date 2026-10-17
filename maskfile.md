# vscode-graph-agent 项目脚本
项目脚本统一通过 `mask` 调用。命令按用途分组，运行 `mask --help` 可查看完整列表。
## dev
> ### start
> [开发] 启动后端、Gateway、Web 和辅助服务
```sh
  bun run dev
```
> ### stop
> [开发] 停止当前 worktree 的 transient systemd 开发服务
```sh
  bun run dev:stop
```
> ### status
> [开发] 查看当前 worktree 的 transient systemd 开发服务状态
```sh
  bun run dev:status
```
> ### backend
> [开发] 仅启动 Workspace 后端
```sh
  bun run scripts/launch/dev.mjs --service=backend
```
> ### gateway
> [开发] 仅启动 Workspace Gateway
```sh
  bun run scripts/launch/dev.mjs --service=gateway
```
> ### web
> [开发] 仅启动当前纯 Web 客户端
```sh
  bun run scripts/launch/dev.mjs --service=web
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
> ### suite <name> [focus]
> [测试] 运行测试矩阵中的指定套件
> 必须显式传入套件名。`e2e-web` 运行整个 Web E2E 目录，`e2e-web-basic-chat-tool-loop` 只运行 Section 8 目标 node。两者都要求通过 `focus` 参数或 `BOXTEAM_E2E_SECTION8_FOCUS` 显式选择该 node 内的 `default` 场景集或合法 scenario key；focus 不改变 pytest node 范围。多个 key 用逗号分隔，`read_cursor_expiry` 必须与 `read_recovery` 连续选择并位于最后。
```sh
  name="${name:?请显式指定测试矩阵 suite 名称}"
  if [ "$name" = "e2e-web" ] || [ "$name" = "e2e-web-basic-chat-tool-loop" ]; then
    focus="${focus:-${BOXTEAM_E2E_SECTION8_FOCUS:-}}"
    focus="${focus:?运行 Web E2E suite 必须显式指定 Section 8 focus}"
    BOXTEAM_E2E_SECTION8_FOCUS="$focus" bun run test:matrix -- --suite="$name"
  elif [ -n "$focus" ]; then
    echo "focus 只适用于 Web E2E suite" >&2
    exit 2
  else
    bun run test:matrix -- --suite="$name"
  fi
```
## install
> ### release
> [安装] 本地打包并安装当前项目的 BoxTeam CLI 和平台 runtime
```sh
  bun run scripts/install/install-release.mjs
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
> ### breaking [baseline]
> [协议] 使用已接受的 Buf image 或 descriptor 文件检查破坏性变更
```sh
  baseline="${baseline:?请提供已接受的 Buf image 或 descriptor 文件路径}"
  bun run check:protocol:breaking "$baseline"
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
  bun run scripts/launch/cross-platform-development-target.mjs status "$target"
```
> ### start [target]
> [开发目标] 启动指定开发目标
```sh
  target="${target:-docker-debian}"
  bun run scripts/launch/cross-platform-development-target.mjs start "$target" --profile development
```
> ### stop [target]
> [开发目标] 停止指定开发目标
```sh
  target="${target:-docker-debian}"
  bun run scripts/launch/cross-platform-development-target.mjs stop "$target" --profile development
```
> ### restart [target]
> [开发目标] 重启指定开发目标
```sh
  target="${target:-docker-debian}"
  bun run scripts/launch/cross-platform-development-target.mjs restart "$target" --profile development
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
