"""复用协议 handler 的单实例进程，记录真实停止意图与非合作关闭。"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sqlite3
import sys
from pathlib import Path
from time import monotonic

import portalocker

from tests.integration.workspace_services.mcp.schema_server import serve


def main() -> None:
    lock, lifecycle, definitions, receipts, database = map(Path, sys.argv[1:])
    version = os.environ["MCP_TEST_VERSION"]

    def record(phase: str) -> None:
        effects = []
        if database.exists():
            with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
                for row in connection.execute("SELECT side_effects_json FROM config_apply_journal WHERE state = 'applying'"):
                    effects.extend(json.loads(row[0]))
        with lifecycle.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"phase": phase, "version": version, "pid": os.getpid(),
                                     "cwd": str(Path.cwd()), "time": monotonic(), "effects": effects}) + "\n")

    async def run() -> None:
        await serve(definitions, receipts)
        if os.environ.get("MCP_TEST_SHUTDOWN") == "noncooperative":
            record("eof")
            await asyncio.Event().wait()

    with portalocker.Lock(lock, timeout=0):
        if os.environ.get("MCP_TEST_SHUTDOWN") == "noncooperative":
            signal.signal(signal.SIGTERM, lambda *_: record("sigterm"))
        record("start")
        try:
            asyncio.run(run())
        finally:
            record("stop")


if __name__ == "__main__":
    main()
