# tests/unit/core/configuration

## 目录用途

存放核心运行配置单测，覆盖配置来源快照、checkpoint 配置构造和日志配置。

## 可修改内容

- 新增或修改 `app/core/` 配置加载与配置构造逻辑对应的单元测试。
- 添加仅服务于本目录测试的 fixture。

## 不可修改内容

- 不在此放置 API 集成测试、e2e 测试或与配置无关的会话状态测试。
- 不在此实现生产配置策略或日志初始化逻辑。

## 规范

- 测试名应表达配置来源、运行条件及可观察结果。
- 临时配置只写入 `tmp_path`；持久测试产物使用 `out/tests/unit/core/configuration/`。
- 使用 pytest fixture 注入配置文件、环境变量和依赖。
