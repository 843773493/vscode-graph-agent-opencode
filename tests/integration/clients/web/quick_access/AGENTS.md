# 目录用途

验证完整工作台快速访问、原搜索服务与业务 owner 的接线和范围隔离。

# 可修改内容

定向服务集成测试、真实 Gateway／Workbench 浏览器场景及专用装具。

# 不可修改内容

不复制生产搜索、编辑器或 Quick Input 实现，不修改固定上游或运行其测试。

# 规范

服务装具经生产 esbuild 与 legacy decorators 编译真实源码。明确记录 Gateway 传输替身和模型装具边界；真实页面通过 Gateway。产物按测试文件镜像，保留外部超时和内存保护。

`default_contract` 在浏览器中运行真实 Provider，只有 Picker 事件及宿主服务替身，用于调用参数、取消与接受的合同检查；不能将它称为原 Chat／空文件编辑器或完整页面验收。`default_access` 经真实 Gateway 创建关键状态，和原搜索／恢复场景一起使用正式 Workbench 构建。

`test_query_lifecycle` 用隔离 A/B/C、产品 API 创建的 Session 和正式 Workbench 验证搜索分支。只在明确场景延迟真实响应或注入 503／401，记录原请求、取消和 request_id；核对排队不发送、超时、单分支重试、离线零查询及身份失效，不以这些替身证明真实服务故障。

整份编辑器设置使用 `settings.mjs` 经真实剪贴板粘贴、保存，并只读实际存储核对 JSON；不要用逐键多行插入留下自动补括号生成的无效配置，也不要直接预写 IndexedDB 替产品完成保存。
