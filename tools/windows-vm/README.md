# 本项目 Windows VM 工具

该工具复用 Kunlun-Code 的持久 Windows Server VM，但使用 A4500 上独立的远端仓库目录，避免覆盖参考项目。连接链路为：

本机 --SSH--> A4500 Linux --SSH(127.0.0.1:22022)--> Windows guest

## 本地配置

复制 \`remote.env.example\` 为 \`tools/ssh/windows-vm-remote.env\`，填写远端目录和本机私钥。真实配置、私钥和 host key 已由仓库根目录 \`.gitignore\` 排除；私钥必须是当前用户可读且权限为 \`600\`。

同步会包含仓库根目录的本地 \`.env\`，用于让 Windows 客体复用当前工作区的测试配置；该文件不会进入 Git，也不会在日志中打印具体值。

默认工具命令：

\`\`\`bash
tools/windows-vm/windows-vm.sh config
tools/windows-vm/windows-vm.sh check
tools/windows-vm/windows-vm.sh sync
tools/windows-vm/windows-vm.sh activate
tools/windows-vm/windows-vm.sh guest-info
tools/windows-vm/windows-vm.sh bootstrap-runtime
tools/windows-vm/windows-vm.sh bootstrap-js
\`\`\`

\`activate\` 会停止 VM、把 QEMU 的 SMB 根目录切换到当前项目的远端仓库、重新启动 VM。它会在远端 state 目录保存一次切换前的 \`config.env\`，完成后可用 \`restore-share\` 恢复。

## 固定模块闭环

所有模块都会先同步当前工作区，并把输出保留到 \`out/tests/temp/windows-vm/artifacts/\`：

\`\`\`bash
tools/windows-vm/windows-vm.sh run-module runtime
tools/windows-vm/windows-vm.sh run-module python-unit
tools/windows-vm/windows-vm.sh run-module python-static
tools/windows-vm/windows-vm.sh run-module gateway-unit
tools/windows-vm/windows-vm.sh run-module terminal-powershell
tools/windows-vm/windows-vm.sh run-module dev-windows
tools/windows-vm/windows-vm.sh run-module js-platform
tools/windows-vm/windows-vm.sh run-module backend-js
tools/windows-vm/windows-vm.sh run-module web-build
tools/windows-vm/windows-vm.sh run-module webview-build
tools/windows-vm/windows-vm.sh run-module extension
tools/windows-vm/windows-vm.sh run-module full-python
\`\`\`

每个失败模块都必须记录原始错误、修复范围和复测结果；不能用其它模块通过替代它。

终端 PowerShell E2E 会把正式测试目录复制到远端 `out/windows-vm/terminal-powershell/`，可用 `tools/windows-vm/windows-vm.sh collect` 收回本地复查。
