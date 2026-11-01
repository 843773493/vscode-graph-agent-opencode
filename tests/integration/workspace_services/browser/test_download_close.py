"""真实下载保存后尚未返回原回执时，关闭必须保持待核实。"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    browser_client as browser_client,  # noqa: PLC0414 — 复用装具，按当前文件隔离。
)


async def test_close_waits_for_the_original_download_publication_after_native_save(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
) -> None:
    client, root, run = browser_client
    target, peer = (f"browser_{uuid4().hex}" for _ in range(2))
    key = json.dumps("download_" + uuid4().hex)
    for rid in (target, peer):
        await client.create_browser(browser_id=rid, owner_revision=1, title="下载关闭验收", url="about:blank")
    receipts: dict[str, object] = {}
    try:
        installed = await client.perform(target, "run", {"code": (
            "const { pathToFileURL } = await import('node:url');"
            "const { join } = await import('node:path');"
            "const { BrowserStateStore } = await import(pathToFileURL(join(process.env.BOXTEAM_PROJECT_ROOT,"
            " 'src/workspace-services/browser/server/browserStateStore.js')));"
            "const arrived = Promise.withResolvers(), resume = Promise.withResolvers();"
            f"const state = globalThis[{key}] = {{ resume, saved: null }};"
            "const original = BrowserStateStore.prototype.writeDownload;"
            "BrowserStateStore.prototype.writeDownload = async function (...args) {"
            " BrowserStateStore.prototype.writeDownload = original;"
            " const saved = await original.apply(this, args); state.saved = saved;"
            " arrived.resolve(); await resume.promise; return saved; };"
            "await page.setContent('<a download=report.txt href=\"data:text/plain,actual-download-body\">download</a>');"
            "await page.locator('a').click(); await arrived.promise; return state.saved;"
        )}, actor="agent")
        saved = installed["result"]
        assert isinstance(saved, dict)
        assert saved["filename"] == "report.txt" and saved["status"] == "completed"
        download_id = saved["download_id"]
        native = root / ".boxteam/browser-manager/downloads" / target / f"{download_id}-report.txt"
        assert native.read_bytes() == b"actual-download-body"
        with pytest.raises(BrowserRequestError) as caught:
            await client.stop_resource(target)
        assert caught.value.code == "browser_close_operation_drain_timeout" and caught.value.status == 408
        unknown = await client.get_resource(target)
        assert unknown["status"] == "running" and unknown["reconciliation_status"] == "unknown"
        assert unknown["transport_owner_revision"] == 1 and unknown["pages"] == []
        assert unknown.get("downloads", []) == []
        rows = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())["browsers"]
        pending, = [row for row in rows if row["browser_id"] == target]
        assert pending["status"] == "running" and pending["reconciliation_status"] == "unknown"
        assert not pending.get("downloads")
        released = await client.perform(peer, "run", {"code": (
            f"globalThis[{key}].resume.resolve(); await page.setContent('<title>independent download peer</title>');"
            "return await page.title();"
        )}, actor="agent")
        assert released["result"] == "independent download peer"
        stopped = (await client.stop_resource(target))["browser"]
        assert isinstance(stopped, dict)
        assert stopped["status"] == "closed" and stopped["reconciliation_status"] == "verified"
        assert stopped["downloads"] == [saved] and stopped["pages"] == []
        assert native.read_bytes() == b"actual-download-body"
        rows = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())["browsers"]
        final, = [row for row in rows if row["browser_id"] == target]
        assert final["status"] == "closed" and final["reconciliation_status"] == "verified"
        assert final["downloads"] == [saved]
        receipts = {"saved": saved, "unknown": unknown, "saved_unknown": pending,
            "stop_failure": caught.value.payload, "stopped": stopped, "saved_final": final}
    finally:
        await client.perform(peer, "run", {"code": (
            f"globalThis[{key}]?.resume.resolve(); delete globalThis[{key}]; return 'released';"
        )}, actor="agent")
        for rid in (target, peer):
            await client.delete_resource(rid)
        (run.artifacts_dir / "download-close.json").write_text(
            json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
