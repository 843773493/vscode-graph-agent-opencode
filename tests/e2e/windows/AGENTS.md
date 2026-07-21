# 目录用途

`tests/e2e/windows/` 存放 Windows 与 VMware 开发目标的系统兼容性 E2E 测试。

# 可修改内容

- Windows 平台进程、路径、SSH、Gateway 联邦和安装兼容性测试。
- 测试所需的 fixture 与平台断言。

# 不可修改内容

- 当前没有 Windows 虚拟机资源时，不得伪造真实 Windows 测试通过。
- 不得在此目录实现生产运行时逻辑。

# 规范

- TODO: 获得 VMware Windows 资源后补充真实测试。
- 测试产物写入 `out/tests/e2e/windows/<测试文件名>/`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
