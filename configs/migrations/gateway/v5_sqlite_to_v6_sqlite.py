"""把完整 v5 冻结有效值转为独立 v6 根节点；不从当前文件补历史定义。"""

from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from app.gateway.config.loader import load_gateway_config
from app.gateway.config.values import GatewayConfig
from configs.installer import resolve_config_resource_source
from configs.maintenance.runtime_logging_migration import (
    LoggingMigrationInput,
    RuntimeLoggingMigrationPlan,
    require_preserved_values,
)
from configs.migrations.gateway.v5_jsonc_to_v6_jsonc import migrate_v5_jsonc_to_v6_jsonc
from configs.runtime import read_jsonc_object


def prepare_v6_snapshot(
    plan: RuntimeLoggingMigrationPlan, *, item: LoggingMigrationInput, project_root: Path | None,
) -> GatewayConfig:
    if item.config_version != 5 or not item.source_baseline:
        raise ValueError("Gateway 旧 active/pending 缺少可核验的逐层来源文档，保留原件")
    files = {source.key: source for source in plan.inputs if source.domain == "gateway" and source.kind == "file"}
    document = migrate_v5_jsonc_to_v6_jsonc(item.payload, source_key="gateway_local.jsonc", user_layer_present=True)
    # 旧快照只有有效值，显式冻结为根；不能将现在磁盘上的父链登记为历史原文。
    document.pop("extends")
    inline = resolve_config_resource_source("gateway_inline.jsonc", project_root=project_root)
    document["settings"]["network"] = read_jsonc_object(inline)["settings"]["network"]
    local = files["gateway_local.jsonc"].path
    config = load_gateway_config(
        config_path=files["gateway.jsonc"].path, local_config_path=local, entry_path=local,
        schema_path=resolve_config_resource_source("gateway_schema.jsonc", project_root=project_root),
        inline_config_path=inline, source_overrides={"gateway_local_mutable_override": document}, persist_migrations=False,
    )
    require_preserved_values({key: value for key, value in item.payload.items() if key not in {"$schema", "config_version"}},
                             config.payload)
    content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    detail = replace(config.source_details[0], presence="present", layer_digest=sha256(content).hexdigest())
    return replace(config, source_details=(detail,))
