# 目录用途

存放 Browser Manager 使用测试页面服务、受控网络条件或其他替身边界的集成测试。

```text
browser/
├── test_browser_custom_tools.py        代理工具与Browser Manager接线
├── test_browser_resource_performance.py 资源治理、帧传输与性能场景
├── browser_element_references.mjs       真实DOM中的元素引用与动作
└── browser_checkpoint_form_state.mjs    表单检查点捕获、释放与恢复
```

`browser_element_references.mjs` 直接使用真实Chromium验证页面摘要、坐标检查与按ref点击的组合，覆盖动态新增、移除、重排、文档版本变化，以及摘要达到元素上限后停止尾部可见性检查；通过 `bun run test:matrix -- --suite=integration-browser-element-references` 运行，固定60秒/4GiB。它不启动Gateway或主Web，也不属于服务端纯单元测试矩阵；Chromium临时文件写入本测试正式输出的runtime目录。

坐标检查同时核CSSOM在一次检查内只整理一次、嵌套与继承规则仍可读、下一次能看到规则更新，以及跨域规则不可读时仍返回可读来源与最终样式；计数断言限定重复解析成本，不以机器耗时作为通过条件。

`browser_checkpoint_form_state.mjs` 验证混合控件的精确定位、值/选择/存储/滚动位置恢复，DOM变更后重新采集，以及大型匿名表单的有界兄弟扫描；使用真实BrowserSession、运行时池、Chromium和受控HTTP页面，Manager持久化为空操作。通过 `bun run test:matrix -- --suite=integration-browser-checkpoint-form-state` 运行，固定60秒/4GiB，原生临时目录归本测试正式runtime；不代表磁盘检查点或Gateway/主Web验收。

## 可修改内容

- 浏览器资源、性能、帧治理和失败恢复集成场景。

## 不可修改内容

- 不要把测试 HTTP 页面服务描述为真实外部 E2E 依赖。

## 规范

- 工作区、端口和产物按测试文件隔离到 `out/tests/integration/workspace_services/browser/`。
