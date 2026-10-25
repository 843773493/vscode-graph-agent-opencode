"""核验 run_task 原始终态；工具成功不代表被测命令成功。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from run_task import process_group_members, write_json


def file_identity(path: Path) -> dict[str, object]:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"sha256": digest, "bytes": path.stat().st_size}


def sealed_product_summary(run: Path) -> dict[str, object] | None:
    """摘录封存驱动的阶段；报告场景键不等于需求整项验收。"""
    path = run / "product-artifacts/result.json"
    if not path.exists():
        return None
    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise TypeError(f"封存产品报告不是对象：{path}")
    summary: dict[str, object] = {
        "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
    }
    if "runningScenario" in payload:
        scenario = payload["runningScenario"]
        if scenario is not None and not isinstance(scenario, str):
            raise TypeError(f"产品报告 runningScenario 类型错误：{path}")
        summary["running_scenario"] = scenario
    if "section8" in payload:
        section = payload["section8"]
        if not isinstance(section, dict):
            raise TypeError(f"产品报告 section8 类型错误：{path}")
        summary["reported_section8_keys"] = list(section)
    if "historyLiveProofs" in payload:
        proofs = payload["historyLiveProofs"]
        if not isinstance(proofs, list):
            raise TypeError(f"产品报告 historyLiveProofs 类型错误：{path}")
        summary["history_live_proof_count"] = len(proofs)
    return summary


def verify_run(run: Path, runner: Path) -> dict[str, object]:
    result_path = run / "result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    before = json.loads((run / "source-before.json").read_text(encoding="utf-8"))
    after = json.loads((run / "source-after.json").read_text(encoding="utf-8"))
    checks = {
        "source_unchanged": before == after,
        "profile_matches": file_identity(Path(result["profile"]))["sha256"]
        == result["profile_sha256"],
        "runner_matches": file_identity(runner)["sha256"] == result["runner_sha256"],
    }
    logs = {}
    for name in ("stdout", "stderr"):
        recorded = result["logs"][name]
        path = Path(recorded["path"]).resolve()
        if path != (run / f"{name}.raw").resolve():
            raise ValueError(f"原始日志路径与 attempt 不一致：{path}")
        identity = file_identity(path)
        checks[f"{name}_matches"] = identity == {
            "sha256": recorded["sha256"], "bytes": recorded["bytes"],
        }
        logs[name] = {"path": str(path), **identity}
    command_exit = None
    actual_exit_code = None
    if "pgid" in result:
        command_exit_path = run / "command-exit.json"
        recorded_exit = json.loads(command_exit_path.read_text(encoding="utf-8"))
        checks["command_exited"] = recorded_exit["stage"] == "command_exited"
        exit_keys = ["pid", "pgid", "exit_code"]
        if "process_guard" in result:
            exit_keys.append("process_guard")
        checks["exit_records_match"] = all(
            recorded_exit[key] == result[key] for key in exit_keys
        )
        command_exit = {"path": str(command_exit_path), **file_identity(command_exit_path)}
        actual_exit_code = result["exit_code"]
        currently_observed_process_ids = process_group_members(result["pgid"])
        recorded_process_group_members = result["remaining_process_group_members"]
        checks["recorded_group_empty"] = not recorded_process_group_members
        checks["current_group_empty"] = not currently_observed_process_ids
    elif result.get("process_guard") == "windows_job_object":
        command_exit_path = run / "command-exit.json"
        recorded_exit = json.loads(command_exit_path.read_text(encoding="utf-8"))
        checks["command_exited"] = recorded_exit["stage"] == "command_exited"
        checks["exit_records_match"] = all(
            recorded_exit[key] == result[key]
            for key in ("pid", "process_guard", "exit_code")
        )
        command_exit = {"path": str(command_exit_path), **file_identity(command_exit_path)}
        actual_exit_code = result["exit_code"]

        process_tree_runner = Path(result["process_tree_runner"])
        checks["process_tree_runner_matches"] = (
            file_identity(process_tree_runner)["sha256"]
            == result["process_tree_runner_sha256"]
        )
        recorded_receipt = result.get("process_tree_receipt")
        if isinstance(recorded_receipt, dict):
            receipt_path = Path(recorded_receipt["path"]).resolve()
            expected_receipt_path = (run / "windows-job-receipt.json").resolve()
            if receipt_path != expected_receipt_path:
                raise ValueError(f"Windows Job receipt 路径与 attempt 不一致：{receipt_path}")
            receipt_identity = file_identity(receipt_path)
            receipt_payload = json.loads(receipt_path.read_text(encoding="utf-8"))
            checks["process_tree_receipt_matches"] = receipt_identity == {
                "sha256": recorded_receipt["sha256"],
                "bytes": recorded_receipt["bytes"],
            }
            checks["windows_job_empty"] = (
                receipt_payload
                == {"exit_code": result["exit_code"], "process_tree_empty": True}
            )
            windows_job_receipt = {
                "path": str(receipt_path), **receipt_identity,
                "process_tree_empty": receipt_payload.get("process_tree_empty"),
            }
        else:
            checks["process_tree_receipt_matches"] = False
            checks["windows_job_empty"] = False
            windows_job_receipt = None
    elif result.get("child_started") is False:
        checks["no_command_exit_before_launch"] = not (run / "command-exit.json").exists()
        actual_exit_code = None
    else:
        raise ValueError(f"终态缺少进程启动或回收事实：{result_path}")
    failures = [name for name, passed in checks.items() if not passed]
    if failures:
        raise ValueError(f"原件机械核验失败：{run}；{', '.join(failures)}")
    receipt: dict[str, object] = {
        "run": str(run), "result": str(result_path),
        "result_identity": file_identity(result_path),
        "recorded_exit_code": result["exit_code"],
        "actual_exit_code": actual_exit_code, "command_exit": command_exit,
        "exit_code_source": "runner_records",
        "duration_seconds": result["duration_seconds"],
        "source_count": len(before), "checks": checks, "logs": logs,
        "business_verified": False,
    }
    if "pgid" in result:
        receipt["recorded_process_group_members"] = recorded_process_group_members
        receipt["currently_observed_process_group_members"] = currently_observed_process_ids
    elif result.get("process_guard") == "windows_job_object":
        receipt["windows_job_receipt"] = windows_job_receipt
    product = sealed_product_summary(run)
    if product is not None:
        receipt["sealed_product_summary"] = product
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path.cwd().resolve()
    temporary_root = root / "out/tests/temp"
    output = args.output.resolve()
    if not output.is_relative_to(temporary_root) or "artifacts" not in output.relative_to(temporary_root).parts:
        raise ValueError("核验收据必须写入已分配的 out/tests/temp/.../artifacts/ 下")
    if output.exists():
        raise FileExistsError(f"核验收据不可覆盖：{output}")
    runner = root / ".codex/skills/team-collaboration-gpt/scripts/run_task.py"
    rows = []
    for run in args.run:
        resolved = run.resolve()
        if not resolved.is_relative_to(temporary_root):
            raise ValueError(f"attempt 必须位于已分配的 out/tests/temp/ 下：{resolved}")
        rows.append(verify_run(resolved, runner))
    write_json(output, {"mechanical_verification": True, "runs": rows, "business_verified": False})
    print(json.dumps({
        "receipt": str(output), "mechanical_verification": True,
        "actual_exit_codes": [row["actual_exit_code"] for row in rows],
        "recorded_exit_codes": [row["recorded_exit_code"] for row in rows],
        "business_verified": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
