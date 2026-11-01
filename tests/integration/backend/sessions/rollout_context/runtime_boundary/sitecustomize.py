"""仅供隔离测试子进程启用；在应用导入前拒绝一次性 v1 导入链。"""

from __future__ import annotations

import json
import os
import sys
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

LEGACY_IMPORT_ROOT = "app.services.infrastructure.rollout_context.migration"
_OBSERVED = (
    "tests.integration.backend.sessions.rollout_context._history_storage_app",
    "app.services.infrastructure.rollout_context.checkpoint.saver",
    "app.services.infrastructure.rollout_context.storage.service",
    "app.services.mapping.itemized",
)
_AUDIT_ROOT = os.environ.get("BOXTEAM_TEST_V2_IMPORT_AUDIT")


def _record(event: str, module: str) -> None:
    assert _AUDIT_ROOT is not None
    with (Path(_AUDIT_ROOT) / f"{os.getpid()}.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, "module": module}, ensure_ascii=False) + "\n")


class V2RuntimeImports(MetaPathFinder):
    def find_spec(
        self, fullname: str, path: object = None, target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname == LEGACY_IMPORT_ROOT or fullname.startswith(LEGACY_IMPORT_ROOT + "."):
            # 先保存拒绝；即使调用方吞下 ImportError，父测试仍会发现这条旧链依赖。
            _record("blocked", fullname)
            raise ModuleNotFoundError(f"v2 runtime 禁止加载一次性导入链: {fullname}", name=fullname)
        if fullname in _OBSERVED:
            _record("observed", fullname)
        return None


if _AUDIT_ROOT is not None:
    assert not any(name == LEGACY_IMPORT_ROOT or name.startswith(LEGACY_IMPORT_ROOT + ".") for name in sys.modules)
    sys.meta_path.insert(0, V2RuntimeImports())
    _record("ready", "sitecustomize")
