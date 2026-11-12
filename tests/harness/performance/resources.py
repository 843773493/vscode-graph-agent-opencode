"""受测进程外运行的低频资源采样；不接管超时和进程回收。"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import psutil


def snapshot(process: psutil.Process) -> dict[str, object]:
    with process.oneshot():
        cpu = process.cpu_times()
        values: dict[str, object] = {
            "pid": process.pid, "created_at": process.create_time(),
            "cpu_user_seconds": cpu.user, "cpu_system_seconds": cpu.system,
            "rss_bytes": process.memory_info().rss, "threads": process.num_threads(),
        }
        for name, getter in (("io", "io_counters"), ("handles", "num_handles" if os.name == "nt" else "num_fds")):
            try:
                value = getattr(process, getter)()
                values[name] = dict(value._asdict()) if name == "io" else value
            except (AttributeError, NotImplementedError):
                values[name] = {"status": "not_supported"}
            except psutil.AccessDenied:
                values[name] = {"status": "permission_denied"}
        return values


def sample_tree(root: psutil.Process, known: dict[tuple[int, float], psutil.Process], *, discover: bool) -> list[dict[str, object]]:
    if discover:
        try:
            for process in [root, *root.children(recursive=True)]:
                known[(process.pid, process.create_time())] = process
        except psutil.NoSuchProcess:
            pass
    samples = []
    for identity, process in tuple(known.items()):
        try:
            if not process.is_running():
                del known[identity]
                continue
            samples.append(snapshot(process))
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            del known[identity]
        except psutil.AccessDenied:
            samples.append({"pid": identity[0], "created_at": identity[1], "status": "permission_denied"})
    return samples


def run(pid: int, directory: Path, interval: float, limit: int, max_seconds: float) -> None:
    stop = directory / "stop-sampling"
    known: dict[tuple[int, float], psutil.Process] = {}
    latest: dict[tuple[int, float], dict[str, object]] = {}
    totals: dict[str, float] = {"cpu_user_seconds": 0, "cpu_system_seconds": 0}
    io_totals: dict[str, int] = {}
    peak, samples, dropped, written = 0, 0, 0, 0
    rss_observed, cpu_observed = False, False
    started = time.monotonic()
    root = None
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        pass
    next_discovery = 0.0
    discovery_interval = max(5.0, interval)
    with (directory / "resources.jsonl").open("x", encoding="utf-8", buffering=1) as stream:
        while root is not None:
            discover = time.monotonic() >= next_discovery
            values = sample_tree(root, known, discover=discover)
            if discover:
                next_discovery = time.monotonic() + discovery_interval
            samples += 1
            rss_values = [int(item["rss_bytes"]) for item in values if "rss_bytes" in item]
            rss = sum(rss_values) if rss_values else None
            if rss is not None:
                rss_observed = True
                peak = max(peak, rss)
            for value in values:
                identity = (int(value["pid"]), float(value["created_at"]))
                before = latest.get(identity, {})
                for metric in totals:
                    if metric in value:
                        cpu_observed = True
                        totals[metric] += max(0, float(value[metric]) - float(before.get(metric, value[metric])))
                io = value.get("io", {})
                if isinstance(io, dict) and "status" not in io:
                    previous_io = before.get("io", io)
                    for key, number in io.items():
                        io_totals[key] = io_totals.get(key, 0) + max(0, number - previous_io.get(key, number))
                latest[identity] = value
            current = {(int(value["pid"]), float(value["created_at"])) for value in values}
            latest = {identity: value for identity, value in latest.items() if identity in current}
            record = {"schema_version": 1, "event": "resource_sample", "monotonic_ns": time.perf_counter_ns(),
                      "scope": "test_process_tree", "root_pid": pid, "process_count": len(values),
                      "rss_sum_bytes": rss, "processes": values}
            line = json.dumps(record, separators=(",", ":")) + "\n"
            if written + len(line) <= limit:
                stream.write(line)
                written += len(line)
            else:
                dropped += 1
            if stop.exists() or not known:
                break
            if time.monotonic() - started > max_seconds:
                raise TimeoutError("资源采样未收到停止通知")
            deadline = time.monotonic() + interval
            while not stop.exists():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, .25))
    summary = {
        "schema_version": 1, "sample_count": samples, "sample_interval_ms": interval * 1000, "process_discovery_interval_ms": discovery_interval * 1000,
        "observed_peak_rss_sum_bytes": peak if rss_observed else None,
        "cpu_observed_deltas": totals if cpu_observed else None,
        "io_observed_deltas": io_totals if io_totals else {"status": "not_observed"},
        "last_process_samples": list(latest.values()), "dropped_samples": dropped,
        "status": "collected" if samples else "process_exited_before_sampling",
        "limitations": ["共享页可能重复计算", "两次后代发现之间的短进程和采样间尖峰可能未覆盖", "CPU增量从首个样本开始，非完整进程累计"],
        "collector": snapshot(psutil.Process()), "elapsed_seconds": time.monotonic() - started,
    }
    (directory / "resources-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--interval-ms", type=int, default=250)
    parser.add_argument("--limit-bytes", type=int, default=64 * 1024 * 1024)
    parser.add_argument("--max-seconds", type=float, required=True)
    args = parser.parse_args()
    run(args.pid, args.directory, args.interval_ms / 1000, args.limit_bytes, args.max_seconds)
