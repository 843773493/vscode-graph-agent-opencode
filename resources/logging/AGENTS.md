# 目录用途

存放 Python、Launcher 和辅助服务共同消费的只读产品日志合同。

# 可修改内容

- 角色路径、轮转与旧实例保留规则、记录字段及跨语言验收样本。

# 不可修改内容

- 不存日志、用户配置覆盖或会话业务数据。
- 不复制到各语言模块中另行维护默认值。

# 规范

- 源码与发行包均从显式 application root 下的本目录读取。
- 合同变更同时迁齐 Python 与 JS 消费者及共享样本。
- 旧实例日志总预算的缺省值只定义在 `runtime-contract.json` 的 `retention.inactive.max_bytes`；Gateway 配置只提供可选覆盖，不复制缺省数值。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
