"""将完整 v3 冻结有效值转为独立 v4 根节点，不用当前文件冒充历史来源。"""

from __future__ import annotations

import json
import sqlite3
from hashlib import sha256
from pathlib import Path

from app.core.config_sources import ConfigSource
from app.core.workspace_identity import read_workspace_id
from app.services.infrastructure.config.snapshot import (
    ConfigSnapshot,
    build_config_snapshot,
)
from app.services.infrastructure.config.state import (
    build_snapshot_secret_binding_summary,
    prepare_config_for_persistence,
    restore_environment_secret_references,
)
from app.services.infrastructure.config_service import ConfigService
from configs.inheritance.values import thaw
from configs.installer import resolve_config_resource_source
from configs.maintenance.runtime_logging_migration import (
    LoggingMigrationInput,
    RuntimeLoggingMigrationPlan,
    require_preserved_values,
)
from configs.migrations.workspace.v3_jsonc_to_v4_jsonc import (
    _options_settings,
    migrate_v3_jsonc_to_v4_jsonc,
)
from configs.runtime import read_jsonc_object, validate_config_payload


def prepare_v4_snapshot(
    plan: RuntimeLoggingMigrationPlan, *, item: LoggingMigrationInput, project_root: Path | None,
) -> tuple[ConfigService, ConfigSnapshot]:
    if not item.source_baseline:
        raise ValueError("Workspace v3 快照缺少可核验的来源身份，保留原件")
    user = next(source for source in plan.inputs if source.domain == "workspace" and source.kind == "file"
                and source.key == "workspace.jsonc")
    entry = next(source.path for source in plan.inputs if source.domain == "workspace" and source.kind == "file"
                 and source.key == "workspace_root_mutable_override"
                 and source.path.parent / "state/workspace.sqlite" == item.path)
    inline = resolve_config_resource_source("workspace_inline.jsonc", project_root=project_root)
    read_workspace_id(entry.parent.parent)
    service = ConfigService(config_path=user.path, workspace_root=entry.parent.parent, inline_config_path=inline)
    service._schema = read_jsonc_object(resolve_config_resource_source("workspace_schema.jsonc", project_root=project_root))
    before = restore_environment_secret_references(item.payload)
    document = migrate_v3_jsonc_to_v4_jsonc(before, source_key="workspace_root_mutable_override")
    # v3 只冻结了完整有效值，没有保存各层原文。转换后的快照明确成为独立根，
    # 不伪造原来的父链；当前可编辑文件仍按各自真实层转换，启动后沿正常流程采用。
    document.pop("extends")
    common = read_jsonc_object(inline)["settings"]
    document["settings"].setdefault("network", common["network"])
    document["settings"].setdefault("model_options", common["model_options"])
    uri = service._source_uri_for_path(entry)
    graph = service._build_source_graph(source_documents={uri: document})
    settings = thaw(graph.resolve_scope().settings)
    config = {**settings["workspace"], "network": settings["network"], "config_version": 4}
    validate_config_payload(config, schema=service._schema)
    service._llm_resolution.list_models(graph)
    require_preserved_values({key: value for key, value in before.items() if key not in {"$schema", "config_version", "llm"}}, config)
    for provider_id, provider in before["llm"]["providers"].items():
        require_preserved_values(_options_settings(provider["defaults"]),
                                 thaw(graph.resolve_provider(provider_id).settings), f"provider/{provider_id}")
        for model_id, model in provider["models"].items():
            require_preserved_values({key: value for key, value in model.items() if key != "overrides"},
                                     config["llm"]["providers"][provider_id]["models"][model_id])
            require_preserved_values(_options_settings(model.get("overrides", {})),
                                     thaw(graph.resolve_model(provider_id, model_id).settings), f"model/{provider_id}/{model_id}")
    content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    detail = ConfigSource(uri=uri, layer="workspace", precedence=0, loaded=True,
                          source_key="workspace_root_mutable_override", presence="present",
                          layer_digest=sha256(content).hexdigest())
    return service, build_config_snapshot(config, graph=graph, source_paths=(entry,), source_details=(detail,),
                                          origins=service._graph_origins(graph))


def migrate_v3_sqlite_to_v4_sqlite(
    connection: sqlite3.Connection, *, source_path: Path, plan: RuntimeLoggingMigrationPlan,
    project_root: Path | None,
) -> None:
    active = next((item for item in plan.inputs if item.path == source_path and item.kind == "active"), None)
    if active is None:
        return
    service, snapshot = prepare_v4_snapshot(plan, item=active, project_root=project_root)
    baseline, _, revisions, digests = service._source_baseline(snapshot)
    allocated = connection.execute("SELECT next_revision FROM config_revision_meta WHERE config_domain='workspace'").fetchone()
    revision = max(active.revision + 1, allocated[0] if allocated else 1)
    payload = prepare_config_for_persistence(snapshot.to_dict())
    connection.execute(
        "UPDATE config_active_snapshot SET active_revision=?,payload_json=?,source_baseline_json=?,"
        "layer_revisions_json=?,layer_digests_json=?,effective_digest=?,secret_bindings_json=?,schema_version=4 "
        "WHERE config_domain='workspace'",
        (revision, _dump(payload), _dump(baseline), _dump(revisions), _dump(digests), snapshot.revision,
         _dump(build_snapshot_secret_binding_summary(payload, baseline))),
    )
    connection.execute(
        "INSERT INTO config_revision_meta(config_domain,next_revision) VALUES ('workspace',?) "
        "ON CONFLICT(config_domain) DO UPDATE SET next_revision=excluded.next_revision", (revision + 1,),
    )
    for item in plan.inputs:
        if item.path != source_path or item.kind != "pending":
            continue
        owner, candidate = prepare_v4_snapshot(plan, item=item, project_root=project_root)
        baseline, _, _, _ = owner._source_baseline(candidate)
        payload = prepare_config_for_persistence(candidate.to_dict())
        connection.execute(
            "UPDATE config_pending_candidate SET payload_json=?,source_baseline_json=?,candidate_digest=?,"
            "effective_digest=?,secret_bindings_json=?,base_active_revision=? WHERE config_domain='workspace' AND candidate_id=?",
            (_dump(payload), _dump(baseline), candidate.revision, candidate.revision,
             _dump(build_snapshot_secret_binding_summary(payload, baseline)), revision, item.key),
        )


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
