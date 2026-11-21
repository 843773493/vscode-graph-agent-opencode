# 目录用途

Python SDK 路由与已打包依赖的独立插件消费验收。

## 可修改内容

SDK 观察路由、离线探针插件及真实 MCP bridge 的 SDK 交付合同。

## 不可修改内容

不复制产品实现，不以开发源码补齐离线依赖。

## 规范

制品复用原 prepared_mcp_stdio_package；维护插件和 bridge 共用不可变包，各自隔离实例。Backend 完整制品初始化归 integration/packaging。
