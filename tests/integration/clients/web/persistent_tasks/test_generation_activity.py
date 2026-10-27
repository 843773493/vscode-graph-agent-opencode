"""真实任务先发布空输出queued，再创建输出，页面仍按策略自动打开。"""

from app.core.resource_uri import thread_resource_uri
from app.testing.model_stream import build_cassette, chat_completion_frames
from tests.integration.backend.sessions.test_persistent_task_generations import (
    generation_configuration,
)
from tests.integration.clients.web.persistent_tasks.test_management import (
    browser_runner as browser_runner,  # noqa: PLC0414 - 同一真实浏览器入口
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
from tests.support.api_waiters import wait_for_job_done


async def test_queued_output_appearing_opens_the_actual_generated_session(
    product_case, oauth_endpoint_factory, http_schema_endpoint_factory, browser_runner,
):
    async with (
        oauth_endpoint_factory() as secure, http_schema_endpoint_factory("sibling") as sibling,
        product_case(secure, sibling, web=True) as (client, replay, case),
    ):
        title = "保持选择的起始会话"
        origin = await create_session(client, title)
        output = await create_session(client, "自动打开的输出会话")
        thread = (await client.get(f"/api/v1/sessions/{output}/main-thread")).json()["data"]["thread_id"]
        configuration = generation_configuration(case.mount.workspace_id, mode="continue_existing", target={
            "workspace_id": case.mount.workspace_id, "session_id": output, "thread_id": thread,
        })
        configuration["name"] = "自动打开的会话任务"
        configuration["action"]["generation"]["ui_policy"] = {"on_run_started": "open_generated", "on_run_completed": "notify"}
        response = await client.post("/api/v1/persistent-tasks", json={"idempotency_key": "web-output-activity", "configuration": configuration})
        assert response.status_code == 200, response.text
        task = response.json()["data"]
        cassette = build_cassette(asset_id="task-generation-activity", model="control-replay-model",
            url="https://opencode.ai/zen/v1/chat/completions", frames=chat_completion_frames(
                response_id="task-generation-activity", model="control-replay-model",
                deltas=({"role": "assistant", "content": "自动激活验收完成。"},), finish_reason="stop",
            ))
        calls = replay.control.plan_model_calls(output, thread, cassette.raw["interactions"])
        result = await browser_runner(case, client, title=title, other_session_id=output,
            target_uri=thread_resource_uri(case.mount.workspace_id, origin,
                (await client.get(f"/api/v1/sessions/{origin}/main-thread")).json()["data"]["thread_id"]),
            script="generation_activity", environment=replay.environment)
        assert result["before"]["generation"]["status"] == result["accepted"]["generation"]["status"] == "queued"
        assert result["before"]["generation"]["outputs"] == []
        assert result["accepted"]["generation"]["outputs"][0]["session_id"] == output
        assert result["accepted"]["task_id"] == task["task_id"]
        await wait_for_job_done(client, result["accepted"]["generation"]["job_id"], max_attempts=20)
        assert len(calls) == len(list(replay.artifacts.glob("*.json"))) == 1
