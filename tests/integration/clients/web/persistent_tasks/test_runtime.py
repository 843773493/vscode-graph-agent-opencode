"""真实 worker 生命周期与后台恢复，经 Gateway 和 Web 核对暂停及失败。"""

from app.core.resource_uri import thread_resource_uri
from tests.integration.clients.web.persistent_tasks.test_management import (
    browser_runner as browser_runner,  # noqa: PLC0414 - 共用真实浏览器装具
)
from tests.integration.clients.web.persistent_tasks.test_management import (
    http_schema_endpoint_factory as http_schema_endpoint_factory,  # noqa: PLC0414
)
from tests.integration.clients.web.persistent_tasks.test_management import (
    oauth_endpoint_factory as oauth_endpoint_factory,  # noqa: PLC0414
)
from tests.integration.clients.web.persistent_tasks.test_management import (
    product_case as product_case,  # noqa: PLC0414
)
from tests.integration.clients.web.persistent_tasks.test_management import (
    recovery_case as recovery_case,  # noqa: PLC0414
)
from tests.support.api_helpers import create_session


async def test_runtime_recovery_pause_and_worker_failure_are_visible(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, browser_runner,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, web=True,
                     backend_app="tests.integration.clients.web.persistent_tasks.runtime_app:app") as (client, replay, case),
    ):
        title = "恢复与失败验收"
        session = await create_session(client, title)
        thread = (await client.get(f"/api/v1/sessions/{session}/main-thread")).json()["data"]["thread_id"]
        uri = thread_resource_uri(case.mount.workspace_id, session, thread)
        response = await client.post("/api/v1/persistent-tasks", json={
            "idempotency_key": "runtime-recovery-web", "configuration": {
                "name": "恢复期间暂停的任务", "trigger": {"type_id": "builtin.manual", "version": "1", "timezone": "UTC"},
                "condition": {"type_id": "builtin.always", "version": "1"},
                "context": {"type_id": "builtin.literal", "version": "1", "content": "恢复期间的新触发"},
                "action": {"type_id": "builtin.runtime_source", "version": "1", "target_uri": uri,
                           "delivery_mode": "context_only", "delivery_policy": "after_turn"},
                "policies": {"completion": "continuous", "misfire": "skip", "match_mode": "each_match"},
            },
        })
        assert response.status_code == 200, response.text
        result = await browser_runner(case, client, target_uri=uri, title=title, other_session_id=session,
                                      script="runtime", environment=replay.environment)
        assert result["paused"]["lifecycle"] == "paused"
        assert result["final"]["lifecycle"] == "paused" and not result["final"]["recovering"]
        assert result["queued"]["phase"] == "suppressed" and result["queued"]["delivery"] is None
        assert result["failure"]["runtime"]["state"] == "failed"
        assert list(replay.artifacts.glob("*.json")) == []
