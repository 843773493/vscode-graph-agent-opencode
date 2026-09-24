from __future__ import annotations

import pytest

from app.services.infrastructure.artifact_service import ArtifactService


async def test_get_response_refuses_to_invent_artifact() -> None:
    """产物存储未实现时必须显式报错，不能返回伪造的 ArtifactDTO。"""
    service = ArtifactService()

    with pytest.raises(RuntimeError) as excinfo:
        await service.get_response("art_probe")

    message = str(excinfo.value)
    assert "art_probe" in message
    assert "尚未实现" in message


async def test_list_by_job_refuses_to_invent_artifacts() -> None:
    """未能证明 job 存在真实产物时，禁止返回固定条数的假列表。"""
    service = ArtifactService()

    with pytest.raises(RuntimeError) as excinfo:
        await service.list_by_job("job_probe")

    message = str(excinfo.value)
    assert "job_probe" in message
    assert "尚未实现" in message
