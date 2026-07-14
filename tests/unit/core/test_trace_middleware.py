from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api.deps import get_request_id
from app.core.trace_middleware import TraceMiddleware
from app.schemas.public_v2.common import APIResponse


def _build_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(TraceMiddleware)

    @app.get("/request-id")
    async def request_id_endpoint(
        request_id: str = Depends(get_request_id),
    ) -> APIResponse[dict[str, str]]:
        return APIResponse(data={"request_id": request_id}, request_id=request_id)

    return TestClient(app)


def test_generated_request_id_is_identical_in_header_and_body() -> None:
    response = _build_client().get("/request-id")

    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    assert request_id
    assert response.json()["request_id"] == request_id
    assert response.json()["data"]["request_id"] == request_id


def test_incoming_request_id_is_used_as_the_single_authority() -> None:
    response = _build_client().get(
        "/request-id",
        headers={"X-Request-ID": "req_from_client"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req_from_client"
    assert response.json()["request_id"] == "req_from_client"
