## 1. 交互预算与真实场景

- [x] 1.1 明确总预算、20次暖读统计、首次读取和进程启动的计时边界，关联已有严格历史预算。
- [x] 1.2 增加真实 Workbench／Gateway／Backend 的目录导航、两个 Session 打开和128KiB文件打开场景，核 owner、内容、request_id、响应字节及 DOM。
- [x] 1.3 在现有 OpenSpec 与相关性能测试注释中提示：不在暖读无条件全量审计，helper计数／进程timeout不代替总延迟。

## 2. 验证与持续审查

- [x] 2.1 注册短定向矩阵入口，完成静态分析、正式构建和受保护真实浏览器验收，保留准确命令／指纹／原始输出／退出码；超预算先归因，不无证据重跑。
  - PERF-004 补验：真实 Gateway／A 后端及正式 Workbench，B 只在外部健康 HTTP 边界无响应。旧 `run-cc4c3836e55e` 的2570.53ms及全目标探测超预算原件保留；仅将会话初始化改为登记读取后，`run-e5670d78c7b5` 1 passed／84.63秒，打开732.85ms，原owner／request_id及Composer均正确；显式健康刷新仍耗时2118.27ms并报告B的offline／ReadTimeout。矩阵300秒／4GiB，未放宽2000ms预算；各一次对照，不代表真实远端、深历史或Electron。原 `PERF-004` 记录本片范围及原件。
  - 首轮 `run-472d73bce50c` 的首次会话5217.70ms超2000ms门，失败原件保留。配置检查与目录导入等相关链路减法后，以同一原期限／未预热场景补验：`run-1bd5f356f776` 1 passed／92.28秒、退出0；首次Composer820.52ms，六次640.41–825.29ms，128KiB文件六次289.71–632.78ms，导航p95 288.68ms，DOM 1134节点，页面错误及模型执行请求为空。准确命令／指纹见 `out/tests/temp/interactive_latency_current_20261105/artifacts/product-1793856490071999727.json`；不代表128Turn深历史、不可达远端或Windows通过。
- [x] 2.2 建立 docs/openspec-performance-review.md，记录实际发现和未验证范围。
- [ ] 2.3 通过正式自动化工具启用每小时检查；当前未提供创建工具，不能以文档或配置文件冒充调度成功。
