# 目录用途

验证完整工作台快速访问、原搜索服务与业务 owner 的接线和范围隔离。

# 可修改内容

定向服务集成测试、真实 Gateway／Workbench 浏览器场景及专用装具。

# 不可修改内容

不复制生产搜索、编辑器或 Quick Input 实现，不修改固定上游或运行其测试。

# 规范

服务装具经生产 esbuild 与 legacy decorators 编译真实源码。明确记录 Gateway 传输替身和模型装具边界；真实页面通过 Gateway。产物按测试文件镜像，保留外部超时和内存保护。

`default_contract` 在浏览器中运行真实 Provider，只有 Picker 事件及宿主服务替身，用于调用参数、取消与接受的合同检查；不能将它称为原 Chat／空文件编辑器或完整页面验收。`default_access` 经真实 Gateway 创建关键状态，和原搜索／恢复场景一起使用正式 Workbench 构建。

`test_query_lifecycle` 用隔离 A/B/C、产品 API 创建的 Session 和正式 Workbench 验证搜索分支。名称统一观察 Gateway SSE；`query_gateway_fixture.py` 仅替换本测试进程的出站网络边界，延迟指定真实领域响应或注入具名读取失败，记录 Gateway 实际分发、期限、取消和 request_id。名称及文件读取失败均在同一 Gateway 出站边界注入；身份 401 在明确浏览器 HTTP 边界注入。核对三并发占满时第四分支不分发、超时保留其他结果、单分支重试、停止目标零业务读取及身份失效；不替换匹配、调度、SSE 编码或业务状态，不以这些替身证明真实服务故障。正常名称／文件和正文验收仍使用真实 Gateway／后端。

整份编辑器设置使用 `tests/support/workbench/settings.mjs` 经真实剪贴板粘贴、保存，并只读实际存储核对 JSON；不要用逐键多行插入留下自动补括号生成的无效配置，也不要直接预写 IndexedDB 替产品完成保存。真实搜索观察同样复用该 support 目录；`query_faults.mjs` 只控制本组显式故障装具，不供 E2E 调用。

页面端口复用本目录 `conftest.py` 的租约 fixture，在各测试自己的端口块内有界选择空闲端口并保留绑定，记录探测原件；占用不终止未知进程，其他租约错误立即暴露。

`file_input.test.mjs` 编译真实 `FileEditorInput` 及同一源码转换，只在文件服务／模型引用服务边界提供计数装具；验证并发引用、解析中关闭与失败后重试，不替代真实页面或 Gateway 模型验收。

`test_request_content` 经真实消息执行生成请求日志，原页面定位后再核损坏／异查询／catalog变化的续点拒绝；正式Python搜索SDK消费同一Gateway联邦诊断，不在驱动内复制SSE解码。首次选择核活动稳定身份后走真实Enter，后续保留鼠标选择和精确位置断言。
