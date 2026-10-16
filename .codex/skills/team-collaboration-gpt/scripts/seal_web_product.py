"""封存正式 Web 产品原件；保留业务退出码，不把封存成功当作业务通过。"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path

from verify_run import verify_run

PRODUCT_FILES = (
    ("artifacts/basic-chat-tool-loop-result.json", "result.json"),
    ("artifacts/basic-chat-tool-loop-browser.log", "browser.log"),
    ("artifacts/basic-chat-tool-loop-browser.stderr.log", "browser.stderr.log"),
    ("artifacts/basic-chat-tool-loop-phases.log", "phases.log"),
    ("runtime/test-control/events.json", "events.json"),
    ("artifacts/basic-chat-tool-loop-build.stdout.log", "build.stdout.log"),
    ("artifacts/basic-chat-tool-loop-build.stderr.log", "build.stderr.log"),
    ("artifacts/basic-chat-tool-loop-failure.png", "failure.png"),
)


def seal_web_product(root: Path, run: Path, product_root: Path) -> dict[str, object]:
    """从已终结 attempt 的时间窗口取原字节，全部核对后才创建封存目录。"""
    root, run, product_root = root.resolve(), run.resolve(), product_root.resolve()
    temporary_root = root / "out/tests/temp"
    if not run.is_relative_to(temporary_root) or "artifacts" not in run.relative_to(temporary_root).parts:
        raise ValueError("attempt 必须位于已分配的 out/tests/temp/.../artifacts/ 下")
    if not product_root.is_relative_to(root / "out/tests"):
        raise ValueError("产品原件只能来自本仓库 out/tests/ 下的正式输出或诊断 fixture")
    destination = run / "product-artifacts"
    if destination.exists():
        raise FileExistsError(f"封存目录不可覆盖：{destination}")
    runner = root / ".codex/skills/team-collaboration-gpt/scripts/run_task.py"
    verification = verify_run(run, runner)
    if verification["actual_exit_code"] is None:
        raise ValueError("业务命令未启动，不能封存为产品运行产物")
    result_bytes = (run / "result.json").read_bytes()
    result = json.loads(result_bytes)
    started = datetime.fromisoformat(result["started_utc"]).timestamp()
    ended = datetime.fromisoformat(result["ended_utc"]).timestamp()
    selected: list[tuple[str, bytes]] = []
    identities: list[dict[str, object]] = []
    omitted: list[dict[str, str]] = []
    for relative_source, name in PRODUCT_FILES:
        source = (product_root / relative_source).resolve()
        if not source.is_relative_to(product_root):
            raise ValueError(f"原件路径越出所属产品目录：{source}")
        # 成功运行不生成失败截图；以前运行的截图也不能归给本次 attempt。
        optional = name == "failure.png"
        if not source.is_file():
            if optional:
                omitted.append({"name": name, "reason": "本次未生成"})
                continue
            raise FileNotFoundError(f"产品原件缺失：{source}")
        before = source.stat()
        if not started <= before.st_mtime <= ended:
            if optional:
                omitted.append({"name": name, "reason": "修改时间不属于本次 attempt"})
                continue
            raise ValueError(f"产品原件修改时间不属于本次 attempt：{source}")
        content = source.read_bytes()
        after = source.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns
        ):
            raise RuntimeError(f"读取时产品原件仍在变化：{source}")
        selected.append((name, content))
        identities.append({
            "name": name, "source": str(source), "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        })
    destination.mkdir()
    for name, content in selected:
        with (destination / name).open("xb") as stream:
            stream.write(content)
        if (destination / name).read_bytes() != content:
            raise RuntimeError(f"封存字节不一致：{destination / name}")
    manifest: dict[str, object] = {
        "run": str(run), "product_root": str(product_root),
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "actual_exit_code": verification["actual_exit_code"],
        "business_verified": False, "files": identities, "omitted": omitted,
    }
    with (destination / "seal-manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--product-root", type=Path)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    product_root = args.product_root or root / "out/tests/e2e/clients/web/test_basic_chat_tool_loop"
    manifest = seal_web_product(root, args.run, product_root)
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__":
    main()
