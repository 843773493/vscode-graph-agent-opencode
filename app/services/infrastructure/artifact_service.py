from __future__ import annotations

from app.schemas.internal_v2.artifact import ArtifactDTO


class ArtifactService:
    """任务产物查询服务。

    TODO(产物存储): 产物存储尚未实现。当前仓库里没有任何组件把产物落盘到
    `${workspace}/.boxteam/artifacts/`，也没有 artifact_id/job_id 到文件的权威索引，
    因此本服务无法回答任何真实查询。按「永不返回虚假默认值」，这里显式失败，
    待真实存储与索引落地后再补上读取实现。
    """

    async def get_response(self, artifact_id: str) -> ArtifactDTO:
        raise RuntimeError(
            f"产物存储尚未实现：无法解析产物 {artifact_id!r}；"
            "当前没有任何组件写入 .boxteam/artifacts/，也没有 artifact_id 索引"
        )

    async def list_by_job(self, job_id: str) -> list[ArtifactDTO]:
        raise RuntimeError(
            f"产物存储尚未实现：无法列出 Job {job_id!r} 的产物；"
            "当前没有任何组件写入 .boxteam/artifacts/，也没有 job_id 索引"
        )
