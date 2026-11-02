# 目录用途

验证 Terminal/Browser 创建链的 intent、原 operation 占用、派发及回执结算。

# 可修改内容

本组 pytest fixture、真实 owner/账本入口测试及原生写入故障屏障。

# 不可修改内容

不实现另一套创建或恢复链，不将传输替身作为真实 PTY/Chromium 终态证据。

# 规范

通过原 operation 创建关键状态；取消、关闭和写盘失败须核对精确身份、冷读原账本及外部调用次数。屏障必须自带释放上限，并保留原错误和取消消息。

未核实的原生created/running观察须保留creating阶段；用原创建入口产生未知结果和lease，冷owner管理stop先取消原创建再停止，原operation不得接管。传输替身只证明消费与顺序，真实进程终态另由集成证明。
