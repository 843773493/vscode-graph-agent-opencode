## Why

LiteLLM 首次导入同步下载全模型价格与能力目录，外部网络超时会阻塞首个模型请求，也会挤占本地回放测试的业务期限。用户要求超时使用旧表，后台继续更新并供后续使用。

## What Changes

- 将 Chat、Responses、Embedding、认证能力注册和回放的 LiteLLM 导入收敛到运行时入口，首次导入只读本地目录。
- 优先使用上次成功缓存；前台仅短暂等待有界后台刷新，超时继续使用旧目录，刷新结果供后续请求和进程采用。
- 复用 LiteLLM 的远程校验、重试、模型注册恢复与查询缓存失效机制；失败明确记录，不影响原模型请求的错误。
- 保留显式只用本地表的官方开关，测试可禁止远程刷新。

## Capabilities

### New Capabilities

- `model-metadata-refresh`: 模型公共元数据的有界等待、后台刷新、缓存及唯一加载入口。

### Modified Capabilities

无。

## Impact

仅涉及后端运行时 LiteLLM 加载、Provider 请求准备和对应测试。缓存属于用户级公共元数据，位于 `BOXTEAM_HOME/cache/`，不包含会话正文或凭据；不改变模型请求参数、历史 assembly、数据库 schema 或 Gateway 业务。
