# tests/unit/services/infrastructure/resource_platform/observation

## 目录用途

存放 `app/services/infrastructure/resource_platform/observation/` 资源观察通道的单元测试。

## 可修改内容

- 资源观察通道的订阅、队列溢出、gap 恢复与通知值校验测试。
- 仅供本目录使用的 pytest fixture。

## 不可修改内容

- 不测试资源来源解析、导航或虚拟 URI 规则。
- 不访问真实工作区、外部网络或运行中的服务。

## 规范

- 依赖通过 pytest fixture 注入；通道状态必须由测试隔离。
- 断言覆盖队列容量、gap 状态转换和订阅取消行为。
