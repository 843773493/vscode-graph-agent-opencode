"""只在隔离页面测试中删除已搜索的原记录，不模拟产品留存入口。"""

import json
import sys
from pathlib import Path

from app.core.path_utils import workspace_context
from app.core.workspace_identity import read_workspace_id
from app.schemas.internal_v2.llm_request_log import LLMRequestLogReferenceDTO
from app.schemas.internal_v2.trace_search import TraceReferenceDTO
from app.services.infrastructure.llm_request_log_service import LLMRequestLogService
from app.services.infrastructure.trace_event_store import TraceEventStore
from tests.support.session_catalog_owner import fixture_catalog_scope


def delete_record(category: str, workspace: Path, serialized_reference: str, backup: Path) -> dict[str, object]:
    sessions = workspace / ".boxteam" / "sessions"
    with fixture_catalog_scope(), workspace_context(read_workspace_id(workspace), workspace):
        if category == "request":
            request = LLMRequestLogReferenceDTO.model_validate_json(serialized_reference)
            owner = LLMRequestLogService(sessions)
            owner.locate(request)
            path = owner._directory(request.session_id) / request.file_name
        elif category == "trace":
            trace = TraceReferenceDTO.model_validate_json(serialized_reference)
            store = TraceEventStore(sessions)
            store.locate(trace)
            path = store._trace_file(trace.session_id)
        else:
            raise ValueError(f"不支持的记录删除故障类别: {category}")
        size = path.stat().st_size
        path.replace(backup)
        return {"category": category, "path": str(path), "backup": str(backup),
                "removed_bytes": size, "exists_after": path.exists()}


if __name__ == "__main__":
    print(json.dumps(delete_record(sys.argv[1], Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])), ensure_ascii=False))
