"""独立测试进程共用可开关的真实日志 I/O 故障与原错观察。"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from traceback import format_exception


def install_failing_diagnostic_sink(
    *, logger_names: tuple[str, ...], control: Path, destination: Path, audit: Path,
) -> None:
    class Sink(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if not control.is_file():
                return
            primary = record.exc_info[1] if record.exc_info is not None else None
            with audit.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({
                    "logger": record.name, "message": record.getMessage(),
                    "error_type": type(primary).__name__ if primary is not None else None,
                    "diagnostic": "".join(format_exception(primary)) if primary is not None else None,
                }, ensure_ascii=False) + "\n")
            # 调用方将普通文件用作父目录，由目标 OS 抛出真实日志位置错误。
            with destination.open("a", encoding="utf-8"):
                pass

    handler = Sink(level=logging.ERROR)
    for name in logger_names:
        logging.getLogger(name).addHandler(handler)
