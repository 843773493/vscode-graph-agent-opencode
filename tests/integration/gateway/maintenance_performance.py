"""独立采集固定三批真实活动清理与 Gateway 分页延迟。"""
from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx


async def collect(url: str, workspace: Path, workspace_id: str, session_id: str, output: Path) -> None:
    db = workspace / ".boxteam/state/workspace.sqlite"
    samples: list[dict[str, object]] = []
    cleanups: list[dict[str, object]] = []
    cold: dict[str, object] = {}
    headers = {"X-Local-Token": "local-dev-token", "X-BoxTeam-Workspace-Id": workspace_id}
    async with httpx.AsyncClient(base_url=url, headers=headers, trust_env=False, timeout=10) as client:
        for batch in range(3):
            now = datetime.now(UTC)
            prefix = f"maintenance-{uuid4().hex}"
            expired = (now - timedelta(days=40)).isoformat()
            with sqlite3.connect(db) as connection:
                connection.executemany("INSERT INTO workspace_activity(event_id,session_id,status,summary,occurred_at) VALUES (?,?,?,?,?)",
                    [(f"{prefix}-expired-{index}", session_id, "completed", "expired", expired) for index in range(10_000)])
                cursor = connection.execute("SELECT MAX(event_seq) FROM workspace_activity").fetchone()[0]
                expected = [f"{prefix}-valid-{index}" for index in range(100)]
                connection.executemany("INSERT INTO workspace_activity(event_id,session_id,status,summary,occurred_at) VALUES (?,?,?,?,?)",
                    [(event, session_id, "completed", "retained", now.isoformat()) for event in expected])
            async def request(index: int, *, cursor: int = cursor, batch: int = batch) -> dict[str, object]:
                started = time.perf_counter()
                response = await client.get("/api/v1/session-catalog/events", params={"after": cursor, "limit": 100})
                ended = time.perf_counter()
                result = {"batch": batch, "index": index, "started": started, "ended": ended,
                          "elapsed_ms": (ended - started) * 1000, "status": response.status_code,
                          "bytes": len(response.content), "request_id": response.headers.get("X-Request-ID"), "body": response.json()}
                return result
            if batch == 0:
                cold = await request(-1)
            # 同一事件循环轮次提交真实清理和20个请求；不延迟或占锁制造重叠。
            async def prune(*, batch: int = batch) -> dict[str, object]:
                response = await client.post("/api/v1/__tests/maintenance-prune")
                response.raise_for_status()
                return {"batch": batch, **response.json()}
            cleanup, *responses = await asyncio.gather(prune(), *(request(index) for index in range(20)))
            cleanups.append(cleanup)
            samples.extend(responses)
            # 先保留全部原件再判断，失败与慢样本不被后续统计过滤。
            output.write_text(json.dumps({"cold": cold, "samples": samples, "cleanups": cleanups}, ensure_ascii=False, indent=2))
            assert cleanup["deleted"] == 10_000, cleanup
            for sample in ([cold] if batch == 0 else []) + responses:
                assert sample["status"] == 200, sample
                body = sample["body"]
                assert sample["request_id"] and body["request_id"] == sample["request_id"], sample
                assert [item["event_id"] for item in body["data"]["items"]] == expected, sample
            with sqlite3.connect(db) as connection:
                assert connection.execute("SELECT COUNT(*) FROM workspace_activity WHERE event_id LIKE ?", (prefix + "-valid-%",)).fetchone()[0] == 100
        durations = sorted(float(sample["elapsed_ms"]) for sample in samples)
        overlap = [sample for sample in samples if any(item["batch"] == sample["batch"] and sample["started"] < item["ended"] and sample["ended"] > item["started"] for item in cleanups)]
        report = {"cold": cold, "samples": samples, "cleanups": cleanups, "p95_ms": durations[math.ceil(len(durations) * .95) - 1],
                  "max_ms": durations[-1], "overlap_count": len(overlap), "overlap_samples": overlap}
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        assert len(samples) == 60
        assert report["p95_ms"] <= 500 and report["max_ms"] <= 1500, report
        assert cold["elapsed_ms"] <= 3000, cold


if __name__ == "__main__":
    asyncio.run(collect(sys.argv[1], Path(sys.argv[2]), sys.argv[3], sys.argv[4], Path(sys.argv[5])))
