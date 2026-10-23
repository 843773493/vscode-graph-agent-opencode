## 1. 唯一加载与后台刷新

- [x] 1.1 实现本地优先加载、按来源隔离的原子缓存、有界前台等待和后台刷新，失败明确记录并退避。
- [x] 1.2 迁齐 Chat、Responses、Embedding、能力注册及回放的直接和间接导入；后续请求采用新目录时保留 runtime registrations 和查询一致性。

## 2. 验证与交付

- [x] 2.1 覆盖迟到、整体超时、损坏缓存、来源隔离、本地模式、并发单刷新、注册保留及前后台原环境恢复。
- [x] 2.2 真实本地 HTTP 与冷进程验证后台成功后下一请求采用、下次启动离线缓存可用，并验证 Provider 同步/异步调用。
- [x] 2.3 完成相关静态分析、严格 OpenSpec 校验和差异检查，保留准确命令、源码指纹、原始输出与退出码。

验收原件位于 `out/tests/temp/model_metadata_refresh/artifacts/runs/`：`metadata-final-unit-004` 105 passed/7.59秒，`metadata-final-http-process-004` 1 passed/14.24秒，`metadata-final-provider-http-002` 6 passed/23.23秒，均 exit0；覆盖真实 SDK/TCP、两个冷进程、同步/异步 Provider wire/hash 合同。`metadata-final-static-005` Ruff exit0。全部测试使用外置 timeout 与4GiB数据段限制，并记录源码前后 SHA、准确命令、raw 与退出码。首轮集成失败分别为新测试装具导入路径错误和 SDK stdout 干扰测试回执，已修正并保留原件；不把局部检查称为完整 Web 验收。最终源码、独立范围补丁与静态/规范复核见同目录 `delivery.json`、`owned.patch` 和 `metadata-final-review-006`。
