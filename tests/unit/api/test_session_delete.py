from __future__ import annotations

from app.api.sessions import router as sessions_router


def test_public_session_delete_route_is_removed() -> None:
    assert not any(
        route.path == "/{session_id}" and "DELETE" in route.methods
        for route in sessions_router.routes
    )
