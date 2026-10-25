# 目录用途

保存 GPT 团队协作可重复使用的本地验证和结构化读取脚本。

```text
scripts/
├── run_task.py     按固定 profile 运行命令，记录源码指纹、日志与平台进程清理事实
│   ├── POSIX       管理独立进程组并核对残留进程
│   └── Windows     调用 tests/support/processes/windows_job.py 管理 Job Object
└── verify_run.py   核验 runner、命令退出和平台进程树回收证据
```

平台进程管理实现归属 `tests/support/processes/`；本目录只负责任务运行与证据核验。`verify_run.py` 复查 POSIX 进程组状态，Windows 则核验 Job Object runner 指纹和回收回执。

# 可修改内容

仅维护本目录中的标准库 Python helper 及其必要参数、manifest 和错误处理。

# 不可修改内容

不得在此改写团队技能入口、协作记忆、业务源码或共享 Git 索引；核验脚本不得输出业务正文、环境变量值或密钥。结构化读取脚本仅按显式参数输出仓库源码或合同 JSON 的选中节点，不读取 `.boxteam/` 业务数据；运行配置只输出必要坐标或元数据。

# 规范

所有 Git 子进程显式使用工具私有 `GIT_INDEX_FILE` 与 `GIT_OPTIONAL_LOCKS=0`。产物只能写入调用方分配的 `out/tests/temp/<task>/artifacts/`；索引只在同任务的 `git/` 中临时创建并清理。代码注释与诊断使用中文，脚本仅依赖 Python 标准库。
