"""Session 集成测试共享的按需旧产品输入 fixture。"""

from tests.integration.backend.sessions.attachment_migration.legacy_inputs import (
    backup_case as backup_case,  # noqa: PLC0414
)
from tests.integration.backend.sessions.attachment_migration.legacy_inputs import (
    legacy_case_factory as legacy_case_factory,  # noqa: PLC0414
)
from tests.integration.backend.sessions.attachment_migration.legacy_inputs import (
    legacy_schema3_inputs as legacy_schema3_inputs,  # noqa: PLC0414
)
from tests.integration.backend.sessions.attachment_migration.legacy_inputs import (
    shared_blob_case as shared_blob_case,  # noqa: PLC0414
)
from tests.support.model_stream.compaction_model import (
    compaction_summary_model as compaction_summary_model,  # noqa: PLC0414 - 本地模型流候选摘要 fixture
)
