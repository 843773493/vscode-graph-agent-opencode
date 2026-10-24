# 目录用途

存放 Browser Manager 使用测试页面服务、受控网络条件或其他替身边界的集成测试。

```text
browser/
├── test_browser_custom_tools.py        代理工具与Browser Manager接线
├── test_browser_resource_performance.py 资源治理、帧传输与三平台性能采样
├── browser_element_references.mjs       真实DOM中的元素引用与动作
└── browser_checkpoint_form_state.mjs    表单检查点捕获、释放与恢复
```

`browser_element_references.mjs` 直接使用真实Chromium验证页面摘要、坐标检查与按ref点击的组合，覆盖动态新增、移除、重排、文档版本变化，以及摘要达到元素上限后停止尾部可见性检查；通过 `bun run test:matrix -- --suite=integration-browser-element-references` 运行，固定60秒/4GiB。它不启动Gateway或主Web，也不属于服务端纯单元测试矩阵；Chromium临时文件写入本测试正式输出的runtime目录。

坐标检查同时核CSSOM在一次检查内只整理一次、嵌套与继承规则仍可读、下一次能看到规则更新，以及跨域规则不可读时仍返回可读来源与最终样式；计数断言限定重复解析成本，不以机器耗时作为通过条件。

摘要标签场景使用真实DOM及保留原生行为的getter计数：验证aria-label、alt、title、表单值与正文的优先级、空白高优先级标签、文本回退及UTF-16截断；已有标签或值时不预读完整正文。计数只覆盖元素标签读取，整页body文本仍正常提取，不能据此宣称全页摘要成本恒定或整体提速。

`browser_checkpoint_form_state.mjs` 验证混合控件的精确定位、值/选择/存储/滚动位置恢复，DOM变更后重新采集，以及大型匿名表单的有界兄弟扫描；使用真实BrowserSession、运行时池、Chromium和受控HTTP页面，Manager持久化为空操作。通过 `bun run test:matrix -- --suite=integration-browser-checkpoint-form-state` 运行，固定60秒/4GiB，原生临时目录归本测试正式runtime；不代表磁盘检查点或Gateway/主Web验收。

`browser_shutdown_operations.mjs` 经真实Manager创建、冻结/冷回收和交互入口，使用HTTP响应与动作屏障覆盖准备、执行和Session排空超时；核对关闭顺序、原页面终态、磁盘检查点与冷Manager重开后的表单值。通过 `bun run test:matrix -- --suite=integration-browser-shutdown-operations` 运行，固定60秒/4GiB；不代表Manager失败收尾、挂起动作终止、Gateway或主Web验收。

性能测试通过 `psutil` 遍历 Browser 后端的子进程，避免依赖 `/proc` 与 `sysconf`：CPU统一累计用户态和系统态秒数；内存 Linux 采样 PSS，Windows/macOS 采样 USS，结果同时记录指标名。服务关闭在 POSIX 发送 SIGTERM，在 Windows 使用独立进程组和 CTRL_BREAK_EVENT，以触发 Browser Manager 的检查点保存。

## 可修改内容

- 浏览器资源、性能、帧治理和失败恢复集成场景。

## 不可修改内容

- 不要把测试 HTTP 页面服务描述为真实外部 E2E 依赖。

## 规范

- 工作区、端口和产物按测试文件隔离到 `out/tests/integration/workspace_services/browser/`。
- 工具测试的进程内`browser_runtime`与后端进程必须共用`integration_startup_mount`提供的显式服务地址；仅创建专属端口的辅助进程，不会自动改变进程内容器的目标，不能依赖默认8012/8015恰好可用。
- 性能测试的页面HTTP服务在绑定后立即登记socket关闭；线程成功启动后再登记shutdown和join，避免启动失败时保留监听端口。停止、等待线程、关闭socket是独立释放动作，某一步失败或中断仍尝试后续动作，并保留原异常链。
