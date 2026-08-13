# tests/unit/browser

## 目录用途

存放 `src/workspace-services/browser/` 独立 Browser 服务源根的 Python 单元测试与跨语言轻量验证。

## 可修改内容

- Browser 服务纯函数和协议辅助行为的单元测试。
- 测试所需的最小 Node 脚本片段。

## 不可修改内容

- 不在此启动真实浏览器进行 E2E 操作。
- 不把 Browser 测试错误归入 `app/` 的生产模块分区。

## 规范

- JavaScript 相关命令统一使用 Bun。
- 临时脚本和输出必须由 pytest 临时目录承载。
