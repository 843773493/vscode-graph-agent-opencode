"""冻结「产物存储尚未实现」的对外契约。

``ArtifactService`` 按「永不返回虚假默认值」改为显式失败（抛 RuntimeError）。
适配层必须把这类「服务端能力缺失」落成 501 并遵守 APIResponse/request_id 约定，
而不是让 FastAPI 直接落成无上下文的 500（响应体也不走工作区信封）。
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api import artifacts as artifacts_api
from app.api import jobs as jobs_api


class _UnimplementedArtifactService:
    async def get_response(self, artifact_id: str):
        raise RuntimeError(f"产物存储尚未实现：无法解析产物 {artifact_id!r}")

    async def list_by_job(self, job_id: str):
        raise RuntimeError(f"产物存储尚未实现：无法列出 Job {job_id!r} 的产物")


@pytest.mark.asyncio
async def test_get_artifact_maps_unimplemented_to_501() -> None:
    with pytest.raises(HTTPException) as captured:
        await artifacts_api.get_artifact(
            "art_001",
            _="local",
            request_id="req_artifact",
            artifact_service=_UnimplementedArtifactService(),
        )

    assert captured.value.status_code == 501
    assert "产物存储尚未实现" in str(captured.value.detail)


@pytest.mark.asyncio
async def test_list_job_artifacts_maps_unimplemented_to_501() -> None:
    with pytest.raises(HTTPException) as captured:
        await jobs_api.list_job_artifacts(
            "job_001",
            _="local",
            request_id="req_job_artifacts",
            artifact_service=_UnimplementedArtifactService(),
        )

    assert captured.value.status_code == 501
    assert "产物存储尚未实现" in str(captured.value.detail)


@pytest.mark.asyncio
async def test_get_artifact_wraps_success_in_api_response() -> None:
    from app.schemas.internal_v2.artifact import ArtifactDTO

    class _OkArtifactService:
        async def get_response(self, artifact_id: str):
            return ArtifactDTO(
                artifact_id=artifact_id,
                job_id="job_x",
                type="markdown",
                name="x.md",
                path="/tmp/x.md",
            )

    response = await artifacts_api.get_artifact(
        "art_001",
        _="local",
        request_id="req_ok",
        artifact_service=_OkArtifactService(),
    )

    assert response.request_id == "req_ok"
    assert response.data is not None
    assert response.data.artifact_id == "art_001"
