"""验证具体路由先于通配代理；公开结构由 proto／OpenAPI 契约测试核对。

不以接口总数或整个 OpenAPI 的摘要代替行为断言；正常新增接口不应要求更新不透明 hash。
"""

from app.gateway.main import app


def test_route_registration_order_is_stable() -> None:
    paths = [getattr(route, "path", None) for route in app.routes]
    workspace_proxy_index = paths.index("/api/v1/{path:path}")
    for path in (
        "/api/gateway/workspaces",
        "/api/gateway/local-directories",
        "/api/gateway/resource-navigation/workspaces/{source_workspace_id}",
        "/api/gateway/skill-source-connections/{gateway_id}/workspaces/{workspace_id}/publications/wait",
        "/api/gateway/skill-sources/refresh",
        "/api/gateway/search/resolve",
        "/api/gateway/search/workspaces",
        "/api/gateway/resource-addresses/parse",
        "/api/gateway/resource-addresses/thread",
        "/api/gateway/resource-addresses/change",
        "/api/v1/resources/navigation",
        "/api/v1/context/read",
        "/api/v1/context/search",
    ):
        assert paths.count(path) == 1, f"Gateway 自有路由必须唯一登记: {path}"
        assert paths.index(path) < workspace_proxy_index, f"Gateway 自有路由被通配代理遮蔽: {path}"
