"""流式读取正式性能证据；供终端和后续 Agent 使用同一份摘要。"""
from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path


def distribution(values: list[float]) -> dict[str, object]:
    ordered = sorted(values)
    return {"sample_count": len(values), "algorithm": "nearest-rank",
            **{name: ordered[max(0, math.ceil(len(ordered) * ratio) - 1)] if ordered else None
               for name, ratio in (("p50_ms", .5), ("p95_ms", .95), ("max_ms", 1))},
            "tail_confidence": "descriptive_only" if len(values) < 20 else "finite_sample"}


def summarize(directory: Path) -> dict[str, object]:
    receipt_path = directory / "run.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {}
    links = directory / "links"
    files = [Path.cwd() / json.loads(link.read_text(encoding="utf-8"))["path"] for link in sorted(links.glob("*.json"))] if links.exists() else sorted(directory.glob("performance-*.jsonl"))
    groups: dict[str, dict[str, object]] = {}
    outcomes: Counter[str] = Counter()
    test_nodes: set[str] = set()
    incomplete, metrics = [], []
    dropped, partial = 0, 0
    for file in files:
        opened: dict[str, dict[str, object]] = {}
        with file.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.endswith("\n"):
                    partial += 1
                    continue
                record = json.loads(line)
                if record.get("schema_version") != 1:
                    raise ValueError(f"未知性能记录版本: {file}")
                event = record.get("event")
                if record.get("test_id"):
                    test_nodes.add(record["test_id"])
                if event in {"metric", "diagnostic", "session_start", "session_end"}:
                    if len(metrics) < 2048:
                        metrics.append(record)
                    else:
                        dropped += 1
                if event == "closed":
                    dropped += record["dropped_records"]
                elif event == "test_report":
                    outcomes[f"{record['stage']}:{record['status']}"] += 1
                elif event == "start":
                    opened[record["stage_id"]] = record
                elif event == "end":
                    opened.pop(record["stage_id"], None)
                    workload = record.get("workload", {})
                    key = json.dumps([record["stage"], workload, record["status"]], sort_keys=True, ensure_ascii=False)
                    if key not in groups and len(groups) >= 2048:
                        dropped += 1
                        continue
                    group = groups.setdefault(key, {"stage": record["stage"], "workload": workload,
                                                   "status": record["status"], "elapsed_ms": [], "observations": [], "count": 0, "max_ms": 0, "failed": 0})
                    elapsed = record["elapsed_ns"] / 1e6
                    group["count"] += 1
                    group["max_ms"] = max(group["max_ms"], elapsed)
                    group["failed"] += record["status"] != "passed"
                    if len(group["elapsed_ms"]) < 1024:
                        group["elapsed_ms"].append(elapsed)
                    if len(group["observations"]) < 128:
                        group["observations"].append(record)
        incomplete.extend({"stage": value["stage"], "test_id": value["test_id"], "source": str(file)} for value in opened.values())
    stages = []
    for group in groups.values():
        stats = distribution(group["elapsed_ms"])
        stats["observed_max_ms"] = group["max_ms"]
        rates = [row["returned_items"] / (row["elapsed_ns"] / 1e9) for row in group["observations"]
                 if row.get("returned_items") is not None and row["elapsed_ns"] > 0]
        stages.append({**group, "distribution": stats, "returned_items_per_second": rates,
                       "samples_truncated": group["count"] > len(group["elapsed_ms"])})
    for metric in metrics:
        if metric["event"] == "session_start":
            receipt.setdefault("environment", {}).update({key: metric[key] for key in ("python_version", "pytest_version") if key in metric})
    resources_path = directory / "resources-summary.json"
    resources = {"status": "not_enabled"}
    if resources_path.exists():
        resources = json.loads(resources_path.read_text(encoding="utf-8"))
    elif (directory / "resources.jsonl").exists():
        observed_peak, count = None, 0
        with (directory / "resources.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                if line.endswith("\n"):
                    value = json.loads(line).get("rss_sum_bytes")
                    if value is not None:
                        observed_peak = max(observed_peak or 0, value)
                    count += 1
        resources = {"status": "sampling_in_progress" if "test_exit_code" not in receipt else "incomplete",
                     "sample_count": count, "observed_peak_rss_sum_bytes": observed_peak}
    summary = {
        "schema_version": 1, "run": receipt, "record_files": [str(file) for file in files],
        "test_nodes": sorted(test_nodes),
        "test_outcomes": dict(outcomes), "stages": sorted(stages, key=lambda item: item["max_ms"], reverse=True),
        "incomplete_stages": incomplete, "metrics": metrics, "dropped_records": dropped, "partial_lines": partial,
        "resources": resources,
        "semantics": "阶段包含子阶段，不可直接累加；CPU/IO为进程共享作用域；分位数仅对已保留样本计算",
    }
    tool = receipt.get("collection", {}).get("tool")
    summary["diagnostic_status"] = ("collected" if any(item["event"] == "diagnostic" for item in metrics) else "not_observed") if tool else "not_requested"
    if tool and summary["diagnostic_status"] == "not_observed" and receipt.get("status") == "passed":
        receipt["infrastructure_status"] = "failed"
        receipt["diagnostic_error"] = f"请求的工具 {tool} 未在所选场景运行；请选择支持该工具的场景"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (directory / "performance-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def compare(before: dict[str, object], after: dict[str, object]) -> dict[str, object]:
    if before.get("schema_version") != 1 or after.get("schema_version") != 1:
        raise ValueError("只支持当前性能报告 schema_version=1，不自动迁移历史格式")
    differences = []
    left, right = before.get("run", {}), after.get("run", {})
    for field in ("suite_id", "collection"):
        if left.get(field) != right.get(field):
            differences.append(field)
    if before.get("test_nodes") != after.get("test_nodes"):
        differences.append("test_population")
    for field in ("platform", "arch", "bun", "python_version", "pytest_version", "cpu", "cpu_count", "total_memory_bytes"):
        if left.get("environment", {}).get(field) != right.get("environment", {}).get(field):
            differences.append(f"environment:{field}")
    for label, report in (("baseline", before), ("candidate", after)):
        if report.get("dropped_records") or report.get("partial_lines") or report.get("incomplete_stages") or report.get("run", {}).get("mixed_source"):
            differences.append(f"{label}:incomplete_or_mixed_source")
        if any(item.get("samples_truncated") for item in report["stages"]):
            differences.append(f"{label}:truncated_stage_samples")
    key = lambda item: (item["stage"], json.dumps(item["workload"], sort_keys=True), item.get("status"))
    index = {key(item): item for item in before["stages"]}
    if set(index) != {key(item) for item in after["stages"]}:
        differences.append("stage_workload_or_outcomes")
    changes = []
    for candidate in after["stages"]:
        baseline = index.get(key(candidate))
        if baseline is None:
            differences.append(f"workload:{candidate['stage']}")
            continue
        old, new = baseline["distribution"]["p50_ms"], candidate["distribution"]["p50_ms"]
        changes.append({"stage": candidate["stage"], "workload": candidate["workload"],
                        "baseline": baseline["distribution"], "candidate": candidate["distribution"],
                        "delta_ms": new - old, "relative_delta": (new - old) / old if old else None})
    return {"schema_version": 1, "comparable": not differences, "limitations": sorted(set(differences)),
            "changes": changes, "verdict": "observation_only_no_automatic_budget_or_baseline_update"}


def print_summary(summary: dict[str, object]) -> None:
    run, resources = summary.get("run", {}), summary["resources"]
    print(json.dumps({"suite": run.get("suite_id"), "status": run.get("status"),
                      "elapsed_seconds": run.get("elapsed_seconds"), "resources": resources.get("status"),
                      "observed_peak_rss_sum_bytes": resources.get("observed_peak_rss_sum_bytes"),
                      "cpu_observed_deltas": resources.get("cpu_observed_deltas"),
                      "test_outcomes": summary["test_outcomes"], "incomplete_stages": len(summary["incomplete_stages"])}, ensure_ascii=False))
    for group in summary["stages"][:12]:
        details = {key: value for key, value in (group.get("observations") or [{}])[0].items()
                   if key in {"input_tokens", "output_tokens", "usage_status", "first_content_ns", "chunks", "returned_count", "returned_items"}}
        print(json.dumps({"stage": group["stage"], "workload": group["workload"], "count": group["count"],
                          **group["distribution"], **details}, ensure_ascii=False))
    for unfinished in summary["incomplete_stages"][:12]:
        print(json.dumps({"incomplete": unfinished}, ensure_ascii=False))
    metric_groups = {}
    for metric in summary.get("metrics", []):
        if metric["event"] in {"metric", "diagnostic"}:
            metric_groups.setdefault(metric["stage"], []).append(metric)
    for name, values in metric_groups.items():
        print(json.dumps({"metric": name, "observations": len(values), "latest": {
            key: value for key, value in values[-1].items()
            if key not in {"schema_version", "run_id", "suite_id", "event", "clock", "monotonic_ns", "worker_id", "workload", "stage"}
        }}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("summarize", "show"):
        command = sub.add_parser(action)
        command.add_argument("path", type=Path)
        command.add_argument("--json", action="store_true")
        command.add_argument("--stage", help="只显示阶段名或HTTP端点包含指定文本的阶段/指标")
    command = sub.add_parser("compare")
    command.add_argument("baseline", type=Path)
    command.add_argument("candidate", type=Path)
    args = parser.parse_args()
    if args.action == "compare":
        print(json.dumps(compare(json.loads(args.baseline.read_text(encoding="utf-8")), json.loads(args.candidate.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2))
    else:
        summary = json.loads(args.path.read_text(encoding="utf-8")) if args.path.is_file() else summarize(args.path)
        if args.stage:
            matches = lambda item: args.stage in item["stage"] or args.stage in item.get("workload", {}).get("path", "")
            summary = {**summary, "stages": [item for item in summary["stages"] if matches(item)],
                       "metrics": [item for item in summary.get("metrics", []) if matches(item)],
                       "incomplete_stages": [item for item in summary["incomplete_stages"] if matches(item)]}
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print_summary(summary)
        if args.action == "summarize" and summary.get("run", {}).get("diagnostic_error"):
            raise SystemExit(summary["run"]["diagnostic_error"])


if __name__ == "__main__":
    main()
