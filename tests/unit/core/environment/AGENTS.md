# tests/unit/core/environment

## 目录用途

存放运行环境与工作区边界单测，覆盖 BOXTEAM_HOME 环境读取、多工作区注册、用户存储迁移及后端工作区初始化。

## 可修改内容

- 新增或修改 `app/core/` 环境、工作区注册和存储迁移逻辑对应的单元测试。
- 添加仅服务于本目录测试的 fixture。

## 不可修改内容

- 不在此放置集成测试、e2e 测试或会话业务测试。
- 不在此实现生产环境、Gateway 或工作区业务逻辑。

## 规范

- 测试名应清楚说明环境输入、工作区边界和预期效果。
- 工作区数据只能写入 `tmp_path` 或本目录对应的 `out/tests/unit/core/environment/`。
- 使用 pytest fixture 注入环境变量和依赖，避免读写用户真实配置。
