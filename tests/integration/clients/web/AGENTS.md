# 目录用途

存放纯 Web 组件/页面 harness，或浏览器前端、Gateway、后端和受控替身服务之间的集成测试。

## 可修改内容

- 浏览器性能、会话历史和网络交互集成测试。
- 真实浏览器加载局部组件 harness 的集成测试。
- 测试专用 Playwright 驱动脚本。

## 不可修改内容

- 没有受控服务替身的完整 Web 产品 E2E。

## 规范

- 完整产品页面场景必须通过真实 Gateway 访问；局部组件 harness 必须明确标识为 Integration。
- 服务替身和页面路由拦截必须显式记录，不能伪造成功状态。
- 运行产物必须写入 `out/tests/integration/clients/web/`。
