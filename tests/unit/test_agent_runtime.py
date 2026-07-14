from unittest.mock import MagicMock, patch

from app.runtime.agent_runtime import build_session_agent_runtime


def test_build_session_agent_runtime_injects_job_service() -> None:
    config_service = MagicMock()
    config_service.resolve_agent_id.return_value = "default"
    dependency_provider = MagicMock()
    job_service = MagicMock()
    dependency_provider.get_job_service.return_value = job_service
    dependency_provider.get_checkpointer.return_value = MagicMock()

    with patch(
        "app.runtime.agent_runtime.create_runtime_deep_agent_for_session"
    ) as create_runtime:
        build_session_agent_runtime(
            session_id="session_test",
            agent_id="default",
            config_service=config_service,
            background_task_registry=MagicMock(),
            background_message_bus=MagicMock(),
            job_event_bus=MagicMock(),
            dependency_provider=dependency_provider,
        )

    assert create_runtime.call_args.kwargs["job_service"] is job_service
