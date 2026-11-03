"""搜索与通信合同在独立进程中均可先行导入，不依赖测试收集顺序。"""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("modules", [
    ("app.schemas.search_federation", "app.schemas.internal_v2.communication"),
    ("app.schemas.internal_v2.communication", "app.schemas.search_federation"),
])
def test_search_and_communication_contracts_import_in_either_order(modules: tuple[str, str]) -> None:
    command = [sys.executable, "-c", "import importlib,sys\nfor name in sys.argv[1:]: importlib.import_module(name)", *modules]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, f"{command}\n{result.stdout}\n{result.stderr}"
