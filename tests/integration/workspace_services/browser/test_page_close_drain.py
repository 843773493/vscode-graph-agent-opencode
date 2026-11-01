"""真实页面已关闭而替代页激活尚未交付时，资源关闭不能提前核实。"""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    browser_client as browser_client,  # noqa: PLC0414 — 原装具按当前文件隔离。
)


async def test_close_waits_for_native_page_close_replacement_and_its_failure_record(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
) -> None:
    client, root, run = browser_client
    target, peer = (f"browser_{uuid4().hex}" for _ in range(2))
    key = json.dumps("page_close_" + uuid4().hex)
    for rid in (target, peer):
        await client.create_browser(browser_id=rid, owner_revision=1, title="页面关闭排空", url="about:blank")
    receipts: dict[str, object] = {}
    try:
        await client.perform(target, "navigate", {"type": "new_tab", "url": "about:blank"}, actor="agent")
        original = await client.get_resource(target)
        assert isinstance(original["pages"], list) and len(original["pages"]) == 2
        closing_page_id = original["active_page_id"]
        # 原生reset先真实完成，仅暂停回执；页面关闭和资源事实仍由原产品链生成。
        installed = await client.perform(target, "run", {"code": (
            "const { pathToFileURL } = await import('node:url');"
            "const { join } = await import('node:path');"
            "const { BrowserPointerController } = await import(pathToFileURL(join(process.env.BOXTEAM_PROJECT_ROOT,"
            " 'src/workspace-services/browser/server/operations/browserInput.js')));"
            "const entered = Promise.withResolvers(), resume = Promise.withResolvers();"
            f"const state = globalThis[{key}] = {{ page, entered, resume, settled: false }};"
            "const original = BrowserPointerController.prototype.reset;"
            "state.restore = () => { BrowserPointerController.prototype.reset = original; };"
            "BrowserPointerController.prototype.reset = async function (...args) {"
            " state.restore(); await original.apply(this, args); entered.resolve();"
            " try { await resume.promise; } finally { state.settled = true; } }; return 'installed';"
        )}, actor="agent")
        assert installed["result"] == "installed"
        closed = await client.perform(peer, "run", {"code": (
            f"const state = globalThis[{key}]; await state.page.close(); await state.entered.promise;"
            "return { closed: state.page.isClosed(), settled: state.settled };"
        )}, actor="agent")
        assert closed["result"] == {"closed": True, "settled": False}
        with pytest.raises(BrowserRequestError) as caught:
            await client.stop_resource(target)
        assert caught.value.code == "browser_close_operation_drain_timeout" and caught.value.status == 408
        pending = await client.get_resource(target)
        assert pending["status"] == "running" and pending["reconciliation_status"] == "unknown"
        assert pending["pages"] == [] and pending["transport_owner_revision"] == 1
        record_path = root / ".boxteam/browser-manager/browsers.json"
        rows = json.loads(record_path.read_text())["browsers"]
        saved_unknown, = [row for row in rows if row["browser_id"] == target]
        assert saved_unknown["status"] == "running" and saved_unknown["reconciliation_status"] == "unknown"
        released = await client.perform(peer, "run", {"code": (
            f"globalThis[{key}].resume.resolve(); await page.setContent('<title>page close peer</title>');"
            "return await page.title();"
        )}, actor="agent")
        assert released["result"] == "page close peer"
        stopped = (await client.stop_resource(target))["browser"]
        assert isinstance(stopped, dict)
        assert stopped["status"] == "closed" and stopped["reconciliation_status"] == "verified"
        assert stopped["pages"] == [] and stopped["transport_owner_revision"] == 1
        rows = json.loads(record_path.read_text())["browsers"]
        saved_final, = [row for row in rows if row["browser_id"] == target]
        assert saved_final["status"] == "closed" and saved_final["reconciliation_status"] == "verified"
        assert closing_page_id in saved_final["resource_transition_error"]
        assert "迟到激活" in saved_final["resource_transition_error"]
        assert (await client.get_resource(target))["pages"] == []
        receipts = {"original": original, "native_close": closed, "failed_stop": caught.value.payload,
            "pending": pending, "saved_unknown": saved_unknown, "stopped": stopped, "saved_final": saved_final}
    finally:
        await client.perform(peer, "run", {"code": (
            f"const state = globalThis[{key}]; state?.restore(); state?.resume.resolve();"
            f"delete globalThis[{key}]; return 'released';"
        )}, actor="agent")
        for rid in (target, peer):
            await client.delete_resource(rid)
        (run.artifacts_dir / "page-close-drain.json").write_text(
            json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
