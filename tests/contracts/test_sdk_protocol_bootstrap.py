"""旧 wheel 无法导入新应用时，纯生成和标准依赖安装仍可完成自举。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from tests.harness.python.run_context import TestRunContext


@pytest.fixture
def bootstrap_checkout(request: pytest.FixtureRequest) -> tuple[Path, Path]:
    context = TestRunContext.from_test_file(Path(request.node.path)).prepare()
    root = context.run_root / "runtime" / "checkout"
    root.mkdir(parents=True)
    source = Path.cwd()
    for directory in ("app", "configs", "resources", "sdk", "proto", "scripts"):
        shutil.copytree(source / directory, root / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules"))
    for name in ("package.json", "pyproject.toml", "uv.lock", "buf.yaml", "buf.gen.yaml", ".python-version"):
        shutil.copyfile(source / name, root / name)
    archive = tomllib.loads((source / "pyproject.toml").read_text())["tool"]["uv"]["sources"]["boxteam-plugin-sdk"]["path"]
    (root / archive).parent.mkdir(parents=True)
    shutil.copyfile(source / archive, root / archive)
    for directory in ("src/clients/web/src/types/protocol_generated", "src/clients/web/src/types/protocol_buf_generated",
                      "src/shared"):
        shutil.copytree(source / directory, root / directory)
    shutil.copyfile(source / "src/clients/web/package.json", root / "src/clients/web/package.json")
    # 复用生成器工具依赖；Python 环境仍由独立标准锁安装，不借用产品源码或根 .venv。
    for directory in ("node_modules", "src/clients/web/node_modules"):
        (root / directory).symlink_to(source / directory, target_is_directory=True)
    return root, context.artifacts_dir


def test_old_sdk_new_wire_generation_install_and_application_gate(bootstrap_checkout) -> None:
    root, artifacts = bootstrap_checkout
    records = []
    environment = {**os.environ, "BOXTEAM_PROJECT_ROOT": str(root)}
    environment.pop("PYTHONPATH", None)

    def run(label: str, command: list[str], expected: int = 0) -> subprocess.CompletedProcess:
        result = subprocess.run(command, cwd=root, env=environment, capture_output=True, text=True,
            timeout=180, check=False)
        (artifacts / f"{label}.stdout.log").write_text(result.stdout)
        (artifacts / f"{label}.stderr.log").write_text(result.stderr)
        records.append({"label": label, "command": command, "cwd": str(root), "exit_code": result.returncode})
        (artifacts / "commands.json").write_text(json.dumps(records, indent=2))
        assert result.returncode == expected, result.stderr
        return result

    run("install-old", ["uv", "sync", "--frozen", "--offline", "--no-dev", "--python", sys.executable])
    python = str(root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    proto = root / "proto/boxteam/common/v1/service_lifecycle.proto"
    proto.write_text(proto.read_text() + "\nmessage SdkBootstrapProbe {\n  string value = 1;\n}\n")
    consumer = root / "app/protocol/sdk_upgrade_probe.py"
    consumer.write_text("from boxteam.common.v1.service_lifecycle_pb2 import SdkBootstrapProbe\n"
        "assert SdkBootstrapProbe.FromString(SdkBootstrapProbe(value='current-wire').SerializeToString()).value == 'current-wire'\n")
    exporter = root / "scripts/export_openapi_snapshot.py"
    exporter.write_text(exporter.read_text().replace("import argparse", "import app.protocol.sdk_upgrade_probe\nimport argparse"))
    failed = run("old-cannot-import-new", [python, "-c", "import app.protocol.sdk_upgrade_probe"], expected=1)
    assert "SdkBootstrapProbe" in failed.stderr
    run("generate-with-old-installed", ["bun", "scripts/generate_protocol.mjs", "--staging-root", str(artifacts / "staging")])
    # 生成不偷偷更新环境；旧 wheel 仍不能满足新消费方。
    run("old-remains-old", [python, "-c", "import app.protocol.sdk_upgrade_probe"], expected=1)
    run("prepare-current", ["bun", "scripts/plugins/prepare-product-sdk.mjs", "python"])
    run("install-current", ["uv", "sync", "--offline", "--no-dev"])
    loaded = run("new-wire-loaded", [python, "-c", ("import app.protocol.sdk_upgrade_probe; "
        "import boxteam.common.v1.service_lifecycle_pb2 as wire; print(wire.__file__)")])
    assert Path(loaded.stdout.strip()).is_relative_to(root / ".venv")
    run("application-gate", ["bun", "scripts/check_protocol_application.mjs"])
    generated = root / "sdk/python/src/boxteam/common/v1/service_lifecycle_pb2.py"
    generated.write_text(generated.read_text() + "\n# 过期产物探针\n")
    watched = [generated, root / "uv.lock", root / "pyproject.toml", Path(loaded.stdout.strip())]
    before = {str(file): hashlib.sha256(file.read_bytes()).hexdigest() for file in watched}
    rejected = run("check-rejects-stale", ["bun", "scripts/generate_protocol.mjs", "--check",
        "--staging-root", str(artifacts / "check-staging")], expected=1)
    assert "协议生成产物不一致" in rejected.stderr
    assert before == {str(file): hashlib.sha256(file.read_bytes()).hexdigest() for file in watched}
