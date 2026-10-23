# 目录用途

`scripts/install/` 存放本地发布包安装和运行时安装后处理入口。

安装命令测试区分“目标发布平台”和“运行主机路径”：前者决定平台包名及npm/npm.cmd，项目根、tarball和prefix的真实路径仍遵循宿主`node:path`规则。纯命令测试使用正式输出镜像路径下的中文、空格路径作为输入，不创建安装目录；通过注入spawn结果检查命令，不执行安装或打包。Windows路径边界模拟只证明路径和命令接线，不代表Windows原生安装验收。

```text
安装入口
├── install-release.mjs       选择本地发布包并调用安装命令
│   ├── 源码指纹与构建metadata  先核平台、版本、源码和归档文件名
│   ├── 归档摘要               仅对仍可能复用的缓存或重建结果读取正文
│   └── install-release-cache.test.mjs  真实复用入口、重建与失败边界
└── runtime-postinstall.mjs   安装包内部的runtime取得与校验
    ├── 本地文件复制 / 远端HTTP下载  取得同一暂存归档
    ├── sha256File                共用摘要校验，失败不解包
    └── tar.x                    解包后核对runtime manifest
```

远端正文通过Node标准库pipeline写入文件，由流owner处理短写、背压和文件关闭；下载入口结束时取消其fetch信号，HTTP拒绝和文件打开失败也不能留下未消费的响应。总下载预算仍为既有15分钟，不是fork业务期限，也不是测试等待预算。

安装结束后只尝试清理本次暂存归档。单一安装或清理异常原样交付，两者同时失败时按安装、清理的顺序聚合并保留安装`cause`；清理失败不能盖掉原安装原因，也不能把已解包的文件描述为已回滚。

本地发行包复用先核构建结果的平台、版本、源码指纹和归档文件名；已明确过期时直接进入既有重建流程，不读取旧归档正文。仍可能复用的缓存和新构建结果必须继续验证两个归档的完整摘要，必要校验的IO错误原样暴露；源码在构建期间变化或重建metadata仍不匹配时拒绝交付，不因此跳过摘要保证。

`install-release-cache.test.mjs`在`out/tests/scripts/install/install-release-cache/workspace/<run_id>/<case_id>/`创建最小完整源码输入，独立Node调用真实`ensureCurrentLocalPackage`及指纹/摘要owner；仅重建子进程为替身。测试记录摘要流读取顺序与字节，覆盖两目标平台的有效缓存、过期metadata、损坏摘要及失败重建，不执行实际打包、安装、Git操作或原生Windows验收。

`runtime-postinstall.test.mjs`的Node子进程只在`out/tests/scripts/install/runtime-postinstall/workspace/<run_id>/<scenario>/`内运行；原始stdout/stderr和精简结果保留在同镜像根`artifacts/<run_id>/<scenario>/`。每个运行ID与场景目录独占创建，运行原件不覆盖、不清理。通过VM执行原入口全文、本地随机端口HTTP及真实文件和摘要，文件边界可注入短写/错误；tar是明确的替身，只验证进入解包前的归档与参数，不证明真实解包或安装成功。fixture自身阶段上限1秒，Node子进程上限5秒，并由外层测试runner保护；不安装依赖、不访问远端发布服务、不写用户安装目录。当前根项目不含下载器的tar依赖，不能为这些边界测试临时安装它。

文件边界替身只覆盖生产入口实际使用的流式写入及最终删除，不保留旧的手写FileHandle写入分支；清理失败场景核暂存归档仍存在、原原因和尝试次数。生产暂存归档仍按安装入口规则清理；测试fixture目录及子进程原始结果则保留，便于失败定位。

# 可修改内容

- 可以维护本地 runtime tarball 校验和安装后处理脚本。
- 可以维护检查源码指纹、必要时触发本地打包并安装 tarball 的入口脚本；版本必须从唯一发布版本源读取。
- 可以维护安装脚本所需的明确错误处理和环境变量读取。

# 不可修改内容

- 不在这里实现 Agent、Gateway 或工作区业务逻辑。
- 不把下载后的 runtime、node_modules 或其他构建产物提交到仓库。

# 规范

- 安装脚本必须校验构建输入指纹、本地 tarball 的 SHA-256 和最终安装文件，失败时直接终止；不得从公共 npm registry 获取 BoxTeam 包。
- 脚本应从当前工作目录或显式传入路径解析安装目标，不猜测仓库根目录。
- 发布打包入口位于 `scripts/release/`，构建实现位于 `packaging/runtime/`。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
