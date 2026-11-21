# 目录用途

提供可独立安装的 Node 插件 SDK。

## 可修改内容

- SDK 包声明、公开入口、Node 运行库及其只读资源。

## 不可修改内容

- 不依赖仓库外的产品源码，不维护具体插件业务规则。
- 不手工修改 `src/generated/` 下的协议生成结果。

## 规范

- SDK 版本由根 `package.json` 在准备／构建时同步。仓库内产品开发使用 Bun workspace，插件和产品发行仍使用独立 tarball。
- 日志静态合同由 `prepare-product-sdk.mjs node` 从唯一资源源生成，`--check` 只核一致性；不手改包内生成资源。
- 公开子模块应能独立导入；模块加载不得启动服务、定时器或读取实例环境。
- 公开入口以 default 指向同一 ESM 文件，兼顾普通 import 和 Launcher 从应用根使用 Node resolver 定位后再导入；不维护第二套 CommonJS 实现。

- 手写 SDK 只维护 `src/*.ts`，不配套手写声明。`build.mjs` 默认用 esbuild 生成开发 JS，`--watch` 登记资源的完整目录树（包括空目录）并增量监听；`--check` 单独严格校验，`--release` 从同一个 TypeScript Program 生成 JS 与声明，再发布到空输出。构建工具使用 Node，运行仍使用目标 Node。
- workspace 的公开类型直读 TS，运行入口只读 `dist/*.js`；归档由构建入口改为指向生成声明，只包含 dist。根安装后脚本及原开发启动入口自动准备开发 JS；修改后沿原停止／启动流程生效，无须额外常驻协调服务，不将开发构建的产物误当作发行校验。
