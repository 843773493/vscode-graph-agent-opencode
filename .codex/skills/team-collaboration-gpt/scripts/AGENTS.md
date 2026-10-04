# 目录用途

保存 GPT 团队协作可重复使用的本地验证脚本。

# 可修改内容

仅维护本目录中的标准库 Python helper 及其必要参数、manifest 和错误处理。

# 不可修改内容

不得在此改写团队技能入口、协作记忆、业务源码或共享 Git 索引；脚本不得输出文件内容、环境变量值或密钥。

# 规范

所有 Git 子进程显式使用工具私有 `GIT_INDEX_FILE` 与 `GIT_OPTIONAL_LOCKS=0`。产物只能写入调用方分配的 `out/tests/temp/<task>/artifacts/`；索引只在同任务的 `git/` 中临时创建并清理。代码注释与诊断使用中文，脚本仅依赖 Python 标准库。
