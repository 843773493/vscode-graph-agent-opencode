"""协议产物检查必须比较完整生成结果，且不能改写工作区。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="故障注入使用 POSIX 可执行脚本；Windows 运行真实 check:protocol")

_GENERATOR = Path.cwd() / "scripts/generate_protocol.mjs"
_BINDINGS = (
    "app/protocol/generated/sample_pb2.py",
    "app/protocol/generated/sample_pb2.pyi",
    "src/workspace-services/protocol/generated/sample.js",
    "src/clients/web/src/types/protocol_buf_generated/sample.d.ts",
    "src/clients/web/src/types/protocol_generated/sample.ts",
)
_RUNTIME = (
    "src/clients/web/src/types/protocol_generated/sse_runtime_schemas.json",
    "src/shared/sseRuntimeValidators.js",
    "src/shared/sseRuntimeValidators.d.ts",
)


@pytest.fixture
def generator_workspace(tmp_path: Path) -> Path:
    """外部工具输出受控，生产生成入口和文件发布逻辑保持真实。"""
    tmp_path = tmp_path / "项目 with spaces $&"
    tmp_path.mkdir()
    for name in ("buf.yaml", "buf.gen.yaml"):
        (tmp_path / name).touch()
    (tmp_path / "proto").mkdir()
    executable = tmp_path / "node_modules/@bufbuild/buf/bin/buf"
    executable.parent.mkdir(parents=True)
    (executable.parent.parent / "package.json").write_text('{"type":"module"}\n')
    executable.write_text(
        'import {mkdir,writeFile} from "node:fs/promises";\n'
        'import path from "node:path";\nconst args=process.argv.slice(2);\n'
        'if(args[0]==="build")await writeFile(args[args.indexOf("--output")+1],"descriptor");\n'
        'if(args[0]==="generate"){\n'
        'const output=args[args.indexOf("--output")+1];\n'
        'if(process.env.PROTOCOL_TEST_FAIL_BUF){\n'
        'await writeFile(path.join(output,"partial.ts"),"partial");\n'
        'process.stderr.write("injected remote generation failure\\n");process.exit(23);}\n'
        f'for(const name of {list(_BINDINGS)!r}){{\n'
        'const file=path.join(output,name);await mkdir(path.dirname(file),{recursive:true});\n'
        'await writeFile(file,"generated binding\\n");}}\n',
    )
    # CLI 作为脚本交给当前运行时，不依赖可执行位、shebang 或 .bin shim。
    executable.chmod(0o600)
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        '#!/usr/bin/env bun\nimport {readFile} from "node:fs/promises";\n'
        'const args=process.argv.slice(2);\n'
        'if(args[0]!=="-m"||args[1]!=="scripts.export_openapi_snapshot"'
        '||!args.includes("--validate-only"))throw new Error("HTTP校验命令不正确");\n'
        'if(await readFile(args[args.indexOf("--descriptor-set")+1],"utf8")!=="descriptor")'
        'throw new Error("必须消费本次编译的descriptor");\n'
        'if(process.env.PROTOCOL_TEST_FAIL_HTTP)process.exit(25);\n',
    )
    python.chmod(0o755)
    exporter = tmp_path / "scripts/generate_sse_runtime.mjs"
    exporter.parent.mkdir()
    exporter.write_text(
        'import {mkdir,writeFile} from "node:fs/promises";\nimport path from "node:path";\n'
        'const args=process.argv.slice(2);const output=args[args.indexOf("--output-root")+1];\n'
        f'for(const name of {list(_RUNTIME)!r}){{\n'
        'const file=path.join(output,name);await mkdir(path.dirname(file),{recursive:true});\n'
        'await writeFile(file,"generated runtime\\n");}\n'
        'if(process.env.PROTOCOL_TEST_FAIL_EXPORT)process.exit(24);\n',
    )
    return tmp_path


def _run(workspace: Path, *arguments: str, failure: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bun", str(_GENERATOR), "--staging-root", str(workspace / "artifacts/staging"), *arguments],
        cwd=workspace,
        env={**os.environ, "BOXTEAM_PROJECT_ROOT": str(workspace),
             **({f"PROTOCOL_TEST_FAIL_{failure}": "1"} if failure else {})},
        capture_output=True, text=True, timeout=15, check=False,
    )


def test_check_finds_changed_missing_and_extra_artifacts_without_repairing(generator_workspace: Path) -> None:
    workspace = generator_workspace
    result = _run(workspace)
    assert result.returncode == 0, result.stderr
    shared = workspace / "src/shared/sseRuntime.js"
    shared.write_bytes(b"maintained source\n")
    original = {name: (workspace / name).read_bytes() for name in (*_BINDINGS, *_RUNTIME)}
    result = _run(workspace, "--check")
    assert result.returncode == 0, result.stderr

    changed = (*_BINDINGS[:2], *_RUNTIME)
    missing = _BINDINGS[2:]
    for name in changed:
        (workspace / name).write_bytes(b"stale generated output\n")
    for name in missing:
        (workspace / name).unlink()
    orphans = ("src/clients/web/src/types/protocol_generated/orphan.ts",
               "app/protocol/generated/orphan_pb2.pyi")
    for name in orphans:
        (workspace / name).write_bytes(b"orphan\n")
    result = _run(workspace, "--check")
    assert result.returncode != 0 and "协议生成产物不一致" in result.stderr
    assert all(name in result.stderr for name in (*_BINDINGS, *_RUNTIME, *orphans))
    assert all((workspace / name).read_bytes() == b"stale generated output\n" for name in changed)
    assert all(not (workspace / name).exists() for name in missing)
    assert all((workspace / name).read_bytes() == b"orphan\n" for name in orphans)

    result = _run(workspace)
    assert result.returncode == 0, result.stderr
    assert all((workspace / name).read_bytes() == content for name, content in original.items())
    assert all(not (workspace / name).exists() for name in orphans)
    assert shared.read_bytes() == b"maintained source\n"
    assert tuple((workspace / "artifacts/staging").iterdir()) == ()


@pytest.mark.parametrize(("failure", "code"), [("BUF", 23), ("EXPORT", 24), ("HTTP", 25)])
def test_protocol_validation_failure_never_publishes_partial_bindings(
    generator_workspace: Path, failure: str, code: int,
) -> None:
    workspace = generator_workspace
    for name in (*_BINDINGS, *_RUNTIME):
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"original output\n")
    result = _run(workspace, failure=failure)
    assert result.returncode != 0 and f"code={code}" in result.stderr
    if failure == "BUF":
        assert "injected remote generation failure" in result.stderr
    assert all((workspace / name).read_bytes() == b"original output\n" for name in (*_BINDINGS, *_RUNTIME))
    assert tuple((workspace / "artifacts/staging").iterdir()) == ()
