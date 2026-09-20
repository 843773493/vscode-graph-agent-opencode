"""冻结 child thread 状态字段的 HTTP 与 Proto 公开契约。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import get_args

import pytest

from app.main import app
from app.schemas.internal_v2.session import ChildThreadStatus, ChildThreadSummaryDTO

PUBLIC_PROTO = Path.cwd() / "proto/boxteam/workspace/v2/public.proto"
SNAPSHOTS = (
    "src/clients/web/openapi.json",
    "src/clients/web/src/types/openapi/index.json",
)


@pytest.fixture(params=("live", *SNAPSHOTS))
def openapi_document(request: pytest.FixtureRequest) -> dict[str, object]:
    if request.param == "live":
        return app.openapi()
    return json.loads((Path.cwd() / request.param).read_text(encoding="utf-8"))


def test_child_thread_status_has_one_authoritative_openapi_field(
    openapi_document: dict[str, object],
) -> None:
    schemas = openapi_document["components"]["schemas"]  # type: ignore[index]
    schema = schemas["ChildThreadSummaryDTO"]  # type: ignore[index]

    assert "status" in schema["required"]  # type: ignore[index]
    assert schema["properties"]["status"]["enum"] == [  # type: ignore[index]
        "pending",
        "running",
        "failed",
    ]


@pytest.mark.parametrize("snapshot", SNAPSHOTS)
def test_child_thread_openapi_snapshots_match_routes(snapshot: str) -> None:
    document = json.loads((Path.cwd() / snapshot).read_text(encoding="utf-8"))
    assert document == app.openapi(), f"需运行 bun run gen:openapi 更新 {snapshot}"


def test_child_thread_status_proto_and_pydantic_fields_match() -> None:
    proto_source = PUBLIC_PROTO.read_text(encoding="utf-8")
    match = re.search(
        r"^message ChildThreadSummaryDTO \{\n(?P<body>.*?)^\}$",
        proto_source,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    fields = {
        field.group("name")
        for field in re.finditer(
            r"^\s*(?:optional\s+|repeated\s+)?[.\w<>]+\s+"
            r"(?P<name>[a-z][a-z0-9_]*)\s*=\s*\d+\s*;",
            match.group("body"),
            flags=re.MULTILINE,
        )
    }
    assert fields == set(ChildThreadSummaryDTO.model_fields)
    assert re.search(r"^\s*string status = 9;", match.group("body"), re.MULTILINE)
    assert get_args(ChildThreadStatus) == ("pending", "running", "failed")
