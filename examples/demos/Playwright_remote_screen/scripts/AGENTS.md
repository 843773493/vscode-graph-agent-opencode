# 目录用途

`scripts/` 存放 Playwright 远程屏幕实验项目的本地开发脚本。

```text
scripts/
└── check.mjs  用 Bun 解析器递归检查项目 JavaScript 语法
```

# 可修改内容

- 检查、测试和辅助运行脚本
- 仅服务于当前实验项目的 Node.js 工具脚本

# 不可修改内容

- 不要在这里接入主应用构建流程
- 不要写入实验项目之外的文件

# 规范

- 脚本使用 ESM
- 语法检查使用 Bun.Transpiler，不启动额外的 Node.js 子进程
- 检查脚本从自身位置定位 demo 根目录，并递归语法检查 `src/`、`scripts/`、`tests/` 下的 JavaScript 文件
- 失败时直接退出非零状态
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
