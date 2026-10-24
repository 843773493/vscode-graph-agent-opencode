"""产品附件与终态 fork 经离线转换保全身份、原件及冻结历史。"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.resource_uri import session_artifact_uri
from app.services.infrastructure.attachment_blob_catalog.catalog import (
    AttachmentBlobCatalog,
)
from app.services.infrastructure.attachment_blob_catalog.directory_schema import (
    require_directory_schema,
)
from app.services.infrastructure.attachment_blob_catalog.legacy_references import (
    resolve_legacy_attachment,
    verify_legacy_references,
)
from app.services.infrastructure.attachment_blob_catalog.locator import (
    resolve_attachment_directory,
)
from app.services.infrastructure.attachment_blob_catalog.migration import (
    backup,
    catalog,
    directories,
)
from app.services.infrastructure.attachment_blob_catalog.migration.preflight import (
    file_digest,
)
from tests.integration.backend.sessions.test_attachment_offline_backup import (
    BODY,
    BackupCase,
    cli,
)
from tests.integration.backend.sessions.test_attachment_offline_directories import (
    source_rows,
)


def prepare_directories(case: BackupCase) -> Path:
    backup.prepare_backup(case.workspace, case.output)
    directories.convert_backup(case.output)
    return case.output / "converted/catalog.sqlite"


def frozen_files(root: Path) -> dict[str, tuple[str, int]]:
    return {path.relative_to(root).as_posix(): (file_digest(path), path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


async def test_cli_prepares_independent_resources_and_complete_legacy_bindings(shared_blob_case: BackupCase) -> None:
    case = shared_blob_case
    owners, blobs = source_rows(case)
    path = prepare_directories(case)
    source = frozen_files(case.output / "data")
    result = cli("prepare-catalog", "--backup", str(case.output))
    assert result.returncode == 0, result.stderr
    response = json.loads(result.stdout)
    assert response == {"state": "catalog_prepared", "backup_id": json.loads((case.output / "manifest.json").read_bytes())["backup_id"],
                        "workspace_id": json.loads((case.output / "manifest.json").read_bytes())["workspace_id"],
                        "source_attachment_schema": 3, "target_attachment_schema": 4, "resources": 2, "legacy_fork_payloads": 0}
    assert str(case.workspace) not in result.stdout and str(case.output) not in result.stdout
    verified = cli("verify-catalog", "--backup", str(case.output))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout) == response
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        require_directory_schema(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        for owner in owners:
            resource = connection.execute("SELECT * FROM attachment_directories WHERE attachment_id=?", (owner["attachment_id"],)).fetchone()
            assert resource["availability"] == "available" and resource["unattached_since"] is None and resource["gc_after"] is None
            original = case.output / "converted/attachments" / resource["relative_locator"] / "original/content"
            assert original.read_bytes() == BODY
            version = connection.execute("SELECT * FROM attachment_directory_files WHERE attachment_id=?", (owner["attachment_id"],)).fetchone()
            assert (version["relative_path"], version["digest"], version["length"], version["file_name"]) == (
                "original/content", blobs[0]["digest"], len(BODY), owner["file_name"],
            )
            association = connection.execute("SELECT * FROM attachment_directory_associations WHERE attachment_id=?", (owner["attachment_id"],)).fetchone()
            assert (association["session_id"], association["thread_id"], association["state"]) == (
                owner["owner_session_id"], owner["owner_thread_id"], "active",
            )
            assert association["binding_token"] == "migration:" + response["backup_id"] + ":" + owner["owner_ref_id"]
            provenance = connection.execute("SELECT * FROM attachment_legacy_bindings WHERE attachment_id=?", (owner["attachment_id"],)).fetchone()
            assert json.loads(provenance["source_owner_json"]) == owner
            assert json.loads(provenance["source_blob_json"]) == blobs[0]
            file_id = session_artifact_uri(response["workspace_id"], owner["owner_session_id"], "attachments", owner["attachment_id"])
            resolved = resolve_legacy_attachment(
                connection, session_id=owner["owner_session_id"], thread_id=owner["owner_thread_id"],
                file_id=file_id, workspace_id=response["workspace_id"],
            )
            assert resolved is not None and resolved.attachment_id == owner["attachment_id"]
            assert resolved.availability == "available" and (resolved.digest, resolved.length) == (blobs[0]["digest"], len(BODY))
            assert (resolve_attachment_directory(path.parent / "attachments", resolved.relative_locator) / "original/content").read_bytes() == BODY
            other, = [candidate for candidate in owners if candidate != owner]
            assert resolve_legacy_attachment(
                connection, session_id=owner["owner_session_id"], thread_id=other["owner_thread_id"],
                file_id=file_id, workspace_id=response["workspace_id"],
            ) is None
            gateway = "gateway_6c8b2f9317a5a2c6c4569958b1e23d84"
            qualified = session_artifact_uri(response["workspace_id"], owner["owner_session_id"], "attachments", owner["attachment_id"], gateway_authority=gateway)
            assert resolve_legacy_attachment(
                connection, session_id=owner["owner_session_id"], thread_id=owner["owner_thread_id"],
                file_id=qualified, workspace_id=response["workspace_id"],
            ) is None
            assert resolve_legacy_attachment(
                connection, session_id=owner["owner_session_id"], thread_id=owner["owner_thread_id"],
                file_id=qualified, workspace_id=response["workspace_id"], gateway_id=gateway,
            ) == resolved
        assert connection.execute("SELECT count(DISTINCT relative_locator) FROM attachment_directories").fetchone()[0] == 2
    complete = frozen_files(case.output / "converted")
    assert catalog.prepare_converted_catalog(case.output) == response
    assert catalog.verify_converted_catalog(case.output) == response
    assert frozen_files(case.output / "converted") == complete
    assert frozen_files(case.output / "data") == source
    with closing(sqlite3.connect(case.workspace / ".boxteam/attachments/catalog.sqlite")) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    # 该隔离目标尚未提供线上切换，旧 owner 必须拒绝把新结构当 schema 3 使用。
    with pytest.raises(RuntimeError), closing(AttachmentBlobCatalog(path)) as online:
        online.connection.execute("SELECT 1")


@pytest.mark.parametrize("state", ["released", "tombstoned", "preview"])
async def test_versions_and_retention_survive_catalog_conversion(
    legacy_case_factory, state: str,
) -> None:
    case = legacy_case_factory("preview" if state == "preview" else "queued")
    preview = base64.b64decode(case.evidence["preview_base64"]) if state == "preview" else None
    released = datetime(2020, 1, 2, tzinfo=UTC)
    until = released + timedelta(days=90)
    if state != "preview":
        # 仅调整迁移输入的保留事实，不替代真实 unload/GC 验收。
        with closing(sqlite3.connect(case.workspace / ".boxteam/attachments/catalog.sqlite")) as connection:
            connection.execute("UPDATE attachment_owner_refs SET state='released', updated_at=?, retention_until=?",
                               (released.isoformat(), until.isoformat()))
            if state == "tombstoned":
                connection.execute("UPDATE attachment_blobs SET availability='tombstoned'")
            connection.commit()
        if state == "tombstoned":
            case.original.unlink()
    path = prepare_directories(case)
    catalog.prepare_converted_catalog(case.output)
    assert catalog.verify_converted_catalog(case.output)["state"] == "catalog_prepared"
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        binding = connection.execute("SELECT * FROM attachment_legacy_bindings ORDER BY attachment_id DESC LIMIT 1").fetchone()
        workspace_id = connection.execute("SELECT workspace_id FROM attachment_directory_metadata").fetchone()[0]
        resolved = resolve_legacy_attachment(
            connection, session_id=binding["source_session_id"], thread_id=binding["source_thread_id"],
            file_id=session_artifact_uri(workspace_id, binding["source_session_id"], "attachments", binding["attachment_id"]),
            workspace_id=workspace_id,
        )
        assert resolved is not None
        assert resolved.availability == ("tombstoned" if state == "tombstoned" else "available")
        if state == "preview":
            digest, length, locator = connection.execute(
                "SELECT f.digest,f.length,d.relative_locator FROM attachment_directory_files f "
                "JOIN attachment_directories d USING(attachment_id) WHERE f.role='preview'",
            ).fetchone()
            assert preview is not None and (digest, length) == ("sha256:" + hashlib.sha256(preview).hexdigest(), len(preview))
            assert (case.output / "converted/attachments" / locator / "preview/512.webp").read_bytes() == preview
        else:
            availability, start, deadline, retention, locator = connection.execute(
                "SELECT availability,unattached_since,gc_after,retention_until,relative_locator FROM attachment_directories",
            ).fetchone()
            assert (start, deadline, retention) == (released.isoformat(), until.isoformat(), until.isoformat())
            assert availability == ("tombstoned" if state == "tombstoned" else "available")
            assert tuple(connection.execute("SELECT state FROM attachment_directory_associations").fetchone()) == ("released",)
            assert tuple(connection.execute("SELECT length FROM attachment_directory_files WHERE role='original'").fetchone()) == (len(BODY),)
            assert (case.output / "converted/attachments" / locator).exists() == (state == "released")


@pytest.mark.parametrize("window", ["partial", "renamed"])
async def test_interrupted_preparation_resumes_without_rewriting_published_catalog(
    backup_case: BackupCase, monkeypatch: pytest.MonkeyPatch, window: str,
) -> None:
    case = backup_case
    path = prepare_directories(case)
    source = frozen_files(case.output / "data")
    with monkeypatch.context() as failure:
        if window == "partial":
            def interrupt_write(target: Path, _rows: dict[str, list[tuple[object, ...]]]) -> None:
                target.write_bytes(b"interrupted private target")
                raise OSError("受控写库中断")
            failure.setattr(catalog, "_write_catalog", interrupt_write)
        else:
            write = catalog.atomic_write_bytes

            def interrupt_publish(target: Path, payload: bytes) -> None:
                if json.loads(payload).get("state") == "catalog_prepared":
                    raise OSError("受控发布中断")
                write(target, payload)
            failure.setattr(catalog, "atomic_write_bytes", interrupt_publish)
        with pytest.raises(OSError, match="受控"):
            catalog.prepare_converted_catalog(case.output)
    assert json.loads((path.parent / "catalog-journal.json").read_bytes())["state"] == "preparing"
    with pytest.raises(RuntimeError, match="尚未完成"):
        catalog.verify_converted_catalog(case.output)
    published = (file_digest(path), path.stat().st_mtime_ns) if path.exists() else None
    assert catalog.prepare_converted_catalog(case.output)["state"] == "catalog_prepared"
    catalog.verify_converted_catalog(case.output)
    if published is not None:
        assert (file_digest(path), path.stat().st_mtime_ns) == published
    assert not path.with_name("catalog.sqlite.partial").exists()
    assert frozen_files(case.output / "data") == source


@pytest.mark.parametrize("damage", ["source", "directory", "catalog", "schema", "rows", "journal"])
async def test_mismatched_inputs_or_catalog_are_rejected_without_repair(backup_case: BackupCase, damage: str) -> None:
    case = backup_case
    path = prepare_directories(case)
    catalog.prepare_converted_catalog(case.output)
    if damage == "source":
        (case.output / "data" / case.original.relative_to(case.workspace / ".boxteam")).write_bytes(b"damaged source")
    elif damage == "directory":
        original, = (path.parent / "attachments").rglob("content")
        original.write_bytes(b"damaged converted original")
    elif damage == "journal":
        journal = path.parent / "catalog-journal.json"
        payload = json.loads(journal.read_bytes())
        payload["backup_id"] = "unknown-source"
        journal.write_text(json.dumps(payload))
    else:
        if damage == "catalog":
            path.write_bytes(b"invalid sqlite file")
        else:
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("DROP INDEX attachment_directories_gc" if damage == "schema" else
                                   "UPDATE attachment_directory_associations SET binding_token='different-binding'")
                connection.commit()
            # 即使攻击者同时更新字节指纹，结构和来源逐行校验仍须拒绝。
            journal = path.parent / "catalog-journal.json"
            payload = json.loads(journal.read_bytes())
            payload["catalog_sha256"] = file_digest(path)
            journal.write_text(json.dumps(payload))
    before = frozen_files(case.output)
    for operation in (catalog.verify_converted_catalog, catalog.prepare_converted_catalog):
        with pytest.raises(RuntimeError):
            operation(case.output)
    assert frozen_files(case.output) == before


async def test_product_execution_and_two_forks_preserve_frozen_payloads_and_capture_hashes(
    legacy_case_factory,
) -> None:
    case = legacy_case_factory("forks")
    keys = case.evidence["fork_keys"]
    original_payloads = [(kind, metadata, raw) for kind, metadata, raw in case.evidence["fork_payloads"]]
    assert original_payloads and {row[1]["copy_operation_id"] for row in original_payloads} == set(keys)
    assert all(row[1]["state"] == "committed" for row in original_payloads if row[0] == "claim")
    captures = case.evidence["captures"]
    assert all(capture is not None for capture in captures)
    capture_root = case.workspace / ".boxteam/navigation/fork-source-captures"
    frozen_captures = frozen_files(capture_root)
    assert frozen_captures
    assert captures[0]["state"] == captures[1]["state"] == "materialized"
    assert (capture_root / captures[0]["snapshot_id"]).is_dir()
    assert not (capture_root / captures[1]["snapshot_id"]).exists()
    output = case.output
    backup.prepare_backup(case.workspace, output)
    directories.convert_backup(output)
    response = catalog.prepare_converted_catalog(output)
    assert response["legacy_fork_payloads"] == len(original_payloads)
    assert catalog.verify_converted_catalog(output) == response
    assert backup.verify_backup(output)["state"] == "prepared"
    copied_captures = frozen_files(output / "data/navigation/fork-source-captures")
    assert {name: digest for name, (digest, _) in copied_captures.items()} == {
        name: digest for name, (digest, _) in frozen_captures.items()
    }
    with closing(sqlite3.connect(output / "converted/catalog.sqlite")) as target:
        target.row_factory = sqlite3.Row
        actual = [(kind, json.loads(metadata), raw) for kind, metadata, raw in target.execute(
            "SELECT record_kind,metadata_json,snapshot_json FROM attachment_legacy_fork_payloads",
        )]
        assert sorted(actual, key=repr) == sorted(original_payloads, key=repr)
        for kind, metadata, raw in actual:
            if kind == "claim":
                preimage = metadata["copy_operation_id"] + metadata["source_snapshot_id"] + metadata["operation_preimage_hash"] + raw
                assert hashlib.sha256(preimage.encode()).hexdigest() == metadata["preimage_hash"]
        # 关键历史由产品生成；新解析仅依赖新 catalog 和新目录，来源与备份正文均可离线。
        originals = [case.workspace / ".boxteam/attachments", output / "data/attachments"]
        for original in originals:
            original.rename(original.with_name("attachments.offline"))
        try:
            for kind, metadata, raw in actual:
                if kind != "mapping":
                    continue
                frozen = json.loads(raw)
                resolved = resolve_legacy_attachment(
                    target, session_id=metadata["target_session_id"], thread_id=metadata["target_thread_id"],
                    file_id=metadata["source_file_id"], workspace_id=response["workspace_id"],
                )
                assert resolved is not None and resolved.attachment_id == frozen["target_attachment_id"]
                assert (resolved.digest, resolved.length, resolved.availability) == (frozen["digest"], len(BODY), "available")
                assert (resolve_attachment_directory(output / "converted/attachments", resolved.relative_locator) / "original/content").read_bytes() == BODY
                assert resolve_legacy_attachment(
                    target, session_id=metadata["target_session_id"], thread_id=metadata["target_thread_id"] + "-unknown",
                    file_id=metadata["source_file_id"], workspace_id=response["workspace_id"],
                ) is None
            verify_legacy_references(target)
        finally:
            for original in originals:
                original.with_name("attachments.offline").rename(original)
        # 局部损坏验证可复用产品生成的历史；不替产品构造执行或 fork 的关键状态。
        for damage in ("version", "owner", "target_uri", "claim_hash", "claim_state", "missing_claim", "alias", "ambiguous"):
            with closing(sqlite3.connect(":memory:")) as broken:
                target.backup(broken)
                broken.row_factory = sqlite3.Row
                broken.execute("DROP TRIGGER attachment_legacy_fork_payloads_no_update")
                broken.execute("DROP TRIGGER attachment_legacy_fork_payloads_no_delete")
                mapping = broken.execute("SELECT * FROM attachment_legacy_fork_payloads WHERE record_kind='mapping' LIMIT 1").fetchone()
                metadata, frozen = json.loads(mapping["metadata_json"]), json.loads(mapping["snapshot_json"])
                if damage == "version":
                    with pytest.raises(sqlite3.IntegrityError, match="versions are immutable"):
                        broken.execute("UPDATE attachment_directory_files SET length=length+1 WHERE attachment_id=?", (frozen["target_attachment_id"],))
                    broken.execute("DROP TRIGGER attachment_directory_files_no_update")
                    broken.execute("UPDATE attachment_directory_files SET length=length+1 WHERE attachment_id=?", (frozen["target_attachment_id"],))
                elif damage in {"owner", "target_uri"}:
                    if damage == "owner":
                        frozen["target_owner_ref_id"] += "-wrong"
                    else:
                        frozen["target_file_id"] = frozen["target_file_id"].replace(frozen["target_attachment_id"], "att_" + "f" * 32)
                    broken.execute("UPDATE attachment_legacy_fork_payloads SET snapshot_json=? WHERE record_kind='mapping' AND record_key=?", (json.dumps(frozen), mapping["record_key"]))
                    if damage == "target_uri":
                        # 同步更新 claim 的合法 hash，确保反例真正验证 URI/owner 关系。
                        claim = broken.execute("SELECT * FROM attachment_legacy_fork_payloads WHERE record_kind='claim' AND record_key=?", (frozen["claim_id"],)).fetchone()
                        claim_metadata = json.loads(claim["metadata_json"])
                        raw = json.dumps(frozen)
                        preimage = claim_metadata["copy_operation_id"] + claim_metadata["source_snapshot_id"] + claim_metadata["operation_preimage_hash"] + raw
                        claim_metadata["preimage_hash"] = hashlib.sha256(preimage.encode()).hexdigest()
                        broken.execute("UPDATE attachment_legacy_fork_payloads SET metadata_json=?,snapshot_json=? WHERE record_kind='claim' AND record_key=?", (json.dumps(claim_metadata), raw, frozen["claim_id"]))
                elif damage in {"claim_hash", "claim_state", "missing_claim"}:
                    claim = broken.execute("SELECT * FROM attachment_legacy_fork_payloads WHERE record_kind='claim' AND record_key=?", (frozen["claim_id"],)).fetchone()
                    assert claim is not None
                    if damage == "missing_claim":
                        broken.execute("DELETE FROM attachment_legacy_fork_payloads WHERE record_kind='claim' AND record_key=?", (claim["record_key"],))
                    else:
                        claim_metadata = json.loads(claim["metadata_json"])
                        claim_metadata["preimage_hash" if damage == "claim_hash" else "state"] = "wrong" if damage == "claim_hash" else "aborted"
                        broken.execute("UPDATE attachment_legacy_fork_payloads SET metadata_json=? WHERE record_kind='claim' AND record_key=?", (json.dumps(claim_metadata), claim["record_key"]))
                elif damage == "alias":
                    frozen["source_file_id"] = "different-source"
                    frozen["source_file_aliases"] = []
                    frozen["target_file_id"] = "different-target"
                    broken.execute("UPDATE attachment_legacy_fork_payloads SET snapshot_json=? WHERE record_kind='mapping' AND record_key=?", (json.dumps(frozen), mapping["record_key"]))
                else:
                    broken.execute("INSERT INTO attachment_legacy_fork_payloads VALUES ('mapping',?,?,?,?)",
                                   (mapping["record_key"] + "duplicate", mapping["copy_operation_id"], mapping["metadata_json"], mapping["snapshot_json"]))
                with pytest.raises(RuntimeError):
                    resolve_legacy_attachment(
                        broken, session_id=metadata["target_session_id"], thread_id=metadata["target_thread_id"],
                        file_id=metadata["source_file_id"], workspace_id=response["workspace_id"],
                    )
        # 来源机器首次保存日期可以不同；保持 owner/version 后不能拿历史 locator 读目标。
        with closing(sqlite3.connect(":memory:")) as remote:
            target.backup(remote)
            remote.row_factory = sqlite3.Row
            remote.execute("DROP TRIGGER attachment_legacy_fork_payloads_no_update")
            for row in remote.execute("SELECT * FROM attachment_legacy_fork_payloads").fetchall():
                frozen, metadata = json.loads(row["snapshot_json"]), json.loads(row["metadata_json"])
                frozen["relative_locator"] = "2000/01/02/" + frozen["blob_id"]
                raw = json.dumps(frozen)
                if row["record_kind"] == "claim":
                    preimage = metadata["copy_operation_id"] + metadata["source_snapshot_id"] + metadata["operation_preimage_hash"] + raw
                    metadata["preimage_hash"] = hashlib.sha256(preimage.encode()).hexdigest()
                remote.execute("UPDATE attachment_legacy_fork_payloads SET metadata_json=?,snapshot_json=? WHERE record_kind=? AND record_key=?",
                               (json.dumps(metadata), raw, row["record_kind"], row["record_key"]))
            verify_legacy_references(remote)
        for table in ("attachment_legacy_bindings", "attachment_legacy_fork_payloads"):
            for operation in (f"UPDATE {table} SET attachment_id=attachment_id" if table.endswith("bindings") else
                              f"UPDATE {table} SET snapshot_json=snapshot_json", f"DELETE FROM {table}"):
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    target.execute(operation)
                target.rollback()
    # capture 所在的原 catalog 整体逐字保全，来源 hash 不经新结构重算或改写。
    with closing(sqlite3.connect(output / "data/navigation/session-catalog.sqlite")) as source_db:
        source_db.row_factory = sqlite3.Row
        for capture in captures:
            assert capture is not None
            row = source_db.execute("SELECT * FROM fork_source_captures WHERE copy_operation_id=?", (capture["copy_operation_id"],)).fetchone()
            assert row is not None and row["preimage_hash"] == capture["preimage_hash"]
