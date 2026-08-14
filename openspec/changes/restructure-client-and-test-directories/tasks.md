## 1. 目录规范与文档基线

- [x] 1.1 更新根 `AGENTS.md`、`src/AGENTS.md` 与测试架构文档，明确当前只开发纯 Web，其他客户端仅为 TODO
- [x] 1.2 创建 `src/clients`、共享层、预留客户端与 `src/workspace-services` 的四段式 `AGENTS.md`
- [x] 1.3 创建 `tests/clients`、`tests/contracts`、`tests/harness`、`tests/runner` 及子目录规范，写明严格 E2E 判定

## 2. 源码目录迁移

- [x] 2.1 将 `src/web` 原样迁移到 `src/clients/web`，保留当前调试工作台改动和未跟踪源码
- [x] 2.2 更新开发、构建、类型生成、配置、包命令和活跃文档中的纯 Web 路径
- [x] 2.3 将 `src/browser` 与 `src/terminal` 迁移到 `src/workspace-services`，更新所有生产入口、资源定位和测试引用
- [x] 2.4 搜索并清除被迁移旧路径的活跃引用，不创建兼容转发目录

## 3. 测试架构迁移

- [x] 3.1 建立客户端共享场景、选择器、能力与 Web Playwright 驱动骨架，其他平台驱动只保留 TODO 规范
- [x] 3.2 建立 JavaScript/Python harness 与 suite runner/matrix，保证运行上下文和产物路径显式可审计
- [x] 3.3 将契约测试入口统一到 `tests/contracts`，更新测试发现、引用和文档
- [x] 3.4 审计 `tests/e2e` 中的服务替身、Playwright 路由替换和替代运行面，将命中的用例迁到对应 Integration 分区
- [x] 3.5 将 E2E 系统测试按 agent、gateway、workspace_services 分区，并保持真实客户端 Web E2E 在 `tests/e2e/clients/web`
- [x] 3.6 拆除仅用于调用 Node Playwright 脚本的 Python 薄包装层；保留确实拥有 Python 运行时 fixture 的编排测试

## 4. 文档与配置收口

- [x] 4.1 更新当前架构、开发运行、测试运行与产物说明中的新目录和命令
- [x] 4.2 更新忽略规则、测试路径映射和发布/打包资源清单，确保新目录不会产生错误跟踪或遗漏
- [x] 4.3 复查所有新增源码目录的 `AGENTS.md` 四段结构和 TODO 边界

## 5. 验证

- [x] 5.1 运行 OpenSpec strict validate、旧路径引用检查和目录边界检查
- [x] 5.2 运行受影响 JavaScript/TypeScript 静态分析、Bun 单元测试和纯 Web 生产构建
- [x] 5.3 运行 pytest 收集、受影响 unit/integration/E2E 测试并修复迁移回归
- [x] 5.4 对照 proposal、spec、design 和任务逐项完成架构审计，确认没有把替身测试留在 E2E
