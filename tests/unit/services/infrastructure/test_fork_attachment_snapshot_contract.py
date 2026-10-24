"""产品生成的旧 fork 载荷逐字冻结，运行态扩展不能改变历史 hash。"""

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from app.services.infrastructure.attachment_blob_catalog.records import (
    ForkAttachmentSnapshot,
)
from app.services.infrastructure.rollout_context.fork.source_snapshot import (
    ForkRolloutSourceSnapshot,
    ForkSourceSnapshot,
)


@pytest.fixture
def historical_fork():
    return json.loads(
        (Path.cwd() / "tests/fixtures/itemized/fork_attachment_v2.json").read_text(
            encoding="utf-8",
        ),
    )


@pytest.fixture
def source_snapshot(historical_fork, tmp_path: Path) -> ForkSourceSnapshot:
    # 此处只验证封存载荷合同；执行、两次 fork 与恢复另由产品集成覆盖。
    return ForkSourceSnapshot(
        rollout=ForkRolloutSourceSnapshot(
            source_session_id=historical_fork["attachments"][0]["source_session_id"],
            source_thread_id=historical_fork["attachments"][0]["source_thread_id"],
            captured_root=tmp_path,
            files=(),
            manifest_bytes=b"",
            manifest_sha256=historical_fork["rollout_manifest_sha256"],
            control_sha256=historical_fork["control_sha256"],
            source_lifecycle_generation=1,
        ),
        attachments=tuple(
            ForkAttachmentSnapshot(**payload)
            for payload in historical_fork["attachments"]
        ),
    )


@pytest.mark.parametrize("claim_index", [0, 1], ids=["first-fork", "deep-fork"])
def test_v2_claim_bytes_and_preimage_are_unchanged(historical_fork, claim_index: int) -> None:
    claim = historical_fork["claims"][claim_index]
    snapshot = ForkAttachmentSnapshot(**json.loads(claim["snapshot_json"]))
    encoded = json.dumps(
        snapshot.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    assert encoded == claim["snapshot_json"]
    assert hashlib.sha256((claim["preimage_prefix"] + encoded).encode("utf-8")).hexdigest() == claim["preimage_sha256"]


def test_v2_source_snapshot_hash_is_unchanged(historical_fork, source_snapshot: ForkSourceSnapshot) -> None:
    assert source_snapshot.snapshot_sha256 == historical_fork["source_snapshot_sha256"]


def test_runtime_field_does_not_extend_frozen_v2_payload(historical_fork, source_snapshot: ForkSourceSnapshot) -> None:
    @dataclass(frozen=True, slots=True)
    class RuntimeSnapshot(ForkAttachmentSnapshot):
        runtime_note: str = "不属于封存合同"

    extended = RuntimeSnapshot(**historical_fork["attachments"][0])
    assert extended.to_payload() == historical_fork["attachments"][0]
    assert replace(source_snapshot, attachments=(extended,)).snapshot_sha256 == historical_fork["source_snapshot_sha256"]


def test_frozen_attachment_change_changes_source_hash(source_snapshot: ForkSourceSnapshot) -> None:
    changed = replace(source_snapshot.attachments[0], file_name="另一份原件.txt")
    assert replace(source_snapshot, attachments=(changed,)).snapshot_sha256 != source_snapshot.snapshot_sha256
