import json
from uuid import UUID

from app.core.workspace_identity import (
    load_or_create_workspace_id,
    workspace_identity_path,
)


def test_workspace_identity_is_a_persistent_uuid4(tmp_path):
    first_id = load_or_create_workspace_id(tmp_path)
    second_id = load_or_create_workspace_id(tmp_path)

    assert first_id == second_id
    assert UUID(first_id).version == 4
    assert json.loads(
        workspace_identity_path(tmp_path).read_text(encoding="utf-8")
    ) == {"workspace_id": first_id}
