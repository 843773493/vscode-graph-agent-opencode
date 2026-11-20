from unittest.mock import MagicMock

import pytest

from app.abstractions.internal_message import PreparedInternalMessage
from app.services.orchestration.runtime_source_dispatcher import RuntimeSourceDispatcher


@pytest.fixture
def dispatcher_dependencies():
    return MagicMock(), MagicMock(), MagicMock()


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["publish_context", "submit"])
@pytest.mark.parametrize("metadata", [{}, {"structured_prompt_kind": "checkpoint_reminder"}])
async def test_internal_entry_rejects_missing_schema_before_admission(entry, metadata, dispatcher_dependencies):
    saver, sessions, jobs = dispatcher_dependencies
    dispatcher = RuntimeSourceDispatcher(saver=saver, session_service=sessions, job_service=jobs)
    with pytest.raises(ValueError, match="metadata|schema version"):
        await getattr(dispatcher, entry)(
            "ses_invalid", "thr_invalid", PreparedInternalMessage("裸附加正文", metadata),
            source_id="invalid-source", idempotency_key="invalid-event",
        )
    assert not saver.mock_calls and not sessions.mock_calls and not jobs.mock_calls
