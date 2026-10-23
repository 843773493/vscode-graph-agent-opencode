"""纯 URI grammar 的冷启动不得加载业务存储依赖。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_resource_uri_cold_import_keeps_catalog_and_domain_unloaded() -> None:
    result = subprocess.run(
        [sys.executable, "-c", """
import sys
from app.core.identifier import create_prefixed_id
from app.core.resource_uri import parse_session_artifact_uri, session_artifact_uri
uri = session_artifact_uri(
    "workspace_cold", create_prefixed_id("ses"), "attachments", "att_" + "a" * 32,
)
parsed = parse_session_artifact_uri(uri)
assert parsed.display_uri == uri
assert parsed.session_artifact_kind == "attachments"
assert not any(
    name.startswith("app.core.session_catalog_store") or name.startswith("app.domain.itemized")
    for name in sys.modules
)
"""],
        cwd=Path.cwd(), capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr
