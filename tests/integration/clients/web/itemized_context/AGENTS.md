# 目录用途

真实 Web → Gateway → Workspace → Saver 的冻结上下文检查 Integration。

## 可修改内容

- 本目录 Python fixture、Playwright 驱动与跨投影断言。

## 不可修改内容

- 不替代页面或后端上下文 API，不操作用户真实会话，不注册仓库根目录。

## 规范

- 普通上下文展示场景复用 model_stream 精确回放，不另开模型 HTTP 服务；HTTP 403 等网络错误专项可保留本地 HTTP 服务，不能标为严格 E2E。
- 查询展示可经真实 Saver 准备历史，不能据此宣称完整执行、fork 或恢复由产品入口通过。
- `test_fork_prefix_recovery.py` 是较早回答截点的定向回归，不替代 `conversation_lifecycle/` 的 Section 8 总验收；当前文件的 fixture 管理独立 Gateway、受管 Backend、model_stream、工作区、端口和产物。真实 Composer 产生两轮工具历史，公开 fork 按第一轮 final item 创建 child，先刷新，再离开页面关闭 SSE；首次正常关闭之前，核对本次 Gateway 创建时间、Backend 祖先和测试命令行后强杀 Backend，经公开 Gateway start 冷恢复，再执行原安全重启。两次核真实 PID/generation 更换和精确历史，最后重开 child 后续聊；只读核原 canonical 来源和封存请求，排除后续工具轮次。通过独立矩阵入口运行，保留原60秒 fork watchdog，不修改公共 fixture 或长回归驱动。
- 工作区与产物镜像本测试 Python 文件路径；截图、下载和 trace 显式写入 artifacts。
- 启动前验证专用端口未占用，只清理本测试拥有的进程。
- 完整页面加载正式 Workbench 产物，按精确 workspace/session/thread 穿透 ShadowRoot；展开真实 manifest 折叠项后核对完整 selection，不把延迟物化正文当作空数据。
- 浏览器保留同步阶段与内存记录、操作 trace、截图和独立业务网络证据；Bun 1.3.11 重复采集完整工作台脚本响应会中止，trace 不采集 DOM/网络快照，不能因此删减分页、刷新和隔离断言。

- `test_model_call_source_freezing.py`只在自身隔离工作区使用既有AGENTS model_call覆盖；与默认Turn入口共用`verify_agents_source_boundary`和唯一浏览器驱动。真实Composer触发工具循环，在首请求封存后改AGENTS，核配对结果之后才追加新来源、同assembly其余binding的Turn边界、下一Turn与安全重启后API/UI/冷Saver原文及请求hash；不代替策略热更新、Skill变化或Section8总验收。

- `test_mixed_source_freezing.py` 使用自己的配置、Skill/AGENTS 文件、端口及正式产物目录，复用唯一来源冻结浏览器驱动；两次模型响应屏障内同时改真实来源，等待 watcher 的精确 URI/原始 SHA/available 发布后放行。两轮五次请求验证 Turn AGENTS 与 model_call Skill 独立冻结、原调用的三次 Skill 选择、metadata 语义不变而 lineage 更新；删除当前 Skill、改写 AGENTS 后，经公开安全重启检查完整 API/UI selection/provenance 及冷 Saver 正文/hash。不替代唯一 Section 8 总验收、策略热更新、Windows或真实 Provider。
- `test_policy_source_freezing.py`复用同一驱动，经真实Composer在首模型响应屏障内改工作区配置与AGENTS；由原watcher发布next_job后才放行，核原assembly不变、同Turn保留原策略、新Turn采用model_call。安全重启后逐项核完整API/UI来源与冷Saver策略、正文和request hash，不代替唯一Section8、Windows或外部Provider。
- 本场景先等待并通过原版按钮关闭Welcome，再执行唯一一次精确owner导航；product-006请求200但目标Tab缺席的启动导航时序仍留TODO，不把原因当作已证，也不代替初始化时序验收。新增五调用/五份UI整段400秒，原三调用仍240秒，业务内期限保持。
