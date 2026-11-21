"""Python SDK 与 Node SDK 使用同一日志记录样本。"""

import json
from pathlib import Path

import pytest
from boxteam_plugin_sdk.logging import RuntimeLogRecord, format_runtime_log_record
from boxteam_plugin_sdk.logging_contract import load_runtime_log_contract


@pytest.mark.parametrize("pretty", [True, False])
def test_python_and_js_share_exact_record_samples(pretty: bool) -> None:
    samples = json.loads(Path("resources/logging/record-samples.json").read_text())
    for sample in samples:
        assert format_runtime_log_record(
            RuntimeLogRecord(**sample["record"]), pretty=pretty,
            contract=load_runtime_log_contract(),
        ) == sample["pretty" if pretty else "compact"]
