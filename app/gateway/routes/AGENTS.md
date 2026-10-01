# 目录用途

`app/gateway/routes/` 存放 Gateway 自有接口（`/api/gateway/*`）的路由模块，按业务分组拆分自原 `app/gateway/main.py`。

# 可修改内容

- 各分组的路由 handler 与其就近使用的依赖提供者、DTO 转换辅助函数。
- `_shared.py` 中同时被 lifespan 与多个路由组使用的辅助函数。
- `__init__.py` 中的模块导出。

# 不可修改内容

- 不得改变任何路由的 path、HTTP method、`response_model`，否则会漂移 openapi 契约。
- 不得改变路由注册顺序；顺序契约由 `app/gateway/route_registry.py` 的 `ROUTE_MODULES` 与文件内定义顺序共同决定。
- 不得在路由模块中实现 Agent 业务逻辑或读写被代理工作区的 `.boxteam` 数据。

# 规范

- 每个路由模块只暴露一个模块级 `router = APIRouter()`；handler 用 `@router.*` 装饰。
- 只被单个路由组使用的辅助函数留在该组文件内；被多个路由组共同使用的辅助函数放入 `_shared.py`。
- 新增路由模块必须同步登记进 `app/gateway/route_registry.py` 的 `ROUTE_MODULES`，并保持拆分前的相对注册顺序。
- 路由模块不得反向 import `app.gateway.main`（该模块只负责应用装配）。
- 模板示例；在整理 `AGENTS.md` 时请保留此行。
