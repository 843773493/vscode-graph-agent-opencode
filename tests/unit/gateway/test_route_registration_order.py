"""Gateway 自有路由注册顺序与 openapi 形态的回归判据。

拆分 `app/gateway/main.py` 后，routes 序列与 openapi 文档必须与拆分前逐字一致。

注意：`operationId` 由 FastAPI 的 `generate_unique_id` 取 `list(route.methods)[0]`
生成，而两个通配代理路由声明了多方法集合，集合迭代顺序受字符串 hash 随机化
（`PYTHONHASHSEED`）影响，因此规范化 openapi 的 SHA256 只在固定 hash seed 下可
复现。openapi 判据因此在 `PYTHONHASHSEED=5` 的子进程中重算，避免测试自身随机失败。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from app.gateway.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[3]

BASELINE_OPENAPI_SHA256 = (
    "b5393213d768589989e22c75e9747e45af8e71f367575009e93f42b0ceda99c3"
)
BASELINE_ROUTE_COUNT = 96
BASELINE_PATH_COUNT = 76
BASELINE_DUPLICATE_OPERATION_IDS = (
    "Duplicate Operation ID proxy_auxiliary_http_api_gateway_workspaces__workspace_id___service_path___path__patch",
    "Duplicate Operation ID proxy_workspace_api_api_v1__path__patch",
)

_CHILD = """
import hashlib
import json
import sys
import warnings

sys.path.insert(0, sys.argv[1])
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always')
    import app.gateway.main as gateway_main
    spec = gateway_main.app.openapi()
raw = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
print(hashlib.sha256(raw.encode()).hexdigest())
print(len(spec.get('paths', {})))
for warning in caught:
    message = str(warning.message)
    if message.startswith('Duplicate Operation ID'):
        print(message.split(' for function ')[0])
"""


def test_route_registration_order_is_stable() -> None:
    paths = [getattr(route, "path", None) for route in app.routes]
    assert len(paths) == BASELINE_ROUTE_COUNT

    # 顺序契约：Gateway 自有接口必须先于两个通配代理路由，工作区代理最后。
    auxiliary_index = paths.index(
        "/api/gateway/workspaces/{workspace_id}/{service_path}/{path:path}"
    )
    assert paths.index("/api/gateway/workspaces") < auxiliary_index
    assert paths.index("/api/gateway/local-directories") < auxiliary_index
    assert paths.index("/api/v1/{path:path}") > auxiliary_index


def test_openapi_document_matches_baseline() -> None:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = "5"
    completed = subprocess.run(
        [sys.executable, "-c", _CHILD, str(PROJECT_ROOT)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=True,
    )
    lines = completed.stdout.splitlines()
    digest = lines[0]
    path_count = int(lines[1])
    duplicates = lines[2:]

    assert path_count == BASELINE_PATH_COUNT
    assert sorted(set(duplicates)) == sorted(BASELINE_DUPLICATE_OPERATION_IDS)
    assert digest == BASELINE_OPENAPI_SHA256
