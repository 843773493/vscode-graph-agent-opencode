# tests/unit/services/infrastructure/config

## 目录用途

存放 `app/services/infrastructure/config/` 配置快照、存储与监听器的单元测试。

## 可修改内容

- 配置重载、修订快照和文件监听行为测试。
- 临时配置文件与日志捕获 fixture。

## 不可修改内容

- 不写入真实用户或工作区配置。
- 不在此测试上层 ConfigService 业务接口。

## 规范

- 文件写入必须使用 pytest 临时目录。
- 重载失败和配置错误必须显式断言。
