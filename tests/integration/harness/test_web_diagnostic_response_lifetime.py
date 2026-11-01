"""正式聊天诊断读取原响应正文后，不应让驱动继续持有完整历史。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.support.paths import chromium_temporary_environment


@pytest.mark.parametrize("reader", ["cdp", "playwright-cache"])
def test_real_chat_diagnostics_release_complete_response_bodies(reader: str, tmp_path: Path) -> None:
    project = Path.cwd()
    driver = project / "tests/integration/clients/web/conversation_lifecycle/conversation_lifecycle.mjs"
    helper = driver.with_name("diagnostic_response_bodies.mjs")
    probe = tmp_path / "probe.mjs"
    probe.write_text(
        'import { registerHooks } from "node:module";\n'
        + (
            "registerHooks({ resolve(specifier, context, nextResolve) {\n"
            f"if (specifier === './diagnostic_response_bodies.mjs' && context.parentURL === {json.dumps(driver.as_uri())}) "
            "return { shortCircuit: true, url: 'data:text/javascript,' + encodeURIComponent("
            + json.dumps(
                f"export {{runDiagnosticResponseLifetimeSelfTest}} from {json.dumps(helper.as_uri())};"
                "export const readDiagnosticResponseBody = response => response.body();"
            )
            + ") };\nreturn nextResolve(specifier, context);\n} });\n"
            if reader == "playwright-cache" else ""
        )
        + 'process.argv.push("--diagnostic-response-lifetime-self-test");\n'
        f"await import({json.dumps(driver.as_uri())});\n",
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH": os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        or shutil.which("chromium") or shutil.which("chromium-browser") or "",
    }
    with chromium_temporary_environment(tmp_path / "chromium-tmp") as temporary_environment:
        environment.update(temporary_environment)
        result = subprocess.run(
            ["node", "--expose-gc", str(probe)], cwd=project, env=environment,
            capture_output=True, text=True, timeout=120, check=False,
        )
    (tmp_path / "stdout.raw").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "stderr.raw").write_text(result.stderr, encoding="utf-8")
    (tmp_path / "process.json").write_text(json.dumps({
        "command": ["node", "--expose-gc", str(probe)], "returncode": result.returncode, "reader": reader,
    }, indent=2) + "\n", encoding="utf-8")
    facts = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"count":')]
    assert len(facts) == 1, result.stderr
    assert facts[0]["completeBodies"] == facts[0]["count"] == 160
    assert facts[0]["nativeRequests"] == 0
    assert facts[0]["nativeResponses"] == facts[0]["count"] + 1
    if reader == "cdp":
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
    else:
        # 旧读取路径必须被同一正式入口的检查拒绝，不能仅验证 helper 自身。
        assert result.returncode != 0
        assert "诊断完成后不应继续持有整批正文" in result.stderr
        assert facts[0]["retainedBytes"] >= facts[0]["count"] * facts[0]["payloadBytes"] / 4
