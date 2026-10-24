# 目录用途

验证 Gateway 主题与图片资源 owner 的输入、存储、错误和引用合同。

# 可修改内容

- 主题解析、图片导入与清单读写的定向测试及 fixture。

# 不可修改内容

- 不启动真实 Gateway 或工作区进程，不访问正常用户配置。

# 规范

- 使用 pytest fixture 注入隔离资源，真实 HTTP 验收放在 integration/gateway。
