"""一次性 GraphBinding 重绑 runner（remove-agent-memory 7.3.2 显式 operator 入口）。

删除 ``StructuredMemoryMiddleware`` slot 并把 ``DEEP_AGENT_GRAPH_REVISION`` 由 1
bump 到 2 后，既有 workspace 的持久 binding 仍是 ``(deep-agent, 1,
sha256:78309db966399b28c023e27b9dd2265f76dc079bfbb93125273b744997cd8e25)``，
在 ``resolve_or_persist_graph_binding`` 处会 fail-closed 抛
``GraphBindingUnavailableError```（不回退到最新图）。本命令把这个历史 selector
一次性重绑到当前 ``DEEP_AGENT_GRAPH_BINDING``，让该 workspace 的历史会话恢复
可执行。

这是面向运维的显式一次性维护命令：必须在升级到 bump 后代码、且该 workspace
已 quiesce（无执行中的 run）时单人执行。

用法::

    uv run python scripts/rebind_graph_binding.py --workspace-root /abs/path/to/workspace

行为：

- ``--workspace-root`` 必填，指向工作区根目录（其下
  ``.boxteam/graph-bindings/graph-bindings.json`` 为 store 文档）；
- 只重绑 selector **逐字段等于**上面那个历史 selector 的 owner；磁盘上任何
  其它值（例如已被重绑过、或来自别的 graph family）都会显式失败并中止，
  绝不按"当前最新图"盲目覆盖；
- 幂等：磁盘已是当前 selector 的 owner 视为 no-op，重复执行安全；
- 成功打印每个 owner 的处置（``rebound``/``already-current``）与退出码 0；
- 失败打印明确错误到 stderr 并以非零退出码结束，绝不静默、不重试。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 该文件作为 workspace root 下的一次性运维命令直接运行。直接运行时
# Python 会把 scripts/ 放在 sys.path[0]；按项目约定从当前工作目录取得
# 显式仓库根，禁止通过文件位置向上猜测根目录。
if __package__ in {None, ""}:
    _repo_root = Path.cwd()
    if not (_repo_root / "app").is_dir():
        raise RuntimeError(
            "rebind_graph_binding 必须从项目根目录运行，且当前目录缺少 app/"
        )
    sys.path.insert(0, str(_repo_root))

from app.agents.graph_binding import (
    DEEP_AGENT_GRAPH_BINDING,
    DEEP_AGENT_GRAPH_ID,
    GraphBinding,
    JsonFileGraphBindingStore,
)

# 本次变更要淘汰的历史 selector（revision 1 + 含 memory slot 的 schema hash）。
# 逐字段硬编码而不是从参数传入：本命令只负责这**一次**已发生的骨架变更，
# 不提供任意 selector 的通用改写能力，避免变成可被滥用的静默覆盖入口。
_HISTORICAL_BINDING = GraphBinding(
    graph_id=DEEP_AGENT_GRAPH_ID,
    graph_revision=1,
    graph_schema_hash=(
        "sha256:78309db966399b28c023e27b9dd2265f76dc079bfbb93125273b744997cd8e25"
    ),
    capability_profile_hash=(
        "sha256:a04ebd2bedd40857bae49a153e00f0979d70fb582e4e0e976d15cfcc77620ca1"
    ),
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "把历史 GraphBinding selector（revision 1 + memory slot hash）"
            "一次性重绑到当前 deep-agent binding（remove-agent-memory 7.3.2）"
        )
    )
    parser.add_argument(
        "--workspace-root",
        required=True,
        type=Path,
        help="工作区根目录绝对或相对路径（其下 .boxteam/ 为业务数据根）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    workspace_root = args.workspace_root.expanduser().resolve()
    if not workspace_root.is_dir():
        raise SystemExit(f"workspace-root 不存在或不是目录: {workspace_root}")
    store = JsonFileGraphBindingStore(
        directory=workspace_root / ".boxteam" / "graph-bindings"
    )
    try:
        owners = store.persisted_owners()
    except Exception as error:  # noqa: BLE001 —— CLI 边界：任何失败都打印完整错误（含类型与详情）并以非零退出，绝不静默吞掉；不重试、不降级。
        print(
            f"读取 GraphBinding store 失败（未做任何改写）: "
            f"{type(error).__name__}: {error}",
            file=sys.stderr,
        )
        return 1
    if not owners:
        print(
            f"没有已持久化的 GraphBinding，无需重绑: workspace_root={workspace_root}"
        )
        return 0
    outcomes: list[tuple[str, str, str]] = []
    for owner in owners:
        try:
            rebound = store.rebind_graph_binding(
                owner,
                expected=_HISTORICAL_BINDING,
                new=DEEP_AGENT_GRAPH_BINDING,
            )
        except Exception as error:  # noqa: BLE001 —— CLI 边界：同上。
            print(
                f"GraphBinding 重绑失败（已改写的 owner 保持新值，其余原样）: "
                f"owner=({owner.session_id!r}, {owner.thread_id!r}), "
                f"{type(error).__name__}: {error}",
                file=sys.stderr,
            )
            return 1
        outcomes.append(
            (
                owner.session_id,
                owner.thread_id,
                "rebound" if rebound else "already-current",
            )
        )
    print(f"GraphBinding 重绑完成: workspace_root={workspace_root}")
    print(f"  当前 selector: {DEEP_AGENT_GRAPH_BINDING!r}")
    for session_id, thread_id, outcome in outcomes:
        print(f"  - ({session_id!r}, {thread_id!r}): {outcome}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

