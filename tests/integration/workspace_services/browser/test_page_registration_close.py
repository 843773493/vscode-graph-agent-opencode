"""真实Chromium弹出页注册尚未结算时，关闭不能提前核实或接纳迟到页面。"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.abstractions.browser_resources import BrowserRequestError
from app.services.infrastructure.browser_manager_client import BrowserManagerClient
from tests.harness.python.run_context import TestRunContext
from tests.integration.workspace_services.browser.test_browser_owner_revision_drain import (
    browser_client as browser_client,  # noqa: PLC0414 - 原装具按当前文件隔离工作区、服务和端口。
)


async def test_late_popup_registration_keeps_close_unknown_until_original_execution_settles(
    browser_client: tuple[BrowserManagerClient, Path, TestRunContext],
) -> None:
    client, root, run = browser_client
    target, peer = (f"browser_{uuid4().hex}" for _ in range(2))
    key = json.dumps("registration_" + uuid4().hex)
    for rid in (target, peer):
        await client.create_browser(browser_id=rid, owner_revision=1, title="弹出页关闭验收", url="about:blank")
    original = await client.get_resource(target)
    receipts: dict[str, object] = {"original": original}
    try:
        # 仅暂停真实CDP结果的交付；页面、context、操作接纳和终态写盘仍由产品创建和结算。
        installed = await client.perform(target, "run", {"code": (
            "const entered = Promise.withResolvers(), resume = Promise.withResolvers();"
            f"const state = globalThis[{key}] = {{ resume, settled: false }};"
            "const original = context.newCDPSession;"
            "context.newCDPSession = async function (...args) {"
            " context.newCDPSession = original; const cdp = await original.apply(this, args);"
            " const send = cdp.send.bind(cdp); cdp.send = async function (method, ...values) {"
            " const result = await send(method, ...values); if (method === 'Network.enable') {"
            " entered.resolve(); try { await resume.promise; } finally { state.settled = true; } }"
            " return result; }; return cdp; };"
            "await context.newPage(); await entered.promise; return 'registration_pending';"
        )}, actor="agent")
        assert installed["result"] == "registration_pending"
        with pytest.raises(BrowserRequestError) as stopped:
            await client.stop_resource(target)
        failure = stopped.value
        assert failure.status == 408 and failure.code == "browser_close_operation_drain_timeout"
        pending = await client.get_resource(target)
        assert pending["status"] == "running" and pending["reconciliation_status"] == "unknown"
        assert pending["transport_owner_revision"] == original["transport_owner_revision"] == 1
        assert pending["pages"] == []
        rows = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())["browsers"]
        saved, = [row for row in rows if row["browser_id"] == target]
        assert saved["status"] == "running" and saved["reconciliation_status"] == "unknown"
        assert saved["page_id"] == original["active_page_id"]
        receipts.update({"failed_stop": failure.payload, "pending": pending, "saved_unknown": saved})
        # 独立资源可继续真实交互，并只解除原CDP等待；不代写资源或恢复状态。
        released = await client.perform(peer, "run", {"code": (
            f"globalThis[{key}].resume.resolve(); await page.setContent('<title>peer remains usable</title>');"
            "return await page.title();"
        )}, actor="agent")
        assert released["result"] == "peer remains usable"
        verified = await client.stop_resource(target)
        closed_browser = verified["browser"]
        assert isinstance(closed_browser, dict)
        assert closed_browser["status"] == "closed"
        assert closed_browser["reconciliation_status"] == "verified"
        assert closed_browser["pages"] == []
        observed = await client.get_resource(target)
        assert observed["pages"] == [] and observed["page_id"] == original["page_id"]
        assert observed["reconciliation_status"] == "verified"
        state = await client.perform(peer, "run", {"code": f"return globalThis[{key}].settled;"}, actor="agent")
        assert state["result"] is True
        deleted = await client.delete_resource(target)
        deleted_browser = deleted["browser"]
        assert isinstance(deleted_browser, dict)
        assert deleted_browser["status"] == "deleted"
        assert deleted_browser["reconciliation_status"] == "verified"
        rows = json.loads((root / ".boxteam/browser-manager/browsers.json").read_text())["browsers"]
        saved, = [row for row in rows if row["browser_id"] == target]
        assert saved["status"] == "deleted" and saved["reconciliation_status"] == "verified"
        assert saved["page_id"] == original["page_id"]
        receipts.update({"verified_stop": verified, "deleted": deleted, "saved_final": saved})
    finally:
        await client.perform(peer, "run", {"code": (
            f"globalThis[{key}]?.resume.resolve(); delete globalThis[{key}]; return 'released';"
        )}, actor="agent")
        for rid in (target, peer):
            await client.delete_resource(rid)
        (run.artifacts_dir / "popup-registration-close.json").write_text(
            json.dumps(receipts, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
