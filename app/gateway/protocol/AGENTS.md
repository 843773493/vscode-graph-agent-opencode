# 目录用途

存放 Gateway 控制面协议与透明代理边界的 Python adapter。

## 可修改内容

- 可以维护 Gateway 健康、工作区注册和代理元数据的协议转换。

## 不可修改内容

- 不得 import Workspace、Terminal 或 Browser 业务协议。
- 不得在这里实现 Gateway 之外的业务规则。

## 规范

- 只依赖 `common` 和 `gateway` 生成绑定。
- 代理请求体和响应体保持透明，不复制被代理服务的 schema。
