# 通用指令

## 常用命令

### JS/TS — Bun

仓库内优先使用本地 `tools/bun.exe`，避免依赖全局 bun。

| 用途 | 命令 |
|------|------|
| 运行脚本 | `bun run <script.js>` |
| 添加依赖 | `bun add <package>` |
| 移除依赖 | `bun remove <package>` |
| 安装所有依赖 | `bun install` |
| 安装用户级配置 | `bun run install:config` |
| 执行 package.json 脚本 | `bun run <script-name>` |

### Python — uv

| 用途 | 命令 |
|------|------|
| 运行脚本 | `uv run python <script.py>` |
| 添加依赖 | `uv add <package>` |
| 移除依赖 | `uv remove <package>` |
| 同步依赖（首次/拉取后） | `uv sync` |
| 运行测试 | `uv run pytest` |
| 运行 lint | `uv run ruff check .` |

> 依赖同步完成后，执行 `uv run python scripts/setup_test_env.py sync-bun` 可自动准备/同步仓库内的 `tools/bun.exe` 和 `src/webview-ui` 依赖。
