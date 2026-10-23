---
applyTo: tests/unit/terminal_prototype/**
---

# 目录作用

放置终端原型相关的最小实验测试，验证持续化 shell 会话、node-pty 执行和后续 xterm.js 接入的接口形状。

当前文件与执行链：

```text
terminal_prototype/
├── AGENTS.md
├── test_node_pty_smoke.py
│   └── 启动 node_pty_smoke.mjs，并核对输出、退出状态和工作目录
└── node_pty_smoke.mjs
    ├── node-pty
    └── 平台 shell
        ├── Windows：COMSPEC 或 cmd.exe
        └── Linux/macOS：SHELL 或 /bin/sh
```

# 可以修改

- 该目录下所有测试文件
- 与终端原型测试直接相关的辅助脚本
- 该目录下的 AGENTS.md

# 不要修改

- 其它测试目录中的文件，除非明确需要联动调整
- 后端生产实现文件，除非本目录测试已验证需要同步变更

# 约定

- 这里优先放最小可运行原型，不追求完整产品化实现
- 测试命名要清晰表达“原型/烟雾测试”属性
- 跨平台行为要显式处理，Windows 与类 Unix 路径/命令差异要写清楚
- 后续若接入真实终端会话，先保留原型测试作为回归保障
