# 目录用途

集中存放 Integration 使用的模型、Gateway target、MCP 和网页协议替身。

```text
http_stubs.py
├── HTTPStubState                      请求记录与响应回调
├── _running_http_stub_server           三种替身共用的 socket 与线程生命周期
├── generation_target_stub              Gateway 下游身份与会话生成协议
├── openai_chat_stub                    Chat 文本响应及可控失败
└── openai_chat_tool_loop_stub          Chat 工具调用与工具结果续写
```

协议 Handler 决定请求与响应，私有运行上下文持有监听 socket 和服务线程。新增协议行为留在对应替身，不复制启动、停机或清理代码；保持公共上下文只返回请求状态，不向消费者暴露服务器句柄。

## 可修改内容

- 明确、确定性、无外部副作用的测试替身和请求记录器。

## 不可修改内容

- `tests/e2e/` 不得导入本目录。
- 不要伪造被测生产模块本身。

## 规范

- 类型和命名必须包含 `stub`/`fake` 等明确语义。
- 替身不得被生产代码导入，也不得静默替代真实依赖。
- 绑定 socket 后立即登记关闭；只有线程成功启动后才登记 shutdown 和 join。一个清理动作失败或中断后仍尝试其余动作，join 的五秒等待结束后必须确认线程已退出。
- 生命周期故障注入归 `tests/unit/harness/processes/`；mock 清理记录不能代替真实 HTTP 响应与 socket 释放证据。
